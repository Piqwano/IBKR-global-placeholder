"""
Global RSI Bot — IBKR (v2.1)
================================
Main loop: scan universe → RSI signals → server-side BRACKET orders
(parent market + OCA TP + trailing stop). State persisted to disk.
Daily loss limit + max-DD auto-flatten. Bracket re-attachment on restart.

v2 additions:
  - Volatility-adjusted position sizing (ATR-based vol targeting)
  - VIX-aware market regime (via ibkr_helpers.get_market_regime)
  - Daily P&L summary sent to Discord at each day rollover
  - Embedded FastAPI dashboard (port 8000 by default)

v2.1 additions:
  - Persistent trade_history in bot_state.json (FIFO-trimmed)
  - Trades recorded on RSI exits AND external/bracket exits
    (exit price reconstructed from ib.fills() when possible)
  - Winrate statistics surfaced in /status and daily Discord summary
    for lifetime / 30d / 7d / 24h
  - Per-position trail_pct / tp_pct persisted in state for accurate
    external-close reason inference after restart

v2.1+ hardening (B1–B4 / H1–H4):
  - Deterministic BASE-currency NLV/cash resolution (B1)
  - TP repair via in-place order amend — no unprotected window (B2)
  - Position-aware fill lookup with partial-fill summation (B3)
  - VIX-unavailable fallback degrades to CAUTION/BEAR only (B4)
  - Dashboard per-symbol price timestamps + stale flag (H1)
  - Trade-history NaN/Inf sanitisation + save fallback (H2)
  - Partial-sell defensive reconciliation w/ arithmetic remainder (H3)
  - ADOPT_ORPHAN gate for manual/orphan position adoption (H4)
"""

import asyncio
import json
import logging
import math
import os
import signal
import sys
import time
from datetime import datetime, date, timedelta, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from config import (
    STOCK_UNIVERSE, ASSET_CONFIG, POSITION_SIZE_PCT,
    RSI_OVERSOLD, RSI_OVERBOUGHT, SCAN_INTERVAL_SECS,
    MAX_POSITIONS, DEFAULT_TRAILING_STOP, DEFAULT_TAKE_PROFIT,
    DISCORD_WEBHOOK, PAPER_MODE, CASH_RESERVE_PCT,
    MAX_COMMISSION_PCT, EXCHANGE_COMMISSIONS,
    USE_VOLUME_FILTER, USE_TREND_FILTER, USE_MA20_FILTER,
    USE_ATR_STOPS, ATR_MULTIPLIER,
    USE_BRACKET_ORDERS, STATE_FILE, STATE_SAVE_ON_EVERY_FILL,
    DAILY_LOSS_LIMIT_PCT, MAX_DRAWDOWN_PCT, FLATTEN_ON_MAX_DD,
    DAILY_RESET_TZ, RESET_MAX_DD_ON_START,
    RATE_LIMIT_PER_SYMBOL, ERROR_RETRY_DELAY,
    REGIME_SIZE_MULTIPLIERS, REATTACH_BRACKETS_ON_RECONCILE,
    PARTIAL_SELL_RECONCILE_WAIT,
    USE_VOL_ADJUSTED_SIZING, VOL_TARGET_ANNUAL,
    VOL_SCALAR_MIN, VOL_SCALAR_MAX,
    DASHBOARD_ENABLED, DASHBOARD_PORT,
    SEND_DAILY_SUMMARY,
    TRADE_HISTORY_MAX_SIZE,
    ADOPT_ORPHAN,
)
from ibkr_helpers import (
    get_ib, disconnect, is_connected,
    get_contract, analyze,
    get_market_regime, get_vix_level,
    cash_guard_check, correlation_check,
    buy_stock, sell_stock, get_all_positions, get_account_summary,
    get_prices_batch, is_market_open,
    flatten_all_positions, cancel_open_orders_for,
    attach_bracket_to_existing_position,
    get_recent_sell_fill,
)
from dashboard import start_dashboard, update_dashboard_state

# ══════════════════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("ibkr-rsi")

# ══════════════════════════════════════════════════════════════════════════
#  STATE
# ══════════════════════════════════════════════════════════════════════════

bot_positions: Dict[str, dict] = {}

day_state = {
    "date": None,
    "start_nlv": 0.0,
    "hit_daily_limit": False,
}

dd_state = {
    "peak_nlv": 0.0,
    "hit_max_dd": False,
}

# Closed-trade history — persisted to bot_state.json.
trade_history: List[dict] = []

# In-memory cache of last-known prices + per-symbol write timestamps.
# SAFETY (H1): _last_price_ts keeps the dashboard honest — any shown P&L
# older than 2× SCAN_INTERVAL_SECS is flagged stale so operator decisions
# are based on fresh data, not silently cached values.
_last_prices: Dict[str, float] = {}
_last_price_ts: Dict[str, str] = {}

_shutdown = False


# ══════════════════════════════════════════════════════════════════════════
#  TIME / FLOAT HELPERS
# ══════════════════════════════════════════════════════════════════════════

def _today_in_reset_tz() -> date:
    return datetime.now(ZoneInfo(DAILY_RESET_TZ)).date()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_utc(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _sanitise_float(val, default: float = 0.0) -> float:
    """
    SAFETY (H2): Sanitise any float value bound for trade_history / JSON.
    Rejects NaN and Inf (which serialise to invalid JSON in strict mode and
    can poison downstream calculations). Returns `default` on bad input so
    a single weird fill can't corrupt the whole state file.
    """
    try:
        f = float(val)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def _mark_price(symbol: str, price: Optional[float]):
    """
    SAFETY (H1): Centralised price writeback — keeps prices and timestamps
    in sync. Never bypass this; the dashboard's stale detection depends on
    every _last_prices write having a matching _last_price_ts entry.
    """
    if price is not None:
        _last_prices[symbol] = price
        _last_price_ts[symbol] = _utc_now_iso()


# ══════════════════════════════════════════════════════════════════════════
#  TRADE HISTORY / WINRATES  (H2)
# ══════════════════════════════════════════════════════════════════════════

def record_closed_trade(symbol: str, pos: dict,
                        exit_price: float, exit_qty: int,
                        reason: str) -> Optional[dict]:
    """
    Append a closed-trade record to trade_history. Returns the record, or
    None if the inputs are insufficient (e.g. missing entry price).

    SAFETY (H2): All numeric inputs sanitised through _sanitise_float so a
    NaN / Inf from a degenerate fill can't poison the JSON state file or
    skew winrate calculations.
    """
    entry = _sanitise_float(pos.get("entry"))
    exit_price = _sanitise_float(exit_price)
    try:
        exit_qty = int(exit_qty or 0)
    except (TypeError, ValueError):
        exit_qty = 0

    if entry <= 0 or exit_qty <= 0 or exit_price <= 0:
        log.warning(
            f"record_closed_trade: skipping {symbol} — bad inputs "
            f"entry={entry} exit={exit_price} qty={exit_qty} "
            f"(reason={reason}) — trade NOT recorded"
        )
        return None

    pnl = (exit_price - entry) * exit_qty
    pnl_pct = (exit_price - entry) / entry * 100

    # Double-check derived values are clean
    if math.isnan(pnl) or math.isinf(pnl) or math.isnan(pnl_pct) or math.isinf(pnl_pct):
        log.error(f"record_closed_trade: NaN/Inf in derived values for {symbol} — refusing to record")
        return None

    now_iso = _utc_now_iso()
    opened_at = pos.get("opened_at")
    # Validate opened_at is parseable; if not, fall back to now so the record
    # at least appears in time-windowed winrates instead of silently dropping.
    if opened_at and _parse_iso_utc(opened_at) is None:
        log.warning(f"record_closed_trade: {symbol} has unparseable opened_at={opened_at!r}; using now")
        opened_at = now_iso

    trade = {
        "symbol": symbol,
        "name": pos.get("name", ""),
        "exchange": pos.get("exchange", ""),
        "currency": pos.get("currency", ""),
        "entry_price": round(entry, 4),
        "exit_price": round(exit_price, 4),
        "qty": exit_qty,
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 3),
        "opened_at": opened_at,
        "closed_at": now_iso,
        "reason": reason,
    }
    trade_history.append(trade)

    # FIFO trim to keep state file bounded
    overflow = len(trade_history) - TRADE_HISTORY_MAX_SIZE
    if overflow > 0:
        del trade_history[:overflow]

    emoji = "✅" if pnl > 0 else ("⚪" if pnl == 0 else "❌")
    log.info(
        f"  {emoji} Trade logged: {symbol} {reason} | "
        f"${entry:.2f}→${exit_price:.2f} x{exit_qty} | "
        f"P&L ${pnl:+,.2f} ({pnl_pct:+.2f}%)"
    )
    return trade


