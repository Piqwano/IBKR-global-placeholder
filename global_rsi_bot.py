"""
Global RSI Bot — IBKR (v2)
=============================
Main loop: scan universe → RSI signals → server-side BRACKET orders
(parent market + OCA TP + trailing stop). State persisted to disk.
Daily loss limit + max-DD auto-flatten. Bracket re-attachment on restart.

v2 additions:
  - Volatility-adjusted position sizing (ATR-based vol targeting)
  - VIX-aware market regime (via ibkr_helpers.get_market_regime)
  - Daily P&L summary sent to Discord at each day rollover
  - Embedded FastAPI dashboard (port 8000 by default)
"""

import asyncio
import json
import logging
import math
import os
import signal
import sys
import time
from datetime import datetime, date, timezone
from typing import Dict, Optional
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
    # v2
    USE_VOL_ADJUSTED_SIZING, VOL_TARGET_ANNUAL,
    VOL_SCALAR_MIN, VOL_SCALAR_MAX,
    DASHBOARD_ENABLED, DASHBOARD_PORT,
    SEND_DAILY_SUMMARY,
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

# In-memory cache of last-known prices, used for dashboard + daily summary
# (so we don't need extra IBKR round-trips to compute exposure).
_last_prices: Dict[str, float] = {}

_shutdown = False


# ══════════════════════════════════════════════════════════════════════════
#  TIME HELPERS
# ══════════════════════════════════════════════════════════════════════════

def _today_in_reset_tz() -> date:
    return datetime.now(ZoneInfo(DAILY_RESET_TZ)).date()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════
#  STATE PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════

def save_state():
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
            }
        payload = {
            "bot_positions": serialisable,
            "day_state": {
                "date": day_state["date"].isoformat() if day_state["date"] else None,
                "start_nlv": day_state["start_nlv"],
                "hit_daily_limit": day_state["hit_daily_limit"],
            },
            "dd_state": dd_state,
            "saved_at": _utc_now_iso(),
        }
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.error(f"save_state failed: {e}")


def load_state():
    global day_state, dd_state
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
            }

        ds = payload.get("day_state", {})
        if ds.get("date"):
            day_state["date"] = date.fromisoformat(ds["date"])
        day_state["start_nlv"] = ds.get("start_nlv", 0.0)
        day_state["hit_daily_limit"] = ds.get("hit_daily_limit", False)

        dd = payload.get("dd_state", {})
        dd_state["peak_nlv"] = dd.get("peak_nlv", 0.0)
        dd_state["hit_max_dd"] = dd.get("hit_max_dd", False)

        if dd_state["hit_max_dd"] and RESET_MAX_DD_ON_START:
            log.warning("🟡 RESET_MAX_DD=1 detected — clearing persisted max-DD halt flag")
            dd_state["hit_max_dd"] = False

        log.info(f"📂 Loaded state: {len(bot_positions)} positions | "
                 f"start NLV ${day_state['start_nlv']:,.2f} | "
                 f"peak NLV ${dd_state['peak_nlv']:,.2f} | "
                 f"max_dd_halted={dd_state['hit_max_dd']} | "
                 f"daily_halted={day_state['hit_daily_limit']}")
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

    # Exposure: qty × last-known market price (fallback to entry)
    total_exposure = 0.0
    for sym, pos in bot_positions.items():
        mkt = _last_prices.get(sym) or pos.get("entry", 0.0)
        total_exposure += pos.get("qty", 0) * mkt
    exposure_pct = (total_exposure / current_nlv * 100) if current_nlv > 0 else 0.0

    try:
        regime, _ = get_market_regime()
    except Exception:
        regime = "UNKNOWN"

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
        f"• Regime: {regime}"
    )
    notify(msg)
    log.info(f"📬 Daily summary sent — {date_str} | PnL {day_pnl_pct:+.2f}% | "
             f"Pos {len(bot_positions)} | Regime {regime}")


# ══════════════════════════════════════════════════════════════════════════
#  DAILY / DRAWDOWN CHECKS
# ══════════════════════════════════════════════════════════════════════════

def roll_over_day_if_needed(current_nlv: float):
    today = _today_in_reset_tz()
    if day_state["date"] != today:
        # Send yesterday's summary BEFORE resetting (so day_state still has
        # yesterday's start_nlv + date).
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
                flatten_all_positions(reason=f"max_dd_{dd_pct*100:.1f}pct")
                bot_positions.clear()
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

    # Apply vol-adjusted sizing on top of base × regime
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
    }
    _last_prices[symbol] = result["price"]

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
    trail_pct = cfg.get("trailing_stop", DEFAULT_TRAILING_STOP)
    tp_pct = cfg.get("take_profit", DEFAULT_TAKE_PROFIT)
    tp_id, trail_id = attach_bracket_to_existing_position(
        contract, remaining_qty, entry, trail_pct, tp_pct
    )
    bot_positions[symbol]["qty"] = remaining_qty
    bot_positions[symbol]["tp_order_id"] = tp_id
    bot_positions[symbol]["trail_order_id"] = trail_id