def compute_winrates() -> dict:
    """
    Winrate = (trades with pnl_pct > 0 / total closed trades) × 100, to 1dp.
    Returns None for any window with zero trades (JSON-null).

    SAFETY (H2):
      - 'lifetime' counts ALL persisted trades (no timestamp dependency),
        so a record with an unparseable closed_at still contributes to the
        lifetime denominator.
      - Time-windowed (24h/7d/30d) filters drop unparseable records but
        LOG a warning so data-quality issues surface instead of silently
        degrading statistics.
    """
    now = datetime.now(timezone.utc)
    unparseable_count = 0

    def _filter_since(seconds: int) -> List[dict]:
        nonlocal unparseable_count
        cutoff = now - timedelta(seconds=seconds)
        out = []
        for t in trade_history:
            dt = _parse_iso_utc(t.get("closed_at"))
            if dt is None:
                unparseable_count += 1
                continue
            if dt >= cutoff:
                out.append(t)
        return out

    def _winrate(trades: List[dict]) -> Optional[float]:
        if not trades:
            return None
        wins = sum(1 for t in trades if _sanitise_float(t.get("pnl_pct")) > 0)
        return round(wins / len(trades) * 100, 1)

    # Lifetime uses full list — NEVER drops records for parse failures
    lifetime = list(trade_history)
    past_30 = _filter_since(30 * 86400)
    past_7 = _filter_since(7 * 86400)
    past_24h = _filter_since(86400)

    # unparseable_count gets incremented 3× per bad record (once per window);
    # divide by 3 for actual count.
    if unparseable_count > 0:
        actual = unparseable_count // 3
        log.warning(
            f"compute_winrates: {actual} trade record(s) have unparseable "
            f"closed_at — excluded from time-windowed stats (lifetime unaffected)"
        )

    return {
        "winrates": {
            "lifetime": _winrate(lifetime),
            "past_30_days": _winrate(past_30),
            "past_7_days": _winrate(past_7),
            "past_24_hours": _winrate(past_24h),
        },
        "counts": {
            "lifetime": len(lifetime),
            "past_30_days": len(past_30),
            "past_7_days": len(past_7),
            "past_24_hours": len(past_24h),
        },
    }


def _classify_external_close_reason(entry: float, exit_price: float,
                                    tp_pct: float, trail_pct: float) -> str:
    """
    Best-effort inference of why a bracket-exited position closed. We don't
    know for sure which of the two OCA children filled without crawling fills
    more aggressively, so we infer from price movement vs the configured
    TP / trail thresholds.
    """
    if entry <= 0 or exit_price <= 0:
        return "bracket_exit"
    ret = (exit_price - entry) / entry
    if ret >= tp_pct * 0.95:
        return "take_profit"
    if ret <= -trail_pct * 0.5:
        return "trailing_stop"
    return "bracket_exit"


# ══════════════════════════════════════════════════════════════════════════
#  STATE PERSISTENCE  (H2)
# ══════════════════════════════════════════════════════════════════════════

def save_state():
    """
    SAFETY (H2): Atomic write (.tmp + os.replace). Uses allow_nan=False so a
    stray NaN/Inf never lands on disk. If trade_history serialisation fails
    (bad record), falls back to preserving prior on-disk history rather than
    destroying it — positions path stays functional independently.
    """
    try:
        serialisable = {}
        for sym, pos in bot_positions.items():
            serialisable[sym] = {
                "entry": pos["entry"],
                "peak": pos["peak"],
                "exchange": pos["exchange"],
                "currency": pos["currency"],
                "name": pos["name"],
                "qty": pos["qty"],
                "tp_order_id": pos.get("tp_order_id"),
                "trail_order_id": pos.get("trail_order_id"),
                "opened_at": pos.get("opened_at"),
                "trail_pct": pos.get("trail_pct"),
                "tp_pct": pos.get("tp_pct"),
            }
        base_payload = {
            "bot_positions": serialisable,
            "day_state": {
                "date": day_state["date"].isoformat() if day_state["date"] else None,
                "start_nlv": day_state["start_nlv"],
                "hit_daily_limit": day_state["hit_daily_limit"],
            },
            "dd_state": dd_state,
            "saved_at": _utc_now_iso(),
        }

        # Try full payload first (positions + history)
        try:
            full_payload = {**base_payload, "trade_history": trade_history}
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(full_payload, f, indent=2, allow_nan=False)
            os.replace(tmp, STATE_FILE)
            return
        except (ValueError, TypeError) as history_err:
            # Strict JSON serialisation failed — almost certainly a bad
            # float in trade_history. Fall back to positions-only to protect
            # the hot recovery path.
            log.error(
                f"save_state: trade_history serialisation FAILED ({history_err}) — "
                f"saving positions/state WITHOUT in-memory history this cycle. "
                f"Investigate trade_history for NaN/Inf entries."
            )
            fallback_payload = {**base_payload, "trade_history": []}
            # Preserve prior on-disk history if loadable — don't silently
            # destroy it just because the in-memory list is sick.
            if os.path.exists(STATE_FILE):
                try:
                    with open(STATE_FILE) as f:
                        prior = json.load(f)
                    if isinstance(prior.get("trade_history"), list):
                        fallback_payload["trade_history"] = prior["trade_history"]
                except Exception:
                    pass  # keep empty list — better than failing save entirely
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(fallback_payload, f, indent=2, allow_nan=False)
            os.replace(tmp, STATE_FILE)

    except Exception as e:
        log.error(f"save_state failed: {e}")


def load_state():
    global day_state, dd_state, trade_history
    if not os.path.exists(STATE_FILE):
        log.info(f"📂 No prior state file at {STATE_FILE}")
        return

    try:
        with open(STATE_FILE) as f:
            payload = json.load(f)

        positions_data = payload.get("bot_positions", {})
        universe_lookup = {s: (s, e, c, n) for s, e, c, n in STOCK_UNIVERSE}

        for sym, pos in positions_data.items():
            if sym not in universe_lookup:
                log.info(f"   Skip persisted {sym} — not in universe")
                continue
            _, exch, curr, name = universe_lookup[sym]
            contract = get_contract(sym, exch, curr)
            if not contract:
                log.warning(f"   Could not re-qualify {sym} — skipping")
                continue
            bot_positions[sym] = {
                "entry": pos["entry"],
                "peak": pos["peak"],
                "contract": contract,
                "exchange": pos["exchange"],
                "currency": pos["currency"],
                "name": pos["name"],
                "qty": pos["qty"],
                "tp_order_id": pos.get("tp_order_id"),
                "trail_order_id": pos.get("trail_order_id"),
                "opened_at": pos.get("opened_at"),
                "trail_pct": pos.get("trail_pct"),
                "tp_pct": pos.get("tp_pct"),
            }

        ds = payload.get("day_state", {})
        if ds.get("date"):
            day_state["date"] = date.fromisoformat(ds["date"])
        day_state["start_nlv"] = ds.get("start_nlv", 0.0)
        day_state["hit_daily_limit"] = ds.get("hit_daily_limit", False)

        dd = payload.get("dd_state", {})
        dd_state["peak_nlv"] = dd.get("peak_nlv", 0.0)
        dd_state["hit_max_dd"] = dd.get("hit_max_dd", False)

        loaded_trades = payload.get("trade_history", [])
        if isinstance(loaded_trades, list):
            trade_history.clear()
            trade_history.extend(loaded_trades[-TRADE_HISTORY_MAX_SIZE:])

        if dd_state["hit_max_dd"] and RESET_MAX_DD_ON_START:
            log.warning("🟡 RESET_MAX_DD=1 detected — clearing persisted max-DD halt flag")
            dd_state["hit_max_dd"] = False

        log.info(
            f"📂 Loaded state: {len(bot_positions)} positions | "
            f"{len(trade_history)} historical trades | "
            f"start NLV ${day_state['start_nlv']:,.2f} | "
            f"peak NLV ${dd_state['peak_nlv']:,.2f} | "
            f"max_dd_halted={dd_state['hit_max_dd']} | "
            f"daily_halted={day_state['hit_daily_limit']}"
        )
    except Exception as e:
        log.error(f"load_state failed: {e} — starting fresh")


# ══════════════════════════════════════════════════════════════════════════
#  NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════

def notify(message: str):
    if not DISCORD_WEBHOOK:
        return
    try:
        import requests
        requests.post(DISCORD_WEBHOOK, json={"content": message}, timeout=5)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════
#  DAILY SUMMARY
# ══════════════════════════════════════════════════════════════════════════

def _format_wr(val: Optional[float]) -> str:
    return f"{val}%" if val is not None else "—"


def send_daily_summary(current_nlv: float):
    """
    Called at day rollover BEFORE day_state is reset. Uses day_state still
    holding yesterday's values (start_nlv, date).
    """
    if not SEND_DAILY_SUMMARY or not DISCORD_WEBHOOK:
        return
    if day_state["date"] is None or day_state["start_nlv"] <= 0:
        return  # first run — nothing to summarise

    start_nlv = day_state["start_nlv"]
    day_pnl_dollars = current_nlv - start_nlv
    day_pnl_pct = (day_pnl_dollars / start_nlv * 100) if start_nlv > 0 else 0.0

    peak = dd_state["peak_nlv"]
    dd_pct = ((current_nlv - peak) / peak * 100) if peak > 0 else 0.0

    total_exposure = 0.0
    for sym, pos in bot_positions.items():
        mkt = _last_prices.get(sym) or pos.get("entry", 0.0)
        total_exposure += pos.get("qty", 0) * mkt
    exposure_pct = (total_exposure / current_nlv * 100) if current_nlv > 0 else 0.0

    try:
        regime, _ = get_market_regime()
    except Exception:
        regime = "UNKNOWN"

    try:
        from ibkr_helpers import _manager as _mgr  # type: ignore
        vix_level = _mgr.get_vix_cache()
    except Exception:
        vix_level = None
    vix_str = f" | VIX {vix_level:.1f}" if vix_level is not None else ""

    wr = compute_winrates()
    w = wr["winrates"]
    c = wr["counts"]

    mode = "PAPER" if PAPER_MODE else "LIVE"
    emoji = "📈" if day_pnl_dollars >= 0 else "📉"
    dd_emoji = "" if dd_pct >= -2 else (" ⚠️" if dd_pct > -10 else " 🛑")
    date_str = day_state["date"].isoformat()

    msg = (
        f"{emoji} **[{mode}] Daily Summary — {date_str}**\n"
        f"• NLV: ${current_nlv:,.2f}\n"
        f"• Day P&L: ${day_pnl_dollars:+,.2f} ({day_pnl_pct:+.2f}%)\n"
        f"• DD from peak: {dd_pct:+.2f}%{dd_emoji}\n"
        f"• Open positions: {len(bot_positions)}  |  "
        f"Exposure: {exposure_pct:.1f}% of NLV\n"
        f"• Regime: {regime}{vix_str}\n"
        f"• Winrate — 24h: {_format_wr(w['past_24_hours'])} ({c['past_24_hours']}) | "
        f"7d: {_format_wr(w['past_7_days'])} ({c['past_7_days']}) | "
        f"30d: {_format_wr(w['past_30_days'])} ({c['past_30_days']}) | "
        f"Lifetime: {_format_wr(w['lifetime'])} ({c['lifetime']})"
    )
    notify(msg)
    log.info(
        f"📬 Daily summary sent — {date_str} | PnL {day_pnl_pct:+.2f}% | "
        f"Pos {len(bot_positions)} | Regime {regime} | "
        f"WR 24h={_format_wr(w['past_24_hours'])}  lifetime={_format_wr(w['lifetime'])}"
    )


# ══════════════════════════════════════════════════════════════════════════
#  DAILY / DRAWDOWN CHECKS
# ══════════════════════════════════════════════════════════════════════════

def roll_over_day_if_needed(current_nlv: float):
    today = _today_in_reset_tz()
    if day_state["date"] != today:
        send_daily_summary(current_nlv)

        log.info(f"📅 New trading day ({DAILY_RESET_TZ}) — resetting daily tracker. "
                 f"Start NLV: ${current_nlv:,.2f}")
        day_state["date"] = today
        day_state["start_nlv"] = current_nlv
        day_state["hit_daily_limit"] = False
        save_state()