def check_exits():
    ibkr_positions = get_all_positions()

    for symbol in list(bot_positions.keys()):
        if symbol not in ibkr_positions or ibkr_positions[symbol]["qty"] <= 0:
            pos = bot_positions.pop(symbol, None)
            _last_prices.pop(symbol, None)
            if pos:
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
        if p is not None:
            _last_prices[sym] = p

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
                pnl_est = (price - entry) * qty
                emoji = "✅" if pnl_est >= 0 else "❌"
                log.info(f"{emoji} SELL {symbol} — RSI overbought {change_pct:+.1f}% "
                         f"| Entry ${entry:.2f} → ${price:.2f} | Est P&L ${pnl_est:+,.2f}")
                mode = "PAPER" if PAPER_MODE else "LIVE"
                notify(f"{emoji} [{mode}] SELL {symbol} ({pos['name']}) — "
                       f"RSI {analysis['rsi']:.1f} | {change_pct:+.1f}%")

                time.sleep(PARTIAL_SELL_RECONCILE_WAIT)
                try:
                    remaining = get_all_positions().get(symbol)
                except Exception as e:
                    log.warning(f"Post-sell position check failed for {symbol}: {e}")
                    remaining = None

                if not remaining or remaining["qty"] <= 0:
                    bot_positions.pop(symbol, None)
                    _last_prices.pop(symbol, None)
                else:
                    remaining_qty = int(remaining["qty"])
                    _handle_partial_sell_remainder(symbol, pos, remaining_qty)

                if STATE_SAVE_ON_EVERY_FILL:
                    save_state()


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
#  STARTUP
# ══════════════════════════════════════════════════════════════════════════

def reconcile_existing_positions():
    existing = get_all_positions()
    if not existing:
        return
    universe_lookup = {s: (s, e, c, n) for s, e, c, n in STOCK_UNIVERSE}

    for sym, info in existing.items():
        if sym in bot_positions:
            continue

        if sym not in universe_lookup:
            log.info(f"   ⚠️  {sym} not in universe — ignoring (manual trade?)")
            continue

        _, exch, curr, name = universe_lookup[sym]
        qty = int(info["qty"])
        if qty <= 0:
            continue

        contract = get_contract(sym, exch, curr)
        if not contract:
            continue

        entry = info["avg_cost"]
        tp_id = None
        trail_id = None

        if REATTACH_BRACKETS_ON_RECONCILE:
            cfg = ASSET_CONFIG.get(sym, {})
            trail_pct = cfg.get("trailing_stop", DEFAULT_TRAILING_STOP)
            tp_pct = cfg.get("take_profit", DEFAULT_TAKE_PROFIT)
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
        }
        bracket_tag = " 🛡️ bracket attached" if tp_id else " ⚠️  UNPROTECTED"
        log.info(f"   ✅ Claimed {sym}: qty {qty} @ avg ${entry:.2f}{bracket_tag}")


def install_signal_handlers():
    def handler(signum, frame):
        global _shutdown
        log.info(f"\n🛑 Signal {signum} received — graceful shutdown")
        _shutdown = True
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def print_startup_banner():
    mode = "PAPER" if PAPER_MODE else "⚠️  LIVE"
    log.info(f"🌍 Global RSI Bot v2 — [{mode}]")
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


# ══════════════════════════════════════════════════════════════════════════
#  DASHBOARD SNAPSHOT
# ══════════════════════════════════════════════════════════════════════════

def _push_dashboard_snapshot(
    nlv: float, cash: float,
    day_pnl_pct: float, day_pnl_dollars: float,
    dd_pct: float,
    regime: str, regime_mult: float,
):
    """Build a JSON-serialisable snapshot and push to dashboard state."""
    positions_out = []
    for sym, pos in bot_positions.items():
        last = _last_prices.get(sym)
        entry = pos.get("entry", 0.0)
        qty = pos.get("qty", 0)
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
            "pnl": round(pnl, 2) if pnl is not None else None,
            "pnl_pct": round(pnl_pct, 3) if pnl_pct is not None else None,
            "tp_order_id": pos.get("tp_order_id"),
            "trail_order_id": pos.get("trail_order_id"),
            "opened_at": pos.get("opened_at"),
            "has_bracket": bool(pos.get("trail_order_id")),
        })

    # Try to surface the cached VIX without forcing a fresh fetch
    try:
        from ibkr_helpers import _manager as _mgr  # type: ignore
        vix_level = _mgr.get_vix_cache()
    except Exception:
        vix_level = None

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
        last_update=_utc_now_iso(),
    )


# ══════════════════════════════════════════════════════════════════════════
#  MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════

def run():
    install_signal_handlers()
    print_startup_banner()

    # Dashboard starts early so /health is reachable even while IBKR warms up
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
            print(f"  🌍 GLOBAL RSI BOT v2 [{mode}] — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"  Positions: {len(bot_positions)}/{MAX_POSITIONS}")
            print("═" * 80)

            summary = get_account_summary()
            nlv = summary.get("NetLiquidation", 0)
            cash = summary.get("TotalCashValue", 0)
            unrealized = summary.get("UnrealizedPnL", 0)
            realized = summary.get("RealizedPnL", 0)

            # Day rollover — may send yesterday's Discord summary BEFORE reset
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

            # Cache regime (call is cheap — 30 min TTL)
            try:
                regime, regime_mult = get_market_regime()
            except Exception as e:
                log.warning(f"get_market_regime failed: {e}")
                regime, regime_mult = "UNKNOWN", 1.0

            if bot_positions:
                dash_contracts = {sym: pos["contract"] for sym, pos in bot_positions.items()}
                dash_prices = get_prices_batch(dash_contracts)
                for sym, p in dash_prices.items():
                    if p is not None:
                        _last_prices[sym] = p
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

            # Push dashboard snapshot after everything is reconciled
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