def check_daily_loss(current_nlv: float) -> bool:
    if day_state["start_nlv"] <= 0:
        return False
    pnl_pct = (current_nlv - day_state["start_nlv"]) / day_state["start_nlv"]
    if pnl_pct <= -DAILY_LOSS_LIMIT_PCT and not day_state["hit_daily_limit"]:
        day_state["hit_daily_limit"] = True
        log.critical(f"🛑 DAILY LOSS LIMIT HIT — {pnl_pct*100:+.2f}% "
                     f"(limit {-DAILY_LOSS_LIMIT_PCT*100:.1f}%) | "
                     f"${day_state['start_nlv']:,.2f} → ${current_nlv:,.2f}")
        notify(f"🛑 Daily loss limit hit {pnl_pct*100:+.2f}% — halting new buys for today")
        save_state()
    return day_state["hit_daily_limit"]


def check_max_drawdown(current_nlv: float) -> bool:
    if current_nlv > dd_state["peak_nlv"]:
        dd_state["peak_nlv"] = current_nlv

    if dd_state["peak_nlv"] <= 0:
        return False

    dd_pct = (current_nlv - dd_state["peak_nlv"]) / dd_state["peak_nlv"]
    if dd_pct <= -MAX_DRAWDOWN_PCT and not dd_state["hit_max_dd"]:
        dd_state["hit_max_dd"] = True
        log.critical(f"🛑 MAX DRAWDOWN HIT — {dd_pct*100:+.2f}% from peak ${dd_state['peak_nlv']:,.2f}")
        notify(f"🛑🛑 MAX DRAWDOWN {dd_pct*100:+.2f}% hit. "
               f"{'FLATTENING ALL' if FLATTEN_ON_MAX_DD else 'Halting new buys'}. "
               f"Restart with RESET_MAX_DD=1 to resume.")
        if FLATTEN_ON_MAX_DD:
            try:
                # Record trades for all open positions BEFORE flattening —
                # use last known prices as best-effort exit estimates.
                for sym, pos in list(bot_positions.items()):
                    exit_px = _last_prices.get(sym) or pos.get("entry", 0.0)
                    record_closed_trade(
                        sym, pos, exit_px, int(pos.get("qty", 0)),
                        "max_dd_flatten"
                    )
                flatten_all_positions(reason=f"max_dd_{dd_pct*100:.1f}pct")
                bot_positions.clear()
                # SAFETY (H1): clear both price + timestamp caches together
                _last_prices.clear()
                _last_price_ts.clear()
            except Exception as e:
                log.error(f"Flatten failed: {e}")
        save_state()
    return dd_state["hit_max_dd"]


# ══════════════════════════════════════════════════════════════════════════
#  VOLATILITY-ADJUSTED SIZING
# ══════════════════════════════════════════════════════════════════════════

def _apply_vol_scalar(symbol: str, base_amount: float, analysis: dict) -> float:
    """
    Scale a base position amount by target-vol / realised-vol. Returns the
    adjusted amount. Clamps the scalar to [VOL_SCALAR_MIN, VOL_SCALAR_MAX].
    """
    if not USE_VOL_ADJUSTED_SIZING:
        return base_amount

    atr_sizing = analysis.get("atr_sizing")
    price = analysis.get("price")
    if not atr_sizing or not price or price <= 0:
        return base_amount

    atr_pct = atr_sizing / price                        # daily vol proxy
    annualised_vol = atr_pct * math.sqrt(252)
    if annualised_vol <= 0:
        return base_amount

    raw_scalar = VOL_TARGET_ANNUAL / annualised_vol
    vol_scalar = max(VOL_SCALAR_MIN, min(VOL_SCALAR_MAX, raw_scalar))
    adjusted = round(base_amount * vol_scalar, 2)

    log.info(
        f"  📐 {symbol}: ATR20={atr_pct*100:.2f}%/day → ann.vol {annualised_vol*100:.1f}% "
        f"→ scalar {vol_scalar:.2f}"
        f"{' (clamped)' if abs(vol_scalar - raw_scalar) > 1e-6 else ''}"
        f" | ${base_amount:.2f} → ${adjusted:.2f}"
    )
    return adjusted


# ══════════════════════════════════════════════════════════════════════════
#  ENTRY
# ══════════════════════════════════════════════════════════════════════════

def try_buy(symbol: str, exchange: str, currency: str, name: str,
            analysis: dict, regime_mult: float):
    if day_state["hit_daily_limit"] or dd_state["hit_max_dd"]:
        return

    open_now, reason = is_market_open(exchange)
    if not open_now:
        log.info(f"  🌙 {symbol} skip — {reason}")
        return

    allowed, reason = correlation_check(symbol, bot_positions)
    if not allowed:
        log.info(f"  ⛔ {symbol} blocked — {reason}")
        return

    ok, cash, portfolio = cash_guard_check()
    if not ok:
        return
    if portfolio <= 0:
        return

    base_amount = round(portfolio * POSITION_SIZE_PCT * regime_mult, 2)
    if regime_mult < 1.0:
        log.info(f"  ⚠️  {symbol}: regime mult {regime_mult*100:.0f}% → base ${base_amount:.2f}")

    amount = _apply_vol_scalar(symbol, base_amount, analysis)

    if amount < 10:
        log.warning(f"  {symbol}: Position too small after sizing (${amount:.2f})")
        return

    if not PAPER_MODE:
        est_commission = EXCHANGE_COMMISSIONS.get(exchange, 10.0)
        commission_pct = est_commission / amount if amount > 0 else 1.0
        if commission_pct > MAX_COMMISSION_PCT:
            min_portfolio = est_commission / MAX_COMMISSION_PCT / POSITION_SIZE_PCT
            log.info(f"  💸 {symbol} ({exchange}): ${est_commission:.0f} fee = "
                     f"{commission_pct*100:.1f}% — unlocks at ~${min_portfolio:,.0f}")
            return

    contract = get_contract(symbol, exchange, currency)
    if not contract:
        return

    cfg = ASSET_CONFIG.get(symbol, {})
    trail_pct = cfg.get("trailing_stop", DEFAULT_TRAILING_STOP)
    tp_pct = cfg.get("take_profit", DEFAULT_TAKE_PROFIT)

    if USE_ATR_STOPS and analysis.get("atr"):
        atr_stop_pct = (ATR_MULTIPLIER * analysis["atr"]) / analysis["price"]
        trail_pct = max(trail_pct, atr_stop_pct)
        log.info(f"  📏 {symbol}: ATR stop = {atr_stop_pct*100:.2f}% → using {trail_pct*100:.2f}%")

    result = buy_stock(contract, amount, trail_pct, tp_pct)
    if not result:
        return

    bot_positions[symbol] = {
        "entry": result["price"],
        "peak": result["price"],
        "contract": contract,
        "exchange": exchange,
        "currency": currency,
        "name": name,
        "qty": result["qty"],
        "tp_order_id": result.get("tp_order_id"),
        "trail_order_id": result.get("trail_order_id"),
        "opened_at": _utc_now_iso(),
        "trail_pct": trail_pct,
        "tp_pct": tp_pct,
    }
    _mark_price(symbol, result["price"])

    mode = "PAPER" if PAPER_MODE else "LIVE"
    regime_tag = f" [regime {regime_mult*100:.0f}%]" if regime_mult < 1.0 else ""
    bracket_tag = " [bracket]" if USE_BRACKET_ORDERS and result.get("trail_order_id") else ""
    notify(f"🟢 [{mode}] BUY {symbol} ({name}) @ ${result['price']:.2f} x{result['qty']} "
           f"| RSI {analysis['rsi']:.1f} | {exchange}{regime_tag}{bracket_tag}")

    if STATE_SAVE_ON_EVERY_FILL:
        save_state()


# ══════════════════════════════════════════════════════════════════════════
#  EXIT MONITORING
# ══════════════════════════════════════════════════════════════════════════

def _handle_partial_sell_remainder(symbol: str, pos: dict, remaining_qty: int):
    log.warning(f"  ⚠️  {symbol}: partial sell — {remaining_qty} shares remain, re-attaching bracket")
    contract = pos["contract"]
    entry = pos["entry"]
    cfg = ASSET_CONFIG.get(symbol, {})
    trail_pct = float(pos.get("trail_pct") or cfg.get("trailing_stop", DEFAULT_TRAILING_STOP))
    tp_pct = float(pos.get("tp_pct") or cfg.get("take_profit", DEFAULT_TAKE_PROFIT))
    tp_id, trail_id = attach_bracket_to_existing_position(
        contract, remaining_qty, entry, trail_pct, tp_pct
    )
    bot_positions[symbol]["qty"] = remaining_qty
    bot_positions[symbol]["tp_order_id"] = tp_id
    bot_positions[symbol]["trail_order_id"] = trail_id
    # Preserve original exit thresholds
    bot_positions[symbol]["trail_pct"] = trail_pct
    bot_positions[symbol]["tp_pct"] = tp_pct


def _reconcile_post_sell(symbol: str, pos: dict,
                         sell_result: dict, price_fallback: float):
    """
    SAFETY (H3): Post-sell reconciliation with the IBKR position book.
    Wrapped in try/except to ensure that if get_all_positions() fails
    (disconnect, timeout, etc.), we still:
      - use sell_result's filled_qty to compute a best-effort remaining qty
      - IMMEDIATELY re-attach a bracket to the remainder so we don't leave
        shares unprotected waiting for the next 15-minute scan cycle.

    Also replaces the blind sleep(1.5) with event-driven polling (200ms)
    for faster typical completion without reducing worst-case safety.
    """
    ib = get_ib()
    fill_price = sell_result.get("avg_fill_price") or price_fallback
    filled_qty = int(sell_result.get("filled_qty") or 0)
    orig_qty = int(pos.get("qty", 0))

    remaining_qty: Optional[int] = None
    try:
        # Event-driven wait — shorter typical case, same worst case.
        deadline = time.time() + PARTIAL_SELL_RECONCILE_WAIT
        while time.time() < deadline:
            ib.waitOnUpdate(timeout=0.2)
            try:
                remaining = get_all_positions().get(symbol)
                remaining_qty = int(remaining["qty"]) if remaining else 0
                break
            except Exception:
                # Transient — keep trying until deadline
                continue
    except Exception as e:
        log.warning(f"Post-sell IBKR position check failed for {symbol}: {e}")
        remaining_qty = None

    # If IBKR query failed entirely, fall back to arithmetic.
    # Defensive: compute max possible remainder so we protect it.
    if remaining_qty is None:
        arithmetic_remainder = max(0, orig_qty - filled_qty)
        log.warning(
            f"  ⚠️  {symbol}: could not query IBKR positions post-sell; "
            f"assuming {arithmetic_remainder} shares remain (orig {orig_qty} - filled {filled_qty}). "
            f"Re-attaching bracket defensively."
        )
        remaining_qty = arithmetic_remainder

    if remaining_qty <= 0:
        # Full close path
        record_closed_trade(
            symbol, pos, fill_price, filled_qty or orig_qty, "rsi_exit"
        )
        bot_positions.pop(symbol, None)
        _last_prices.pop(symbol, None)
        _last_price_ts.pop(symbol, None)
    else:
        # Partial close — record what sold, re-attach bracket to remainder
        sold_qty = max(0, orig_qty - remaining_qty)
        if sold_qty > 0:
            record_closed_trade(
                symbol, pos, fill_price, sold_qty, "rsi_exit_partial"
            )
        _handle_partial_sell_remainder(symbol, pos, remaining_qty)

    if STATE_SAVE_ON_EVERY_FILL:
        save_state()


def _record_external_close(symbol: str, pos: dict,
                           last_known_price: Optional[float]):
    """
    Called when IBKR no longer reports a position we were tracking.

    SAFETY (B3): Passes pos["opened_at"] as opened_after to
    get_recent_sell_fill so fills from earlier trades on the same contract
    can't be attributed to this one, AND all partial fills from this exit
    are summed into a weighted-average exit price.
    """
    exit_price: Optional[float] = None
    exit_qty = int(pos.get("qty") or 0)
    exit_reason = "bracket_exit"

    # Parse the position's open time — hard lower bound on fills
    opened_after_dt = _parse_iso_utc(pos.get("opened_at"))

    try:
        fill = get_recent_sell_fill(
            pos["contract"],
            lookback_seconds=86400,
            opened_after=opened_after_dt,
        )
        if fill:
            exit_price = fill["price"]
            # Cap at tracked qty defensively — never over-attribute shares
            # to this position if the fill lookup returns more than we held.
            if fill.get("qty") and fill["qty"] > 0:
                if exit_qty > 0:
                    exit_qty = min(exit_qty, fill["qty"]) if fill["qty"] < exit_qty else exit_qty
                else:
                    exit_qty = fill["qty"]
    except Exception as e:
        log.warning(f"Fill lookup failed for {symbol}: {e}")

    if exit_price is None:
        exit_price = last_known_price or pos.get("entry", 0.0)
        exit_reason = "external_close_estimated"

    entry = float(pos.get("entry") or 0.0)
    trail_pct = float(pos.get("trail_pct") or ASSET_CONFIG.get(symbol, {}).get("trailing_stop", DEFAULT_TRAILING_STOP))
    tp_pct = float(pos.get("tp_pct") or ASSET_CONFIG.get(symbol, {}).get("take_profit", DEFAULT_TAKE_PROFIT))

    if exit_reason == "bracket_exit":
        exit_reason = _classify_external_close_reason(entry, exit_price, tp_pct, trail_pct)

    record_closed_trade(symbol, pos, exit_price, exit_qty, exit_reason)


def check_exits():
    ibkr_positions = get_all_positions()

    # Reconcile positions that IBKR no longer reports (bracket filled or
    # manual close). Record them before popping.
    for symbol in list(bot_positions.keys()):
        if symbol not in ibkr_positions or ibkr_positions[symbol]["qty"] <= 0:
            pos = bot_positions.pop(symbol, None)
            last_known = _last_prices.pop(symbol, None)
            _last_price_ts.pop(symbol, None)
            if pos:
                _record_external_close(symbol, pos, last_known)
                log.info(f"  {symbol}: Position closed externally (bracket or manual) — reconciled")
                mode = "PAPER" if PAPER_MODE else "LIVE"
                notify(f"✅ [{mode}] {symbol} closed (bracket exit or manual)")
        elif symbol in ibkr_positions:
            bot_positions[symbol]["qty"] = int(ibkr_positions[symbol]["qty"])

    if not bot_positions:
        if STATE_SAVE_ON_EVERY_FILL:
            save_state()
        return

    dash_contracts = {sym: pos["contract"] for sym, pos in bot_positions.items()}
    prices = get_prices_batch(dash_contracts)
    for sym, p in prices.items():
        _mark_price(sym, p)

    for symbol in list(bot_positions.keys()):
        pos = bot_positions[symbol]
        contract = pos["contract"]
        price = prices.get(symbol)
        if price is None:
            continue

        if price > pos["peak"]:
            bot_positions[symbol]["peak"] = price

        cfg = ASSET_CONFIG.get(symbol, {})
        rsi_exit = cfg.get("rsi_exit", RSI_OVERBOUGHT)

        analysis = analyze(contract)
        if not analysis:
            continue

        if analysis["rsi"] >= rsi_exit:
            entry = pos["entry"]
            qty = pos["qty"]
            change_pct = (price - entry) / entry * 100
            log.info(f"🔔 RSI exit {symbol} — RSI {analysis['rsi']:.1f} ≥ {rsi_exit}")

            try:
                cancel_open_orders_for(contract)
            except Exception as e:
                log.warning(f"Could not cancel OCA for {symbol}: {e}")

            sell_result = sell_stock(contract, qty)
            if sell_result:
                fill_price = sell_result.get("avg_fill_price") or price
                filled_qty = int(sell_result.get("filled_qty") or qty)
                pnl_est = (fill_price - entry) * filled_qty
                emoji = "✅" if pnl_est >= 0 else "❌"
                log.info(f"{emoji} SELL {symbol} — RSI overbought {change_pct:+.1f}% "
                         f"| Entry ${entry:.2f} → ${fill_price:.2f} | Est P&L ${pnl_est:+,.2f}")
                mode = "PAPER" if PAPER_MODE else "LIVE"
                notify(f"{emoji} [{mode}] SELL {symbol} ({pos['name']}) — "
                       f"RSI {analysis['rsi']:.1f} | {change_pct:+.1f}%")

                # SAFETY (H3): defensive reconciliation — handles IBKR query
                # failures by falling back to arithmetic remainder and
                # protecting it immediately.
                _reconcile_post_sell(symbol, pos, sell_result, price_fallback=price)


# ══════════════════════════════════════════════════════════════════════════
#  SCAN
# ══════════════════════════════════════════════════════════════════════════

EXCHANGE_NAMES = {
    "SMART": "🇺🇸 US", "ASX": "🇦🇺 ASX", "LSE": "🇬🇧 LSE",
    "IBIS": "🇩🇪 XETRA", "SBF": "🇫🇷 Paris", "AEB": "🇳🇱 Amsterdam",
    "SEHK": "🇭🇰 HKEX", "SGX": "🇸🇬 SGX",
}


def scan_all_markets():
    if day_state["hit_daily_limit"]:
        log.info("⏸️  Daily loss limit active — skipping scan")
        return
    if dd_state["hit_max_dd"]:
        log.info("⏸️  Max DD active — skipping scan")
        return

    regime, regime_mult = get_market_regime()
    if regime_mult == 0:
        log.info("🔴 BEAR — skipping all buys this cycle")
        return

    by_exchange = {}
    for sym, exch, curr, name in STOCK_UNIVERSE:
        by_exchange.setdefault(exch, []).append((sym, exch, curr, name))

    signals_found = 0

    for exchange, stocks in by_exchange.items():
        open_now, reason = is_market_open(exchange)
        status = "OPEN" if open_now else "CLOSED"
        log.info(f"\n  ── {EXCHANGE_NAMES.get(exchange, exchange)} ({len(stocks)} stocks) [{status}] ──")

        if not open_now:
            log.info(f"     Skip — {reason}")
            continue

        for symbol, exch, currency, name in stocks:
            if _shutdown:
                return
            if symbol in bot_positions:
                continue
            if len(bot_positions) >= MAX_POSITIONS:
                break

            contract = get_contract(symbol, exch, currency)
            if not contract:
                continue

            analysis = analyze(contract)
            if analysis is None:
                continue

            rsi = analysis["rsi"]
            price = analysis["price"]
            vol_ok = analysis["vol_ok"] if USE_VOLUME_FILTER else True
            trend_ok = analysis["trend_ok"] if USE_TREND_FILTER else True
            ma20_ok = analysis["ma20_ok"] if USE_MA20_FILTER else True

            gap = rsi - RSI_OVERSOLD
            filters_passed = vol_ok and trend_ok and ma20_ok

            if rsi <= RSI_OVERSOLD and filters_passed:
                sig = "🟢 SIGNAL"
                signals_found += 1
            elif rsi <= RSI_OVERSOLD:
                failed = []
                if USE_VOLUME_FILTER and not analysis["vol_ok"]: failed.append("vol✗")
                if USE_TREND_FILTER and not analysis["trend_ok"]: failed.append("trend✗")
                if USE_MA20_FILTER and not analysis["ma20_ok"]: failed.append("ma20✗")
                sig = f"🔵 RSI ok but {','.join(failed)}"
            elif rsi <= RSI_OVERSOLD + 10:
                sig = "🟡 approaching"
            else:
                sig = ""

            log.info(f"  {symbol:<8} {name:<20} ${price:<10.2f} RSI:{rsi:5.1f} ({gap:+.1f}) {sig}")

            if rsi <= RSI_OVERSOLD and filters_passed:
                log.info(f"  🚨 {symbol}: attempting buy")
                try_buy(symbol, exch, currency, name, analysis, regime_mult)

            time.sleep(RATE_LIMIT_PER_SYMBOL)

    log.info(f"\n  Signals: {signals_found} | Positions: {len(bot_positions)}/{MAX_POSITIONS}")


# ══════════════════════════════════════════════════════════════════════════
#  STARTUP  (H4)
# ══════════════════════════════════════════════════════════════════════════

def reconcile_existing_positions():
    """
    Reconcile IBKR-reported positions against bot_positions from state.

    SAFETY (H4): Positions present in IBKR but NOT in persisted state are
    ORPHANS — possibly manual trades, possibly stale state. We do NOT
    auto-adopt them unless ADOPT_ORPHAN=1 is explicitly set in the env.
    This prevents the bot from silently taking over a human's mis-click
    and attaching brackets at its guessed (default) exit percentages.

    Positions already in bot_positions (loaded from state) are handled
    by the normal check_exits() flow and are not touched here.
    """
    existing = get_all_positions()
    if not existing:
        return
    universe_lookup = {s: (s, e, c, n) for s, e, c, n in STOCK_UNIVERSE}

    orphan_count = 0
    adopted_count = 0
    skipped_count = 0

    for sym, info in existing.items():
        if sym in bot_positions:
            continue  # already tracked from state

        qty = int(info["qty"])
        if qty <= 0:
            continue

        if sym not in universe_lookup:
            log.info(f"   ℹ️  {sym} not in universe — ignoring (external trade)")
            continue

        orphan_count += 1

        # SAFETY (H4): orphan gate
        if not ADOPT_ORPHAN:
            log.warning(
                f"   🚫 ORPHAN {sym}: qty={qty} @ ${info['avg_cost']:.2f} — "
                f"NOT adopting (ADOPT_ORPHAN=1 required). "
                f"Position is untracked; its existing IBKR orders (if any) "
                f"remain intact. Set ADOPT_ORPHAN=1 and restart to claim."
            )
            skipped_count += 1
            continue

        # ADOPT_ORPHAN=1 — proceed with adoption + bracket attachment
        _, exch, curr, name = universe_lookup[sym]

        contract = get_contract(sym, exch, curr)
        if not contract:
            log.error(f"   ❌ Could not qualify contract for orphan {sym} — skipping")
            skipped_count += 1
            continue

        entry = info["avg_cost"]
        cfg = ASSET_CONFIG.get(sym, {})
        trail_pct = cfg.get("trailing_stop", DEFAULT_TRAILING_STOP)
        tp_pct = cfg.get("take_profit", DEFAULT_TAKE_PROFIT)
        tp_id = None
        trail_id = None

        if REATTACH_BRACKETS_ON_RECONCILE:
            tp_id, trail_id = attach_bracket_to_existing_position(
                contract, qty, entry, trail_pct, tp_pct
            )

        bot_positions[sym] = {
            "entry": entry, "peak": entry,
            "contract": contract,
            "exchange": exch, "currency": curr, "name": name,
            "qty": qty,
            "tp_order_id": tp_id,
            "trail_order_id": trail_id,
            "opened_at": _utc_now_iso(),
            "trail_pct": trail_pct,
            "tp_pct": tp_pct,
        }
        bracket_tag = " 🛡️ bracket attached" if tp_id else " ⚠️  UNPROTECTED"
        log.warning(
            f"   ⚠️  ADOPTED ORPHAN {sym}: qty {qty} @ avg ${entry:.2f}{bracket_tag} "
            f"(ADOPT_ORPHAN=1 was set)"
        )
        adopted_count += 1

    if orphan_count > 0:
        log.info(
            f"   Orphan summary: {orphan_count} detected | "
            f"{adopted_count} adopted | {skipped_count} skipped | "
            f"ADOPT_ORPHAN={'ON' if ADOPT_ORPHAN else 'OFF'}"
        )
        if skipped_count > 0 and not ADOPT_ORPHAN:
            notify(
                f"⚠️ [STARTUP] {skipped_count} orphan IBKR position(s) detected "
                f"but NOT adopted. Review manually. Restart with ADOPT_ORPHAN=1 "
                f"to claim."
            )


def install_signal_handlers():
    def handler(signum, frame):
        global _shutdown
        log.info(f"\n🛑 Signal {signum} received — graceful shutdown")
        _shutdown = True
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def print_startup_banner():
    mode = "PAPER" if PAPER_MODE else "⚠️  LIVE"
    log.info(f"🌍 Global RSI Bot v2.1 — [{mode}]")
    log.info(f"   Universe : {len(STOCK_UNIVERSE)} stocks across US/ASX/UK/EU/HK/CA/SG")
    log.info(f"   RSI      : oversold={RSI_OVERSOLD} overbought={RSI_OVERBOUGHT} period=14")
    log.info(f"   Filters  : Vol={'ON' if USE_VOLUME_FILTER else 'OFF'} "
             f"Trend200={'ON' if USE_TREND_FILTER else 'OFF'} "
             f"MA20={'ON' if USE_MA20_FILTER else 'OFF'}")
    log.info(f"   Exits    : Trail={DEFAULT_TRAILING_STOP*100:.0f}% "
             f"TP={DEFAULT_TAKE_PROFIT*100:.0f}% + RSI≥{RSI_OVERBOUGHT}")
    log.info(f"   Brackets : {'✅ server-side OCA' if USE_BRACKET_ORDERS else '❌ polling (UNSAFE)'}")
    log.info(f"   ATR stops: {'ON ('+str(ATR_MULTIPLIER)+'×ATR)' if USE_ATR_STOPS else 'OFF'}")
    log.info(f"   Sizing   : base {POSITION_SIZE_PCT*100:.0f}% × regime"
             f"{' × vol-scalar ['+str(VOL_SCALAR_MIN)+'–'+str(VOL_SCALAR_MAX)+']' if USE_VOL_ADJUSTED_SIZING else ''}"
             f" | target ann.vol {VOL_TARGET_ANNUAL*100:.0f}% | max {MAX_POSITIONS} pos")
    log.info(f"   Regime   : SPY + VIX | BULL={REGIME_SIZE_MULTIPLIERS['BULL']*100:.0f}% "
             f"CAUTION={REGIME_SIZE_MULTIPLIERS['CAUTION']*100:.0f}% "
             f"BEAR={REGIME_SIZE_MULTIPLIERS['BEAR']*100:.0f}%")
    log.info(f"   Loss lim : Daily={DAILY_LOSS_LIMIT_PCT*100:.1f}% "
             f"MaxDD={MAX_DRAWDOWN_PCT*100:.1f}% "
             f"(flatten={FLATTEN_ON_MAX_DD}) reset_tz={DAILY_RESET_TZ}")
    log.info(f"   Scan     : every {SCAN_INTERVAL_SECS // 60}m")
    log.info(f"   Dashboard: {'ON port '+str(DASHBOARD_PORT) if DASHBOARD_ENABLED else 'OFF'}")
    log.info(f"   Orphans  : ADOPT_ORPHAN={'ON — will claim' if ADOPT_ORPHAN else 'OFF — will skip (safe default)'}")
    log.info(f"   History  : {len(trade_history)} trades loaded (cap {TRADE_HISTORY_MAX_SIZE})")


# ══════════════════════════════════════════════════════════════════════════
#  DASHBOARD SNAPSHOT  (H1)
# ══════════════════════════════════════════════════════════════════════════

def _push_dashboard_snapshot(
    nlv: float, cash: float,
    day_pnl_pct: float, day_pnl_dollars: float,
    dd_pct: float,
    regime: str, regime_mult: float,
):
    """
    Build a JSON-serialisable snapshot and push to dashboard state.

    SAFETY (H1): every position's snapshot now carries last_price_ts,
    last_price_age_seconds, and a boolean last_price_stale. Operator
    checking the dashboard during a partial IBKR data outage will see
    immediately which positions have stale prices, rather than acting on
    silently-cached hours-old P&L.
    """
    now_utc = datetime.now(timezone.utc)
    stale_threshold = timedelta(seconds=SCAN_INTERVAL_SECS * 2)  # 2 scan cycles

    positions_out = []
    for sym, pos in bot_positions.items():
        last = _last_prices.get(sym)
        last_ts_str = _last_price_ts.get(sym)
        entry = pos.get("entry", 0.0)
        qty = pos.get("qty", 0)

        is_stale = True
        age_seconds: Optional[float] = None
        if last_ts_str:
            last_ts = _parse_iso_utc(last_ts_str)
            if last_ts is not None:
                age_seconds = (now_utc - last_ts).total_seconds()
                is_stale = (now_utc - last_ts) > stale_threshold

        pnl = None
        pnl_pct = None
        if last is not None and entry:
            pnl = (last - entry) * qty
            pnl_pct = (last - entry) / entry * 100

        positions_out.append({
            "symbol": sym,
            "name": pos.get("name"),
            "exchange": pos.get("exchange"),
            "currency": pos.get("currency"),
            "qty": qty,
            "entry": round(entry, 4),
            "peak": round(pos.get("peak", 0.0), 4),
            "last_price": last,
            "last_price_ts": last_ts_str,
            "last_price_age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
            "last_price_stale": is_stale,
            "pnl": round(pnl, 2) if pnl is not None else None,
            "pnl_pct": round(pnl_pct, 3) if pnl_pct is not None else None,
            "tp_order_id": pos.get("tp_order_id"),
            "trail_order_id": pos.get("trail_order_id"),
            "opened_at": pos.get("opened_at"),
            "has_bracket": bool(pos.get("trail_order_id")),
        })

    try:
        from ibkr_helpers import _manager as _mgr  # type: ignore
        vix_level = _mgr.get_vix_cache()
    except Exception:
        vix_level = None

    wr = compute_winrates()

    update_dashboard_state(
        mode="PAPER" if PAPER_MODE else "LIVE",
        connected=is_connected(),
        nlv=nlv,
        cash=cash,
        day_start_nlv=day_state["start_nlv"],
        peak_nlv=dd_state["peak_nlv"],
        day_pnl_pct=day_pnl_pct,
        day_pnl_dollars=day_pnl_dollars,
        dd_pct=dd_pct,
        regime=regime,
        regime_mult=regime_mult,
        vix=vix_level,
        daily_halted=day_state["hit_daily_limit"],
        max_dd_halted=dd_state["hit_max_dd"],
        positions=positions_out,
        universe_size=len(STOCK_UNIVERSE),
        max_positions=MAX_POSITIONS,
        winrates=wr["winrates"],
        trade_counts=wr["counts"],
        last_update=_utc_now_iso(),
    )


# ══════════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════

def run():
    install_signal_handlers()

    if DASHBOARD_ENABLED:
        try:
            start_dashboard(DASHBOARD_PORT)
        except Exception as e:
            log.warning(f"Could not start dashboard: {e}")

    try:
        get_ib()
    except Exception as e:
        log.critical(f"❌ Could not connect to IBKR: {e}")
        sys.exit(1)

    summary = get_account_summary()
    nlv = summary.get("NetLiquidation", 0)
    cash = summary.get("TotalCashValue", 0)
    log.info(f"✅ Account | NLV: ${nlv:,.2f} | Cash: ${cash:,.2f}")

    load_state()
    print_startup_banner()
    reconcile_existing_positions()

    if day_state["date"] is None:
        day_state["date"] = _today_in_reset_tz()
        day_state["start_nlv"] = nlv
    if dd_state["peak_nlv"] <= 0:
        dd_state["peak_nlv"] = nlv
    save_state()

    mode = "PAPER" if PAPER_MODE else "⚠️  LIVE"

    while not _shutdown:
        try:
            if not is_connected():
                log.warning("Not connected — get_ib() will reconnect")
                get_ib()

            print("\n" + "═" * 80)
            print(f"  🌍 GLOBAL RSI BOT v2.1 [{mode}] — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"  Positions: {len(bot_positions)}/{MAX_POSITIONS}  |  History: {len(trade_history)} trades")
            print("═" * 80)

            summary = get_account_summary()
            nlv = summary.get("NetLiquidation", 0)
            cash = summary.get("TotalCashValue", 0)
            unrealized = summary.get("UnrealizedPnL", 0)
            realized = summary.get("RealizedPnL", 0)

            roll_over_day_if_needed(nlv)

            day_pnl_pct = ((nlv - day_state["start_nlv"]) / day_state["start_nlv"] * 100) if day_state["start_nlv"] else 0
            day_pnl_dollars = nlv - day_state["start_nlv"] if day_state["start_nlv"] else 0
            dd_pct = ((nlv - dd_state["peak_nlv"]) / dd_state["peak_nlv"] * 100) if dd_state["peak_nlv"] else 0

            log.info(f"💼 NLV: ${nlv:,.2f} | Cash: ${cash:,.2f} | "
                     f"Day P&L: {day_pnl_pct:+.2f}% | DD: {dd_pct:+.2f}% "
                     f"(peak ${dd_state['peak_nlv']:,.2f})")
            if unrealized or realized:
                log.info(f"   Unrealized: ${unrealized:+,.2f} | Realized: ${realized:+,.2f}")

            halted_daily = check_daily_loss(nlv)
            halted_dd = check_max_drawdown(nlv)

            try:
                regime, regime_mult = get_market_regime()
            except Exception as e:
                log.warning(f"get_market_regime failed: {e}")
                regime, regime_mult = "UNKNOWN", 1.0

            if bot_positions:
                dash_contracts = {sym: pos["contract"] for sym, pos in bot_positions.items()}
                dash_prices = get_prices_batch(dash_contracts)
                for sym, p in dash_prices.items():
                    _mark_price(sym, p)
                for sym, pos in bot_positions.items():
                    price = dash_prices.get(sym)
                    entry = pos["entry"]
                    qty = pos["qty"]
                    if price:
                        change_pct = (price - entry) / entry * 100
                        pnl = (price - entry) * qty
                        emoji = "🟢" if pnl >= 0 else "🔴"
                        tag = " [bracket]" if pos.get("trail_order_id") else " ⚠️ UNPROTECTED"
                        log.info(f"  {emoji} {sym:<8} qty:{qty} | "
                                 f"${entry:.2f}→${price:.2f} ({change_pct:+.1f}%) | "
                                 f"P&L ${pnl:+.2f}{tag}")
                check_exits()

            if not halted_daily and not halted_dd:
                scan_all_markets()

            _push_dashboard_snapshot(
                nlv=nlv, cash=cash,
                day_pnl_pct=day_pnl_pct, day_pnl_dollars=day_pnl_dollars,
                dd_pct=dd_pct,
                regime=regime, regime_mult=regime_mult,
            )

            save_state()
            print("═" * 80)
            log.info(f"⏳ Next scan in {SCAN_INTERVAL_SECS // 60} min...")

            for _ in range(SCAN_INTERVAL_SECS):
                if _shutdown:
                    break
                time.sleep(1)

        except (TimeoutError, ConnectionError, OSError, asyncio.CancelledError) as e:
            log.warning(f"⚠️  Connection issue ({type(e).__name__}): {e} — reconnecting...")
            try:
                disconnect()
            except Exception:
                pass
            time.sleep(ERROR_RETRY_DELAY)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Unexpected error: {type(e).__name__}: {e} — retrying in {ERROR_RETRY_DELAY}s")
            import traceback
            log.error(traceback.format_exc())
            time.sleep(ERROR_RETRY_DELAY)

    log.info("👋 Shutting down — saving state and disconnecting")
    save_state()
    disconnect()


if __name__ == "__main__":
    run()
