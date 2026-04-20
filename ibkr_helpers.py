"""
IBKR Helpers — Connection, Data, Bracket Orders, Regime, Guards (v2.3)
========================================================================
Thread-safe connection management, event-driven trade waits with partial-fill
handling, bracket orders with server-side TP + trailing stop, market-hours
gating, atomic post-fill TP repair (in-place amend), VIX-aware market
regime with safe degradation, execution lookup for trade-history
reconciliation.

v2.3 fixes (on top of R-round):
  C1: account-values health is now cycle-atomic via collect_account_summary()
  C2: _place_bracket wraps full sequence in rollback try/finally
  C3: _reconcile_post_sell corroborates disagreement with fills before
      preferring arithmetic (prevents phantom-share re-attach)
  C4/M8: has_protective_orders verifies TP+trail composition, not just qty
  H6: OCA groups use UUID suffix to eliminate collisions
  M2: market-hours grace is open-side ZERO, close-side 5 min
  M7: VIX gets its own tight TTL (VIX_CACHE_TTL)
  H2: _wait_for_fill handles PendingCancel explicitly as terminal-dead
  H8: _verify_order_live distinguishes "filled-too-early" from "active"
"""

import asyncio
import logging
import math
import threading
import time as _time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple, List, NamedTuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ib_insync import IB, Stock, Index, Forex, MarketOrder, LimitOrder, Order, Trade, util

from config import (
    IB_HOST, IB_PORT, IB_CLIENT_ID,
    CASH_RESERVE_PCT, MAX_POSITIONS, MAX_PER_CORR_GROUP,
    CORR_GROUPS, BARS_FOR_RSI, RSI_PERIOD, SEHK_LOT_SIZES,
    MARKET_DATA_LIVE, REGIME_CACHE_TTL, VIX_CACHE_TTL,
    MARKET_DATA_SNAPSHOT_WAIT, MARKET_DATA_BATCH_WAIT,
    CONNECTION_TIMEOUT, TRADE_FILL_TIMEOUT,
    RECONNECT_INITIAL_DELAY, RECONNECT_MAX_DELAY,
    RECONNECT_BACKOFF_FACTOR, RECONNECT_MAX_ATTEMPTS,
    EXCHANGE_SESSIONS, MARKET_HOURS_GRACE_MINS_OPEN, MARKET_HOURS_GRACE_MINS_CLOSE,
    ATR_PERIOD, ATR_PERIOD_FOR_SIZING,
    USE_BRACKET_ORDERS, REPAIR_TP_AFTER_FILL,
    DEFAULT_TRAILING_STOP, DEFAULT_TAKE_PROFIT,
    USE_VIX_IN_REGIME,
    VIX_BULL_MAX, VIX_CAUTION_MAX_ABOVE_200MA, VIX_CAUTION_MAX_BELOW_200MA,
    ORDER_PLACEMENT_VERIFY_SECS,
    FX_CACHE_TTL, FX_SNAPSHOT_WAIT,
)

log = logging.getLogger("ibkr-rsi")

def _round_to_tick(price: float, exchange: str) -> float:
    """
    H-10: Round a limit price to the minimum tick size for the exchange.
    IBKR rejects orders (Warning 110 → silent parent cancellation) when
    the price doesn't conform to the venue's minimum price variation.
    """
    if exchange == "LSE":
        # LSE prices in GBX: 0.5 GBX below 5000, 1.0 GBX above.
        tick = 1.0 if price >= 5000 else 0.5
    elif exchange in ("ASX", "SEHK", "SGX", "IBIS", "SBF", "AEB"):
        tick = 0.01
    else:
        tick = 0.01
    return round(round(price / tick) * tick, 4)


# ══════════════════════════════════════════════════════════════════════════
#  NUMERIC COERCION HELPERS  (L-8)
# ══════════════════════════════════════════════════════════════════════════

def sanitise_float(val, default: float = 0.0) -> float:
    """Coerce to float; on NaN/Inf/parse-failure return `default`."""
    try:
        f = float(val)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def sanitise_price(val) -> Optional[float]:
    """Strict positive-price coercion: None on bad/zero/negative/NaN/Inf."""
    if val is None:
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f) or f <= 0:
        return None
    return f


# ══════════════════════════════════════════════════════════════════════════
#  ACCOUNT-VALUE HEALTH FLAG  (R1 + C1 CYCLE-ATOMIC FIX)
# ══════════════════════════════════════════════════════════════════════════
# v2.3 C1: Previously _resolve_account_value was called per-tag and every
# clean tag overrode the flag to True — meaning a degraded NLV followed by
# a clean TotalCashValue in the same accountSummary cycle would leave the
# flag True. We now do atomic per-cycle resolution:
#   - _resolve_account_value_raw(...) returns (value, healthy_bool, reason)
#     and does NOT touch the module flag.
#   - collect_account_summary(...) does all the resolutions in one pass
#     then calls _set_account_health(final_healthy, combined_reason) once.
# Legacy _resolve_account_value kept as a thin wrapper for callers that
# still need a single value (none remain, but defensive).

_account_values_healthy: bool = True
_account_values_health_reason: str = ""
_account_values_lock = threading.Lock()


def account_values_healthy() -> Tuple[bool, str]:
    with _account_values_lock:
        return _account_values_healthy, _account_values_health_reason


def _set_account_health(healthy: bool, reason: str = ""):
    global _account_values_healthy, _account_values_health_reason
    with _account_values_lock:
        prev = _account_values_healthy
        _account_values_healthy = healthy
        _account_values_health_reason = reason
        if prev != healthy:
            if healthy:
                log.info(f"✅ Account-values health restored: {reason or 'clean BASE resolution'}")
            else:
                log.error(f"🚫 Account-values health DEGRADED — new buys blocked until resolved: {reason}")


class _ResolutionResult(NamedTuple):
    value: float
    healthy: bool
    reason: str


def _resolve_account_value_raw(account_values, tag: str,
                               allowed_currencies=("USD", "AUD", "CAD", "SGD", "GBP", "EUR")
                               ) -> _ResolutionResult:
    """
    C1: Pure function. Returns (value, healthy, reason).
    Does NOT touch the module health flag — that's the cycle-level caller's
    job so one degraded tag can poison the whole cycle atomically.
    """
    # Pass 1 — BASE is authoritative
    for item in account_values:
        if item.tag == tag and item.currency == "BASE":
            try:
                val = float(item.value)
                return _ResolutionResult(val, True, f"{tag} BASE clean")
            except (TypeError, ValueError):
                return _ResolutionResult(
                    0.0, False,
                    f"malformed BASE value for {tag}: {item.value!r}"
                )

    # Pass 2 — no BASE, fall back
    for item in account_values:
        if item.tag == tag and item.currency in allowed_currencies:
            try:
                val = float(item.value)
                reason = (f"no BASE row for {tag} — fell back to "
                          f"{item.currency} {val}")
                log.warning(f"⚠️  {reason}. Verify IBKR account config.")
                return _ResolutionResult(val, True, reason)
            except (TypeError, ValueError):
                continue

    return _ResolutionResult(
        0.0, False, f"could not resolve {tag} from accountSummary"
    )


def _resolve_account_value(account_values, tag: str,
                           allowed_currencies=("USD", "AUD", "CAD", "SGD", "GBP", "EUR")) -> float:
    """
    Backwards-compat shim. Callers should prefer collect_account_summary
    for cycle-atomic health. This single-tag version does NOT update the
    module flag (that was the C1 bug); the caller is responsible.
    """
    result = _resolve_account_value_raw(account_values, tag, allowed_currencies)
    if not result.healthy:
        log.error(f"{result.reason} — returning value {result.value} (health untouched)")
    return result.value


def collect_account_summary(tags=("NetLiquidation", "TotalCashValue", "BuyingPower",
                                   "UnrealizedPnL", "RealizedPnL")) -> Dict[str, float]:
    """
    C1: Cycle-atomic account-values resolution.

    Resolves every requested tag, aggregates health, and sets the module-level
    flag ONCE based on whether ALL tags resolved cleanly via BASE. Any single
    degraded tag marks the whole cycle as degraded — try_buy will block.
    """
    ib = get_ib()
    try:
        account_values = ib.accountSummary()
    except Exception as e:
        _set_account_health(False, f"accountSummary call failed: {e}")
        return {}

    present_tags = {item.tag for item in account_values}
    result: Dict[str, float] = {}
    cycle_healthy = True
    failure_reasons: List[str] = []

    for tag in tags:
        if tag not in present_tags:
            # Tag absent — not necessarily a health issue (e.g., RealizedPnL
            # may be absent on fresh accounts). Skip but don't penalise.
            continue
        res = _resolve_account_value_raw(account_values, tag)
        result[tag] = res.value
        if not res.healthy:
            cycle_healthy = False
            failure_reasons.append(res.reason)

    if cycle_healthy:
        _set_account_health(True, "all tags BASE-resolved cleanly")
    else:
        _set_account_health(False, "; ".join(failure_reasons))

    return result


# ══════════════════════════════════════════════════════════════════════════
#  CONNECTION MANAGER
# ══════════════════════════════════════════════════════════════════════════

class IBConnectionManager:
    """Singleton IB connection. Thread-safe, auto-reconnecting."""

    def __init__(self):
        self._ib: Optional[IB] = None
        self._lock = threading.RLock()
        self._contract_cache: Dict[str, Stock] = {}
        self._regime_cache: Optional[Tuple[str, float]] = None
        self._regime_time: float = 0.0
        self._vix_cache: Optional[Tuple[float, float]] = None
        # FX rate cache: (from_ccy, to_ccy) -> (rate, time)
        self._fx_cache: Dict[Tuple[str, str], Tuple[float, float]] = {}
        # Account base currency — resolved once per session from accountSummary
        self._base_currency: Optional[str] = None
        self._disconnect_handler_registered = False

    def _ensure_loop(self):
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())

    def get(self) -> IB:
        with self._lock:
            if self._ib is not None and self._ib.isConnected():
                return self._ib
            self._connect_with_retries()
            return self._ib

    def _connect_with_retries(self):
        attempt = 0
        delay = RECONNECT_INITIAL_DELAY

        while True:
            attempt += 1
            try:
                self._ensure_loop()
                if self._ib is None:
                    self._ib = IB()
                    self._register_event_handlers()

                log.info(f"🔌 Connecting to IBKR {IB_HOST}:{IB_PORT} (attempt {attempt})...")
                self._ib.connect(
                    IB_HOST, IB_PORT,
                    clientId=IB_CLIENT_ID,
                    readonly=False,
                    timeout=CONNECTION_TIMEOUT,
                )

                if MARKET_DATA_LIVE:
                    self._ib.reqMarketDataType(1)
                    data_label = "LIVE"
                else:
                    self._ib.reqMarketDataType(3)
                    data_label = "DELAYED (15-20 min lag)"

                accounts = self._ib.managedAccounts()
                log.info(f"✅ Connected | Accounts: {accounts} | Market data: {data_label}")

                # R12: clear ALL caches that could carry stale state across
                # a reconnect. VIX uses its own TTL now (M7) but the cache
                # itself is reset here to avoid cross-session staleness.
                self._contract_cache.clear()
                self._regime_cache = None
                self._regime_time = 0.0
                self._vix_cache = None
                self._fx_cache.clear()
                self._base_currency = None
                log.info("   Caches cleared: contracts, regime, VIX, FX, base-ccy")
                return

            except Exception as e:
                log.error(f"❌ Connection attempt {attempt} failed: {type(e).__name__}: {e}")
                if RECONNECT_MAX_ATTEMPTS and attempt >= RECONNECT_MAX_ATTEMPTS:
                    log.critical(f"Max reconnect attempts ({RECONNECT_MAX_ATTEMPTS}) reached")
                    raise
                log.info(f"⏳ Retrying in {delay:.0f}s...")
                _time.sleep(delay)
                delay = min(delay * RECONNECT_BACKOFF_FACTOR, RECONNECT_MAX_DELAY)

    def _register_event_handlers(self):
        if self._disconnect_handler_registered or self._ib is None:
            return
        self._ib.disconnectedEvent += self._on_disconnect
        self._ib.connectedEvent += self._on_connect
        self._disconnect_handler_registered = True

    def _on_disconnect(self):
        log.warning("⚠️  IBKR disconnected — will reconnect on next API call")

    def _on_connect(self):
        log.info("🔗 IBKR connection established")

    def is_connected(self) -> bool:
        with self._lock:
            return self._ib is not None and self._ib.isConnected()

    def disconnect(self):
        with self._lock:
            if self._ib and self._ib.isConnected():
                self._ib.disconnect()
                log.info("Disconnected from IBKR")

    def get_cached_contract(self, key: str) -> Optional[Stock]:
        with self._lock:
            return self._contract_cache.get(key)

    def cache_contract(self, key: str, contract: Stock):
        with self._lock:
            self._contract_cache[key] = contract

    def get_regime_cache(self) -> Optional[Tuple[str, float]]:
        with self._lock:
            if self._regime_cache and (_time.time() - self._regime_time) < REGIME_CACHE_TTL:
                return self._regime_cache
            return None

    def set_regime_cache(self, value: Tuple[str, float]):
        with self._lock:
            self._regime_cache = value
            self._regime_time = _time.time()

    def get_vix_cache(self) -> Optional[float]:
        """M7: VIX has its own TTL (VIX_CACHE_TTL, default 120s) because
        during fast markets VIX moves far faster than SPY-200MA regime."""
        with self._lock:
            if self._vix_cache and (_time.time() - self._vix_cache[1]) < VIX_CACHE_TTL:
                return self._vix_cache[0]
            return None

    def set_vix_cache(self, level: float):
        with self._lock:
            self._vix_cache = (level, _time.time())

    def get_fx_cache(self, from_ccy: str, to_ccy: str) -> Optional[float]:
        with self._lock:
            hit = self._fx_cache.get((from_ccy, to_ccy))
            if hit and (_time.time() - hit[1]) < FX_CACHE_TTL:
                return hit[0]
            return None

    def set_fx_cache(self, from_ccy: str, to_ccy: str, rate: float):
        with self._lock:
            self._fx_cache[(from_ccy, to_ccy)] = (rate, _time.time())

    def get_base_currency_cached(self) -> Optional[str]:
        with self._lock:
            return self._base_currency

    def set_base_currency(self, ccy: str):
        with self._lock:
            self._base_currency = ccy


_manager = IBConnectionManager()


def get_ib() -> IB:
    return _manager.get()


def disconnect():
    _manager.disconnect()


def is_connected() -> bool:
    return _manager.is_connected()


# ══════════════════════════════════════════════════════════════════════════
#  MARKET HOURS  (M2)
# ══════════════════════════════════════════════════════════════════════════

def is_market_open(exchange: str, now: Optional[datetime] = None) -> Tuple[bool, str]:
    """
    M2: grace split — ZERO grace on open-side (no pre-market buys into thin
    liquidity / no live price), 5-min grace on close-side (lets TP/trail
    fill right at the bell).
    """
    sess = EXCHANGE_SESSIONS.get(exchange)
    if not sess:
        return True, "unknown exchange — deferring to IBKR"

    tz = ZoneInfo(sess["tz"])
    now = (now or datetime.now(tz=ZoneInfo("UTC"))).astimezone(tz)

    if now.weekday() not in sess["days"]:
        return False, f"{exchange} closed (weekend, local {now.strftime('%a %H:%M')})"

    open_grace = timedelta(minutes=MARKET_HOURS_GRACE_MINS_OPEN)
    close_grace = timedelta(minutes=MARKET_HOURS_GRACE_MINS_CLOSE)
    open_dt = now.replace(hour=sess["open"].hour, minute=sess["open"].minute,
                          second=0, microsecond=0) - open_grace
    close_dt = now.replace(hour=sess["close"].hour, minute=sess["close"].minute,
                           second=0, microsecond=0) + close_grace

    if not (open_dt <= now <= close_dt):
        return False, f"{exchange} closed (local {now.strftime('%H:%M')}, session {sess['open']}-{sess['close']})"

    lunch = sess.get("lunch")
    if lunch:
        lunch_start, lunch_end = lunch
        ls = now.replace(hour=lunch_start.hour, minute=lunch_start.minute, second=0, microsecond=0)
        le = now.replace(hour=lunch_end.hour, minute=lunch_end.minute, second=0, microsecond=0)
        if ls <= now < le:
            return False, f"{exchange} lunch break (local {now.strftime('%H:%M')})"

    return True, f"{exchange} open (local {now.strftime('%H:%M')})"


# ══════════════════════════════════════════════════════════════════════════
#  CONTRACT BUILDERS
# ══════════════════════════════════════════════════════════════════════════

def make_stock(symbol: str, exchange: str, currency: str) -> Optional[Stock]:
    ib = get_ib()
    # Use SMART routing for non-US stocks to avoid IBKR Error 10311
    # (direct-routing precautionary warning). The originally-specified
    # exchange becomes the primaryExchange so IBKR routes correctly.
    if exchange == "SMART":
        contract = Stock(symbol, "SMART", currency)
    else:
        contract = Stock(symbol, "SMART", currency, primaryExchange=exchange)
    try:
        qualified = ib.qualifyContracts(contract)
    except Exception as e:
        log.warning(f"Qualify failed {symbol}/{exchange}: {e}")
        return None
    if not qualified:
        log.warning(f"Could not qualify {symbol} on {exchange}")
        return None
    return qualified[0]


def get_contract(symbol: str, exchange: str, currency: str) -> Optional[Stock]:
    key = f"{symbol}:{exchange}:{currency}"
    cached = _manager.get_cached_contract(key)
    if cached is not None:
        return cached
    contract = make_stock(symbol, exchange, currency)
    if contract:
        _manager.cache_contract(key, contract)
    return contract


# ══════════════════════════════════════════════════════════════════════════
#  HISTORICAL DATA
# ══════════════════════════════════════════════════════════════════════════

def get_daily_bars(contract, days: int = BARS_FOR_RSI) -> Optional[pd.DataFrame]:
    ib = get_ib()
    try:
        bars = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=f"{days + 10} D",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
        if not bars:
            return None
        df = util.df(bars)
        if df is None or len(df) == 0:
            return None
        return df
    except Exception as e:
        log.warning(f"Bars error {contract.symbol}/{getattr(contract,'exchange','?')}: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════
#  LIVE PRICES
# ══════════════════════════════════════════════════════════════════════════

def _valid_price(val) -> bool:
    return val is not None and not math.isnan(val) and val > 0


def _extract_price(ticker) -> Optional[float]:
    for attr in ("last", "close"):
        v = getattr(ticker, attr, None)
        if _valid_price(v):
            return v
    mp = ticker.marketPrice()
    if _valid_price(mp):
        return mp
    for attr in ("delayedLast", "delayedClose"):
        v = getattr(ticker, attr, None)
        if _valid_price(v):
            return v
    return None


def get_current_price(contract: Stock) -> Optional[float]:
    """
    M-1: On delayed data (free tier), MARKET_DATA_SNAPSHOT_WAIT=3s is often
    not enough for the snapshot to populate. A silent None here causes the
    buy signal to be dropped entirely. We now retry the wait once before
    giving up.
    """
    ib = get_ib()
    ticker = None
    try:
        ticker = ib.reqMktData(contract, "", True, False)
        ib.sleep(MARKET_DATA_SNAPSHOT_WAIT)
        price = _extract_price(ticker)
        if price is None:
            # One extra wait window — delayed snapshots often need it
            ib.sleep(MARKET_DATA_SNAPSHOT_WAIT)
            price = _extract_price(ticker)
        return round(price, 4) if price else None
    except Exception as e:
        log.warning(f"Price error {contract.symbol}: {e}")
        return None
    finally:
        # L7: always cancel the subscription, even on exception
        if ticker is not None:
            try:
                ib.cancelMktData(contract)
            except Exception:
                pass


def get_prices_batch(contracts: Dict[str, Stock]) -> Dict[str, Optional[float]]:
    if not contracts:
        return {}
    ib = get_ib()
    tickers = {}
    for symbol, contract in contracts.items():
        try:
            tickers[symbol] = ib.reqMktData(contract, "", True, False)
        except Exception as e:
            log.warning(f"Batch price request failed for {symbol}: {e}")

    ib.sleep(MARKET_DATA_BATCH_WAIT)

    prices: Dict[str, Optional[float]] = {}
    for symbol, ticker in tickers.items():
        p = _extract_price(ticker)
        prices[symbol] = round(p, 4) if p else None

    # Always cancel every subscription we started (L7)
    for symbol, contract in contracts.items():
        if symbol not in tickers:
            continue
        try:
            ib.cancelMktData(contract)
        except Exception:
            pass

    fetched = sum(1 for p in prices.values() if p is not None)
    log.info(f"  📊 Batch price fetch: {fetched}/{len(contracts)} prices")
    return prices


# ══════════════════════════════════════════════════════════════════════════
#  INDICATORS
# ══════════════════════════════════════════════════════════════════════════

def calc_rsi(closes: np.ndarray, period: int = RSI_PERIOD) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)


def calc_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray,
             period: int = ATR_PERIOD) -> Optional[float]:
    if len(close) < period + 1:
        return None
    tr = np.maximum(high[1:] - low[1:],
         np.maximum(np.abs(high[1:] - close[:-1]),
                    np.abs(low[1:] - close[:-1])))
    atr = np.mean(tr[:period])
    for i in range(period, len(tr)):
        atr = (atr * (period - 1) + tr[i]) / period
    return round(float(atr), 4)


def analyze(contract) -> Optional[dict]:
    df = get_daily_bars(contract)
    if df is None or len(df) < RSI_PERIOD + 5:
        return None

    closes = df["close"].values.astype(float)
    highs = df["high"].values.astype(float)
    lows = df["low"].values.astype(float)
    volumes = df["volume"].values.astype(float)
    price = round(closes[-1], 4)

    rsi = calc_rsi(closes)
    if rsi is None:
        return None

    avg_vol = float(np.mean(volumes[-21:-1])) if len(volumes) >= 21 else float(np.mean(volumes[:-1]))
    vol_ok = float(volumes[-1]) > avg_vol if avg_vol > 0 else True

    ma20 = float(np.mean(closes[-20:])) if len(closes) >= 20 else float(np.mean(closes))
    ma50 = float(np.mean(closes[-50:])) if len(closes) >= 50 else float(np.mean(closes))
    ma200 = float(np.mean(closes[-200:])) if len(closes) >= 200 else float(np.mean(closes))
    trend_ok = price > ma200
    ma20_ok = price > ma20

    atr = calc_atr(highs, lows, closes, period=ATR_PERIOD) if len(closes) > ATR_PERIOD else None
    atr_sizing = (calc_atr(highs, lows, closes, period=ATR_PERIOD_FOR_SIZING)
                  if len(closes) > ATR_PERIOD_FOR_SIZING else None)

    return {
        "rsi": rsi, "price": price,
        "vol_ok": vol_ok, "trend_ok": trend_ok, "ma20_ok": ma20_ok,
        "ma20": round(ma20, 4), "ma50": round(ma50, 4), "ma200": round(ma200, 4),
        "avg_volume": avg_vol,
        "atr": atr,
        "atr_sizing": atr_sizing,
    }


# ══════════════════════════════════════════════════════════════════════════
#  VIX
# ══════════════════════════════════════════════════════════════════════════

def get_vix_level() -> Optional[float]:
    cached = _manager.get_vix_cache()
    if cached is not None:
        return cached

    ib = get_ib()
    try:
        vix = Index("VIX", "CBOE", "USD")
        qualified = ib.qualifyContracts(vix)
        if not qualified:
            log.warning("Could not qualify VIX contract (CBOE)")
            return None
        bars = ib.reqHistoricalData(
            qualified[0],
            endDateTime="",
            durationStr="5 D",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
        if not bars:
            log.warning("VIX historical data returned empty")
            return None
        level = round(float(bars[-1].close), 2)
        _manager.set_vix_cache(level)
        return level
    except Exception as e:
        log.warning(f"VIX fetch failed: {e} — regime will degrade per B4 policy")
        return None


# ══════════════════════════════════════════════════════════════════════════
#  FX — ACCOUNT BASE CURRENCY + RATES (C-1)
# ══════════════════════════════════════════════════════════════════════════
# Position sizing is expressed as a percent of NLV. NLV is denominated in the
# account BASE currency (typically USD). Without FX conversion, sizing an AUD
# stock against a USD NLV budget mis-sizes by the FX ratio. These helpers
# resolve the base currency once per session and the pair rates with a short
# TTL.

_FX_STALE_WARN_LEVEL = 0.0  # last warn-price guard (unused; placeholder)


def get_account_base_currency() -> str:
    """
    Resolve account base currency. IBKR's accountSummary returns each tag
    with `currency="BASE"` plus a matching row with the real currency code.
    We compare values across the two rows: whichever non-BASE currency has
    the same value as BASE is the account's base currency.

    Cached for the session. Falls back to "USD" if resolution fails.
    """
    cached = _manager.get_base_currency_cached()
    if cached:
        return cached

    ib = get_ib()
    try:
        vals = ib.accountSummary()
    except Exception as e:
        log.warning(f"get_account_base_currency: accountSummary failed ({e}) — defaulting USD")
        _manager.set_base_currency("USD")
        return "USD"

    # Gather NLV rows
    base_val: Optional[float] = None
    candidates: Dict[str, float] = {}
    for item in vals:
        if item.tag != "NetLiquidation":
            continue
        try:
            v = float(item.value)
        except (TypeError, ValueError):
            continue
        if item.currency == "BASE":
            base_val = v
        else:
            candidates[item.currency] = v

    if base_val is None or not candidates:
        log.warning("get_account_base_currency: no BASE/currency pair in NLV — defaulting USD")
        _manager.set_base_currency("USD")
        return "USD"

    # Pick the currency whose value matches BASE (within rounding)
    best_ccy = None
    best_diff = float("inf")
    for ccy, v in candidates.items():
        diff = abs(v - base_val)
        if diff < best_diff:
            best_diff = diff
            best_ccy = ccy

    # Accept if within 0.5% of BASE
    if best_ccy and base_val > 0 and best_diff / base_val < 0.005:
        log.info(f"💱 Account base currency detected: {best_ccy}")
        _manager.set_base_currency(best_ccy)
        return best_ccy

    log.warning(
        f"get_account_base_currency: no row matched BASE NLV={base_val} "
        f"(candidates={candidates}) — defaulting USD"
    )
    _manager.set_base_currency("USD")
    return "USD"


def _fetch_fx_rate_once(from_ccy: str, to_ccy: str) -> Optional[float]:
    """One-shot fetch of an FX rate via a Forex contract snapshot."""
    ib = get_ib()
    # ib_insync Forex pairs are expressed as BASEQUOTE (e.g. AUDUSD = how
    # many USD per 1 AUD). We try BASEQUOTE first, then invert.
    pair_symbol = f"{from_ccy}{to_ccy}"
    qualified_contract = None
    ticker = None
    try:
        pair = Forex(pair_symbol)
        qualified = ib.qualifyContracts(pair)
        if not qualified:
            return None
        qualified_contract = qualified[0]
        ticker = ib.reqMktData(qualified_contract, "", True, False)
        ib.sleep(FX_SNAPSHOT_WAIT)
        price: Optional[float] = None
        for attr in ("last", "close", "marketPrice"):
            v = getattr(ticker, attr, None)
            if callable(v):
                try:
                    v = v()
                except Exception:
                    v = None
            if _valid_price(v):
                price = float(v)
                break
        if price is None:
            bid = getattr(ticker, "bid", None)
            ask = getattr(ticker, "ask", None)
            if _valid_price(bid) and _valid_price(ask):
                price = (float(bid) + float(ask)) / 2
        return price
    except Exception as e:
        log.warning(f"FX fetch {pair_symbol} failed: {e}")
        return None
    finally:
        if qualified_contract is not None:
            try:
                ib.cancelMktData(qualified_contract)
            except Exception:
                pass


def get_fx_rate(from_ccy: str, to_ccy: str) -> Optional[float]:
    """
    Return the FX rate converting 1 unit of from_ccy into to_ccy.
    Cached with FX_CACHE_TTL. Returns None if the pair can't be resolved —
    caller must treat None as "cannot size this trade safely".
    """
    if not from_ccy or not to_ccy:
        return None
    if from_ccy == to_ccy:
        return 1.0

    hit = _manager.get_fx_cache(from_ccy, to_ccy)
    if hit is not None:
        return hit

    # Try direct
    direct = _fetch_fx_rate_once(from_ccy, to_ccy)
    if direct is not None and direct > 0:
        _manager.set_fx_cache(from_ccy, to_ccy, direct)
        # Also cache inverse for free
        _manager.set_fx_cache(to_ccy, from_ccy, 1.0 / direct)
        return direct

    # Try inverse
    inverse = _fetch_fx_rate_once(to_ccy, from_ccy)
    if inverse is not None and inverse > 0:
        rate = 1.0 / inverse
        _manager.set_fx_cache(from_ccy, to_ccy, rate)
        _manager.set_fx_cache(to_ccy, from_ccy, inverse)
        return rate

    log.warning(f"FX rate {from_ccy}->{to_ccy} unresolved")
    return None


def convert_to_base(amount: float, from_ccy: str) -> Optional[float]:
    """Convert `amount` in from_ccy to the account base currency."""
    base = get_account_base_currency()
    rate = get_fx_rate(from_ccy, base)
    if rate is None:
        return None
    return amount * rate


def convert_from_base(amount_base: float, to_ccy: str) -> Optional[float]:
    """Convert `amount_base` in account base currency to to_ccy."""
    base = get_account_base_currency()
    rate = get_fx_rate(base, to_ccy)
    if rate is None:
        return None
    return amount_base * rate


# ══════════════════════════════════════════════════════════════════════════
#  MARKET REGIME (SPY + VIX)
# ══════════════════════════════════════════════════════════════════════════

def get_market_regime() -> Tuple[str, float]:
    from config import REGIME_SIZE_MULTIPLIERS

    cached = _manager.get_regime_cache()
    if cached:
        return cached

    spy = get_contract("SPY", "SMART", "USD")
    if not spy:
        log.warning("Could not get SPY for regime check — defaulting CAUTION")
        default = ("CAUTION", REGIME_SIZE_MULTIPLIERS["CAUTION"])
        _manager.set_regime_cache(default)
        return default

    analysis = analyze(spy)
    if not analysis:
        default = ("CAUTION", REGIME_SIZE_MULTIPLIERS["CAUTION"])
        _manager.set_regime_cache(default)
        return default

    price = analysis["price"]
    ma50 = analysis["ma50"]
    ma200 = analysis["ma200"]
    above_200 = price > ma200

    vix = get_vix_level() if USE_VIX_IN_REGIME else None

    if vix is not None:
        if above_200 and vix < VIX_BULL_MAX:
            regime = "BULL"
        elif above_200 and vix <= VIX_CAUTION_MAX_ABOVE_200MA:
            regime = "CAUTION"
        elif above_200:
            regime = "CAUTION"
        elif vix < VIX_CAUTION_MAX_BELOW_200MA:
            regime = "CAUTION"
        else:
            regime = "BEAR"
        extra = f" | VIX {vix:.2f}"
    elif USE_VIX_IN_REGIME:
        regime = "CAUTION" if above_200 else "BEAR"
        extra = " | VIX N/A (degraded: capped at CAUTION/BEAR)"
        log.warning(
            f"⚠️  VIX unavailable with USE_VIX_IN_REGIME=True — "
            f"regime capped at {regime} (no BULL allowed without VIX confirmation)"
        )
    else:
        if above_200 and price > ma50:
            regime = "BULL"
        elif above_200:
            regime = "CAUTION"
        else:
            regime = "BEAR"
        extra = " | VIX disabled (SPY-only)"

    mult = REGIME_SIZE_MULTIPLIERS[regime]
    emoji = {"BULL": "🟢", "CAUTION": "🟡", "BEAR": "🔴"}[regime]
    log.info(
        f"{emoji} Regime: {regime} | SPY ${price:.2f} "
        f"(50MA ${ma50:.2f}, 200MA ${ma200:.2f}){extra}"
    )

    result = (regime, mult)
    _manager.set_regime_cache(result)
    return result


# ══════════════════════════════════════════════════════════════════════════
#  GUARDS  (B1 + R1 + C1)
# ══════════════════════════════════════════════════════════════════════════

def cash_guard_check() -> Tuple[bool, float, float]:
    """
    C1: Uses collect_account_summary for atomic health update.
    If health is degraded, blocks the buy and returns (False, 0, 0).
    """
    try:
        values = collect_account_summary(("TotalCashValue", "NetLiquidation"))

        healthy, reason = account_values_healthy()
        if not healthy:
            log.info(f"  🚫 Cash guard: account-values degraded ({reason}) — blocking buy")
            return False, 0.0, 0.0

        cash = values.get("TotalCashValue", 0.0)
        portfolio = values.get("NetLiquidation", 0.0)

        if portfolio <= 0:
            return False, 0.0, 0.0

        cash_pct = cash / portfolio
        if cash_pct < CASH_RESERVE_PCT:
            log.info(f"  💰 Cash guard: ${cash:,.2f} ({cash_pct*100:.1f}%) below "
                     f"{CASH_RESERVE_PCT*100:.0f}% reserve — skipping buy")
            return False, cash, portfolio
        return True, cash, portfolio
    except Exception as e:
        log.warning(f"Cash guard error: {e} — blocking buy (conservative)")
        _set_account_health(False, f"cash_guard_check exception: {e}")
        return False, 0.0, 0.0


def correlation_check(symbol: str, current_positions: dict) -> Tuple[bool, str]:
    held_symbols = set(current_positions.keys())
    if len(held_symbols) >= MAX_POSITIONS:
        return False, f"Max {MAX_POSITIONS} positions reached"
    if symbol in held_symbols:
        return False, f"Already holding {symbol}"
    for group_name, group_symbols in CORR_GROUPS.items():
        if symbol not in group_symbols:
            continue
        held_in_group = sum(1 for s in held_symbols if s in group_symbols)
        if held_in_group >= MAX_PER_CORR_GROUP:
            return False, f"Already {held_in_group} in '{group_name}' (max {MAX_PER_CORR_GROUP})"
    return True, "ok"


# ══════════════════════════════════════════════════════════════════════════
#  ORDER HELPERS
# ══════════════════════════════════════════════════════════════════════════

# R9: order statuses that indicate the order is live and working
_ACTIVE_ORDER_STATUSES = {"Submitted", "PreSubmitted", "Accepted", "ApiPending"}
# H2: statuses that indicate terminal non-fill (including PendingCancel
# which was previously treated as "keep waiting")
_TERMINAL_DEAD_STATUSES = {"Cancelled", "ApiCancelled", "Inactive", "PendingCancel"}


def _calc_quantity(contract: Stock, amount: float, price: float) -> Optional[int]:
    if contract.exchange == "SGX":
        shares = int(amount / price)
        qty = (shares // 100) * 100
        if qty < 100:
            log.warning(f"Cannot afford 100-lot of {contract.symbol} @ ${price:.2f}")
            return None
        return qty
    if contract.exchange == "SEHK":
        lot = SEHK_LOT_SIZES.get(contract.symbol, 100)
        shares = int(amount / price)
        qty = (shares // lot) * lot
        if qty < lot:
            log.warning(f"Cannot afford {lot}-lot of {contract.symbol} @ ${price:.2f}")
            return None
        return qty
    qty = int(amount / price)
    if qty < 1:
        log.warning(f"Cannot afford 1 share of {contract.symbol} @ ${price:.2f}")
        return None
    return qty


def _wait_for_fill(trade: Trade, timeout: float = TRADE_FILL_TIMEOUT) -> Tuple[bool, int]:
    """
    H2: terminal-dead statuses now include PendingCancel. Previously
    PendingCancel was treated as "keep waiting", which could cause the
    caller to time out and assume rejection — while the order silently
    filled on the next broker update.
    """
    ib = get_ib()
    deadline = _time.time() + timeout
    total_qty = int(trade.order.totalQuantity)

    while _time.time() < deadline:
        status = trade.orderStatus.status
        filled_qty = int(trade.orderStatus.filled or 0)

        if status == "Filled" or filled_qty >= total_qty:
            return True, filled_qty

        if status in _TERMINAL_DEAD_STATUSES and filled_qty == 0:
            return False, 0

        ib.waitOnUpdate(timeout=1.0)

    filled_qty = int(trade.orderStatus.filled or 0)
    if filled_qty > 0:
        log.warning(
            f"⚠️  Trade {trade.order.orderId} PARTIAL fill at timeout: "
            f"{filled_qty}/{total_qty} (status={trade.orderStatus.status})"
        )
    return False, filled_qty


def _safe_cancel(trade_or_order):
    ib = get_ib()
    try:
        order = trade_or_order.order if hasattr(trade_or_order, 'order') else trade_or_order
        ib.cancelOrder(order)
    except Exception:
        pass


def _oca_group_name(symbol: str, purpose: str = "") -> str:
    """H6: UUID-based OCA group naming eliminates collision risk."""
    suffix = uuid.uuid4().hex[:8]
    prefix = f"OCA_{symbol}"
    if purpose:
        prefix = f"{prefix}_{purpose}"
    return f"{prefix}_{suffix}"


# ══════════════════════════════════════════════════════════════════════════
#  ORDER VERIFICATION  (R9 + H8)
# ══════════════════════════════════════════════════════════════════════════

def _verify_order_live(trade: Trade, timeout: float = ORDER_PLACEMENT_VERIFY_SECS,
                       label: str = "",
                       allow_filled: bool = True) -> bool:
    """
    R9: Poll orderStatus for up to `timeout` seconds to verify a freshly-
    placed or amended order actually became active.

    H8: `allow_filled` controls whether a 'Filled' status counts as success.
    For BUY parents, yes. For child TP/trail orders freshly placed, a
    'Filled' during the verification window means the protection is
    ALREADY GONE (position closed immediately — possible on a gapping
    market). Caller should set allow_filled=False for children and handle
    the insta-fill branch explicitly.
    """
    if trade is None or trade.order is None:
        return False
    ib = get_ib()
    deadline = _time.time() + timeout
    last_status = "<none>"
    sym = getattr(trade.contract, "symbol", "?")
    oid = trade.order.orderId
    tag = f"{label or 'order'} {sym}#{oid}"

    while _time.time() < deadline:
        status = trade.orderStatus.status or ""
        last_status = status
        if status in _ACTIVE_ORDER_STATUSES:
            return True
        if status == "Filled":
            if allow_filled:
                return True
            # H8: caller doesn't want instant-fill counted as "live"
            log.warning(
                f"⚠️  {tag} filled during verification window — "
                f"protective order already terminal"
            )
            return False
        if status in _TERMINAL_DEAD_STATUSES:
            reason = trade.log[-1].message if trade.log else "no log"
            log.error(f"❌ {tag} rejected/dead: {status} — {reason}")
            return False
        ib.waitOnUpdate(timeout=0.2)

    log.warning(
        f"⚠️  {tag} did not reach active status within {timeout}s "
        f"(last status: {last_status}) — treating as NOT verified"
    )
    return False


def _verify_order_cancelled(trade: Trade, timeout: float = ORDER_PLACEMENT_VERIFY_SECS,
                            label: str = "") -> bool:
    """
    H-3: After _safe_cancel, confirm the order actually reached a terminal
    dead state. `_safe_cancel` swallows all exceptions, so a silently-failed
    cancel can leave a child bracket alive — e.g., after a verify-failed
    child placement we'd think we'd cleaned up and return (None, None), but
    a live orphan SELL could still fire.

    Returns True only if status ∈ {Cancelled, ApiCancelled, Inactive,
    PendingCancel, Filled} within `timeout`. Filled is accepted because a
    filled order is also no longer live and cannot cause double-quote.
    """
    if trade is None or trade.order is None:
        return True  # nothing to verify
    ib = get_ib()
    deadline = _time.time() + timeout
    terminal = _TERMINAL_DEAD_STATUSES | {"Filled"}
    sym = getattr(trade.contract, "symbol", "?")
    oid = trade.order.orderId
    tag = f"{label or 'cancel'} {sym}#{oid}"

    while _time.time() < deadline:
        status = trade.orderStatus.status or ""
        if status in terminal:
            return True
        ib.waitOnUpdate(timeout=0.2)

    log.critical(
        f"🚨 {tag} did not reach terminal status within {timeout}s "
        f"(last status: {trade.orderStatus.status!r}) — order MAY STILL BE LIVE"
    )
    return False


# ══════════════════════════════════════════════════════════════════════════
#  TP AMEND  (B2 + R2)
# ══════════════════════════════════════════════════════════════════════════

def _modify_tp_limit_price(tp_trade: Trade, new_limit_price: float) -> bool:
    """
    B2: In-place amend of TP limit preserving OCA linkage.
    R2: Verify post-submit that the amended order is still live.
    """
    if tp_trade is None or tp_trade.order is None:
        return False
    ib = get_ib()
    try:
        tp_trade.order.lmtPrice = _round_to_tick(new_limit_price, tp_trade.contract.exchange)
        ib.placeOrder(tp_trade.contract, tp_trade.order)
        if not _verify_order_live(tp_trade, label="TP amend"):
            log.error(
                f"⚠️  TP amendment did NOT verify active for {tp_trade.contract.symbol}; "
                f"original OCA linkage preserved — position remains protected. "
                f"Manual inspection recommended."
            )
            return False
        return True
    except Exception as e:
        log.error(f"TP modify failed for {tp_trade.contract.symbol}: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════
#  BRACKET PLACEMENT  (H6 + H8 + C2 rollback)
# ══════════════════════════════════════════════════════════════════════════

def _place_child_bracket_orders(contract: Stock, qty: int, entry_ref_price: float,
                                trail_pct: float, tp_pct: float,
                                purpose: str = "") -> Tuple[Optional[Trade], Optional[Trade]]:
    ib = get_ib()
    oca = _oca_group_name(contract.symbol, purpose)
    tp_price = _round_to_tick(entry_ref_price * (1 + tp_pct), contract.exchange)

    tp = LimitOrder("SELL", qty, tp_price)
    tp.tif = "GTC"
    tp.ocaGroup = oca
    tp.ocaType = 1
    tp.transmit = False

    trail = Order()
    trail.action = "SELL"
    trail.orderType = "TRAIL"
    trail.totalQuantity = qty
    trail.trailingPercent = round(trail_pct * 100, 4)
    trail.tif = "GTC"
    trail.ocaGroup = oca
    trail.ocaType = 1
    trail.transmit = True

    tp_trade = None
    trail_trade = None
    try:
        tp_trade = ib.placeOrder(contract, tp)
        trail_trade = ib.placeOrder(contract, trail)

        # R9 + H8: verify children go live; a child "Filled" during the
        # verification window means protection is already gone — treat as
        # failure so caller can react instead of silently trusting the id.
        tp_ok = _verify_order_live(tp_trade, label="child TP", allow_filled=False)
        trail_ok = _verify_order_live(trail_trade, label="child trail", allow_filled=False)
        if not (tp_ok and trail_ok):
            log.error(
                f"🚨 Child bracket for {contract.symbol} placement unverified "
                f"(tp_ok={tp_ok}, trail_ok={trail_ok}) — cancelling both to avoid "
                f"ambiguous state"
            )
            # H-3: verify cancels actually stuck. A silently-failed cancel
            # here leaves an orphan SELL that can fire later as a surprise.
            if tp_trade is not None:
                _safe_cancel(tp_trade)
            if trail_trade is not None:
                _safe_cancel(trail_trade)
            tp_dead = _verify_order_cancelled(tp_trade, label="child TP cancel")
            trail_dead = _verify_order_cancelled(trail_trade, label="child trail cancel")
            if not (tp_dead and trail_dead):
                log.critical(
                    f"🚨 {contract.symbol}: child cancel verification FAILED "
                    f"(tp_dead={tp_dead}, trail_dead={trail_dead}). Possible "
                    f"orphan SELL order live — operator must inspect."
                )
            return None, None
        return tp_trade, trail_trade
    except Exception as e:
        log.error(f"Child bracket placement failed for {contract.symbol}: {e}")
        if tp_trade is not None:
            _safe_cancel(tp_trade)
        if trail_trade is not None:
            _safe_cancel(trail_trade)
        tp_dead = _verify_order_cancelled(tp_trade, label="child TP cancel") if tp_trade else True
        trail_dead = _verify_order_cancelled(trail_trade, label="child trail cancel") if trail_trade else True
        if not (tp_dead and trail_dead):
            log.critical(
                f"🚨 {contract.symbol}: exception-path cancel verification FAILED "
                f"(tp_dead={tp_dead}, trail_dead={trail_dead}) — orphan SELL risk"
            )
        return None, None


def _place_bracket(contract: Stock, qty: int, entry_price_est: float,
                   trail_pct: float, tp_pct: float) -> Optional[Tuple[Trade, Trade, Trade]]:
    """
    C2: Full rollback on any mid-sequence exception. If parent submitted
    and any child placement raises, cancel everything we got back before
    returning None — no unprotected half-brackets.
    H6: UUID-based OCA group name.
    """
    ib = get_ib()
    parent_id = ib.client.getReqId()
    tp_id = ib.client.getReqId()
    trail_id = ib.client.getReqId()
    oca_group = _oca_group_name(contract.symbol, f"parent{parent_id}")

    tp_price = _round_to_tick(entry_price_est * (1 + tp_pct), contract.exchange)

    limit_price = _round_to_tick(entry_price_est * 1.01, contract.exchange)
    parent = LimitOrder("BUY", qty, limit_price)
    parent.orderId = parent_id
    parent.transmit = False

    tp_order = LimitOrder("SELL", qty, tp_price)
    tp_order.orderId = tp_id
    tp_order.parentId = parent_id
    tp_order.ocaGroup = oca_group
    tp_order.ocaType = 1
    tp_order.tif = "GTC"
    tp_order.transmit = False

    trail = Order()
    trail.orderId = trail_id
    trail.parentId = parent_id
    trail.action = "SELL"
    trail.orderType = "TRAIL"
    trail.totalQuantity = qty
    trail.trailingPercent = round(trail_pct * 100, 4)
    trail.ocaGroup = oca_group
    trail.ocaType = 1
    trail.tif = "GTC"
    trail.transmit = True    # Final leg releases the whole group

    parent_trade = None
    tp_trade = None
    trail_trade = None
    try:
        parent_trade = ib.placeOrder(contract, parent)
        tp_trade = ib.placeOrder(contract, tp_order)
        trail_trade = ib.placeOrder(contract, trail)
        return parent_trade, tp_trade, trail_trade
    except Exception as e:
        # C2: rollback everything we got back before raising
        log.critical(
            f"🚨 Bracket placement FAILED mid-sequence for {contract.symbol}: {e}. "
            f"Rolling back any submitted legs."
        )
        for t in (parent_trade, tp_trade, trail_trade):
            if t is not None:
                _safe_cancel(t)
        return None


# ══════════════════════════════════════════════════════════════════════════
#  BUY — BRACKET  (R7)
# ══════════════════════════════════════════════════════════════════════════

def buy_stock_bracket(contract: Stock, amount: float,
                      trail_pct: float, tp_pct: float) -> Optional[dict]:
    ib = get_ib()
    price = get_current_price(contract)
    if not price or price <= 0:
        log.warning(f"Cannot buy {contract.symbol} — no price available")
        return None

    qty = _calc_quantity(contract, amount, price)
    if qty is None:
        return None

    result = _place_bracket(contract, qty, price, trail_pct, tp_pct)
    if not result:
        return None

    parent_trade, tp_trade, trail_trade = result
    filled, filled_qty = _wait_for_fill(parent_trade)

    if not filled and filled_qty == 0:
        reason = parent_trade.log[-1].message if parent_trade.log else "Unknown"
        status = parent_trade.orderStatus.status
        log.error(f"❌ BUY {contract.symbol} REJECTED — {status}: {reason}")
        _safe_cancel(tp_trade)
        _safe_cancel(trail_trade)
        return None

    if not filled and filled_qty > 0:
        # R7: PLACE-BEFORE-CANCEL on partial fill
        log.warning(
            f"⚠️  {contract.symbol} PARTIAL fill {filled_qty}/{qty} — "
            f"R7: placing new correctly-sized bracket BEFORE cancelling old"
        )
        actual_price = parent_trade.orderStatus.avgFillPrice or price

        new_tp, new_trail = _place_child_bracket_orders(
            contract, filled_qty, actual_price, trail_pct, tp_pct,
            purpose=f"repair_p{parent_trade.order.orderId}"
        )

        if new_tp is None or new_trail is None:
            log.critical(
                f"🚨 {contract.symbol}: NEW partial-fill bracket failed to place/verify. "
                f"Leaving OLD oversized bracket in place — it WILL protect the "
                f"{filled_qty} filled shares but will over-quote. Cancelling only "
                f"the parent remainder. Manual review required."
            )
            _safe_cancel(parent_trade)
            actual_qty = filled_qty
            tp_order_id = str(tp_trade.order.orderId) if tp_trade else None
            trail_order_id = str(trail_trade.order.orderId) if trail_trade else None
        else:
            log.info(
                f"   ✅ New partial-fill bracket live "
                f"(TP#{new_tp.order.orderId}, Trail#{new_trail.order.orderId}); "
                f"cancelling old oversized bracket"
            )
            _safe_cancel(parent_trade)
            _safe_cancel(tp_trade)
            _safe_cancel(trail_trade)
            ib.waitOnUpdate(timeout=0.5)
            actual_qty = filled_qty
            tp_order_id = str(new_tp.order.orderId)
            trail_order_id = str(new_trail.order.orderId)

    else:
        # Full fill path
        actual_price = parent_trade.orderStatus.avgFillPrice or price
        actual_qty = int(parent_trade.orderStatus.filled or qty)
        tp_order_id = str(tp_trade.order.orderId)
        trail_order_id = str(trail_trade.order.orderId)

        # B2 + R2: in-place amend of TP to match actual fill price
        if REPAIR_TP_AFTER_FILL:
            current_tp_price = tp_trade.order.lmtPrice
            correct_tp = round(actual_price * (1 + tp_pct), 2)
            if current_tp_price and abs(correct_tp - current_tp_price) / current_tp_price > 0.002:
                log.info(f"  🔧 TP repair {contract.symbol}: ${current_tp_price:.2f} → "
                         f"${correct_tp:.2f} (in-place amend, trail untouched)")
                if _modify_tp_limit_price(tp_trade, correct_tp):
                    log.info(f"     ✅ TP amended in place (orderId {tp_order_id})")
                else:
                    log.error(
                        f"  ⚠️  TP amend failed/unverified for {contract.symbol}; "
                        f"keeping original TP @ ${current_tp_price:.2f} "
                        f"(position remains protected)"
                    )

    log.info(
        f"✅ BUY  {contract.symbol:<8} @ ${actual_price:.4f} qty:{actual_qty} "
        f"| TP:${round(actual_price*(1+tp_pct),2)} | Trail:{trail_pct*100:.1f}% "
        f"| {contract.exchange} | Parent:{parent_trade.order.orderId}"
    )

    return {
        "order_id": str(parent_trade.order.orderId),
        "qty": actual_qty,
        "price": round(actual_price, 4),
        "tp_order_id": tp_order_id,
        "trail_order_id": trail_order_id,
    }


def buy_stock_simple(contract: Stock, amount: float) -> Optional[dict]:
    log.critical(
        "🚨 buy_stock_simple invoked — USE_BRACKET_ORDERS=False means NO PROTECTION. "
        "This is an unsafe configuration for real money. Verify intent."
    )
    ib = get_ib()
    price = get_current_price(contract)
    if not price or price <= 0:
        return None
    qty = _calc_quantity(contract, amount, price)
    if qty is None:
        return None
    try:
        limit_price = round(price * 1.01, 2)
        order = LimitOrder("BUY", qty, limit_price)
        trade = ib.placeOrder(contract, order)
        filled, filled_qty = _wait_for_fill(trade)

        if not filled and filled_qty == 0:
            reason = trade.log[-1].message if trade.log else "Unknown"
            log.error(f"❌ BUY {contract.symbol} REJECTED — {trade.orderStatus.status}: {reason}")
            return None

        if not filled and filled_qty > 0:
            log.warning(f"⚠️  {contract.symbol} PARTIAL {filled_qty}/{qty} — "
                        f"UNPROTECTED (no bracket in simple mode)")
            _safe_cancel(trade)

        actual_price = trade.orderStatus.avgFillPrice or price
        return {
            "order_id": str(trade.order.orderId),
            "qty": filled_qty,
            "price": round(actual_price, 4),
            "tp_order_id": None,
            "trail_order_id": None,
        }
    except Exception as e:
        log.error(f"Buy failed {contract.symbol}: {e}")
        return None


def buy_stock(contract: Stock, amount: float, trail_pct: float, tp_pct: float) -> Optional[dict]:
    if USE_BRACKET_ORDERS:
        return buy_stock_bracket(contract, amount, trail_pct, tp_pct)
    return buy_stock_simple(contract, amount)


def sell_stock(contract: Stock, qty: float) -> Optional[dict]:
    ib = get_ib()
    qty = int(qty)
    if qty < 1:
        log.warning(f"Cannot sell <1 share of {contract.symbol}")
        return None
    try:
        order = MarketOrder("SELL", qty)
        trade = ib.placeOrder(contract, order)
        filled, filled_qty = _wait_for_fill(trade)

        if not filled and filled_qty == 0:
            reason = trade.log[-1].message if trade.log else "Unknown"
            log.error(f"❌ SELL {contract.symbol} REJECTED — {trade.orderStatus.status}: {reason}")
            return None
        if not filled and filled_qty > 0:
            log.warning(f"⚠️  SELL {contract.symbol} PARTIAL {filled_qty}/{qty} at timeout — "
                        f"cancelling remainder")
            _safe_cancel(trade)

        avg_fill = sanitise_price(trade.orderStatus.avgFillPrice)

        avg_fill_display = f"${avg_fill:.4f}" if avg_fill is not None else "N/A"
        log.info(f"🔴 SELL {contract.symbol:<8} qty:{filled_qty}/{qty} | {contract.exchange} | "
                 f"Order:{trade.order.orderId} @ {avg_fill_display}")
        return {
            "order_id": str(trade.order.orderId),
            "filled_qty": filled_qty,
            "requested_qty": qty,
            "avg_fill_price": avg_fill,
        }
    except Exception as e:
        log.error(f"Sell failed {contract.symbol}: {e}")
        return None


def cancel_open_orders_for(contract: Stock):
    ib = get_ib()
    try:
        for t in ib.openTrades():
            if t.contract.conId == contract.conId:
                ib.cancelOrder(t.order)
                log.info(f"   🧹 Cancelled open order {t.order.orderId} on {contract.symbol}")
    except Exception as e:
        log.warning(f"cancel_open_orders failed for {contract.symbol}: {e}")


def cancel_all_orders():
    ib = get_ib()
    try:
        ib.reqGlobalCancel()
        log.warning("🚨 reqGlobalCancel() issued")
    except Exception as e:
        log.error(f"Global cancel failed: {e}")


# ══════════════════════════════════════════════════════════════════════════
#  EXECUTION LOOKUP  (B3)
# ══════════════════════════════════════════════════════════════════════════

def get_recent_sell_fill(contract: Stock,
                         lookback_seconds: int = 86400,
                         opened_after: Optional[datetime] = None) -> Optional[dict]:
    """
    B3: Summed weighted-average SELL fills with opened_after floor.
    Returns {"price", "qty", "exec_id", "time"} or None.

    C3 note: used by _reconcile_post_sell to corroborate IBKR position
    readings vs arithmetic before the "prefer arithmetic" branch fires.
    """
    ib = get_ib()
    try:
        fills = ib.fills()
    except Exception as e:
        log.warning(f"ib.fills() failed for {contract.symbol}: {e}")
        return None

    generic_cutoff = datetime.now(timezone.utc) - timedelta(seconds=lookback_seconds)
    if opened_after is not None:
        if opened_after.tzinfo is None:
            opened_after = opened_after.replace(tzinfo=timezone.utc)
        cutoff = max(generic_cutoff, opened_after)
    else:
        cutoff = generic_cutoff

    qualifying: List[Tuple[datetime, "object"]] = []
    for fill in fills:
        try:
            if fill.contract.conId != contract.conId:
                continue
            side = getattr(fill.execution, "side", "")
            if side not in ("SLD", "SELL"):
                continue
            exec_time = fill.time
            if exec_time is None:
                continue
            if exec_time.tzinfo is None:
                exec_time = exec_time.replace(tzinfo=timezone.utc)
            if exec_time < cutoff:
                continue
            qualifying.append((exec_time, fill))
        except Exception:
            continue

    if not qualifying:
        return None

    total_qty = 0
    notional = 0.0
    latest_time: Optional[datetime] = None
    latest_exec_id: Optional[str] = None
    for exec_time, fill in qualifying:
        try:
            px = float(fill.execution.avgPrice or fill.execution.price or 0)
            sh = int(float(fill.execution.shares or 0))
        except (TypeError, ValueError):
            continue
        if px <= 0 or sh <= 0:
            continue
        total_qty += sh
        notional += px * sh
        if latest_time is None or exec_time > latest_time:
            latest_time = exec_time
            latest_exec_id = fill.execution.execId

    if total_qty <= 0 or notional <= 0:
        return None

    weighted_avg = notional / total_qty
    return {
        "price": round(weighted_avg, 4),
        "qty": total_qty,
        "exec_id": latest_exec_id,
        "time": latest_time,
    }


# ══════════════════════════════════════════════════════════════════════════
#  BRACKET RE-ATTACHMENT  (R8 + C4/M8)
# ══════════════════════════════════════════════════════════════════════════

def wait_for_open_orders_stable(max_wait: float = 10.0,
                                stable_secs: float = 2.0) -> int:
    """
    H-8: After (re)connect, GTC orders from prior sessions take time to
    flow back into ib_insync's openTrades() list. reconcile_existing_positions
    calls has_protective_orders → inspect_protective_orders → ib.openTrades()
    and will falsely report "no protection" until the broker finishes
    registering those orders, triggering a DUPLICATE bracket attach.

    This helper polls ib.openTrades() until the count has been stable for
    `stable_secs` seconds (or max_wait elapsed) and returns the final count.
    Call once before startup reconcile; not needed mid-loop.
    """
    ib = get_ib()
    deadline = _time.time() + max_wait
    last_count = -1
    stable_since = _time.time()
    # Proactively ask the broker to re-send open orders — ib_insync does
    # this via reqAutoOpenOrders on connect, but an explicit nudge is cheap.
    try:
        ib.reqAllOpenOrders()
    except Exception as e:
        log.warning(f"reqAllOpenOrders nudge failed (non-fatal): {e}")
    while _time.time() < deadline:
        ib.waitOnUpdate(timeout=0.5)
        try:
            count = len(ib.openTrades())
        except Exception:
            count = last_count
        if count == last_count:
            if _time.time() - stable_since >= stable_secs:
                return count
        else:
            last_count = count
            stable_since = _time.time()
    return last_count if last_count >= 0 else 0


def inspect_protective_orders(contract: Stock) -> dict:
    """
    C4/M8: Decompose active SELL protective orders into TP (limit) vs
    trail (TRAIL) components. Used to detect half-protected positions
    that need bracket re-attachment.

    Returns: {
        "tp_qty": int,           # summed qty of active LIMIT SELL orders
        "trail_qty": int,        # summed qty of active TRAIL SELL orders
        "other_qty": int,        # other active SELL orders (stop, stop-limit, ...)
        "total_qty": int,
    }
    """
    ib = get_ib()
    info = {"tp_qty": 0, "trail_qty": 0, "other_qty": 0, "total_qty": 0}
    try:
        for t in ib.openTrades():
            if t.contract.conId != contract.conId:
                continue
            if t.order.action != "SELL":
                continue
            status = t.orderStatus.status or ""
            if status not in _ACTIVE_ORDER_STATUSES:
                continue
            try:
                q = int(t.order.totalQuantity or 0)
            except (TypeError, ValueError):
                q = 0
            if q <= 0:
                continue
            otype = (t.order.orderType or "").upper()
            if otype == "LMT":
                info["tp_qty"] += q
            elif otype == "TRAIL":
                info["trail_qty"] += q
            else:
                info["other_qty"] += q
            info["total_qty"] += q
    except Exception as e:
        log.warning(f"inspect_protective_orders error for {contract.symbol}: {e}")
    return info


def has_protective_orders(contract: Stock, required_qty: Optional[int] = None) -> bool:
    """
    C4/M8: A position is "fully protected" only if BOTH an active TP (LMT
    SELL) AND an active trailing stop (TRAIL SELL) cover at least
    `required_qty`. Previously this counted any summed SELL qty — which
    would falsely flag a bare TP-only or bare trail-only position as
    protected, and would also pass through stale GTC sells from old
    sessions.

    Returns True only when BOTH legs cover the required qty.
    """
    info = inspect_protective_orders(contract)
    required = required_qty if (required_qty and required_qty > 0) else 1
    return info["tp_qty"] >= required and info["trail_qty"] >= required


def attach_bracket_to_existing_position(
    contract: Stock, qty: int, entry_ref_price: float,
    trail_pct: float = DEFAULT_TRAILING_STOP,
    tp_pct: float = DEFAULT_TAKE_PROFIT
) -> Tuple[Optional[str], Optional[str]]:
    """
    C4/M8: If the position is HALF-protected (TP-only or trail-only), we
    now cancel the half-bracket and place a fresh full bracket. Only
    fully-protected positions (both TP+trail covering qty) are skipped.
    """
    info = inspect_protective_orders(contract)
    tp_covers = info["tp_qty"] >= qty
    trail_covers = info["trail_qty"] >= qty

    if tp_covers and trail_covers:
        log.info(
            f"   ℹ️  {contract.symbol} already has full active protection "
            f"(TP qty={info['tp_qty']}, trail qty={info['trail_qty']} ≥ {qty}) "
            f"— skipping attach"
        )
        return (None, None)

    if info["total_qty"] > 0:
        # Half-protected or other-typed — cancel and replace with full bracket
        log.warning(
            f"   ⚠️  {contract.symbol} HALF-PROTECTED "
            f"(tp={info['tp_qty']}, trail={info['trail_qty']}, other={info['other_qty']}, "
            f"required={qty}) — cancelling existing SELL orders and placing fresh full bracket"
        )
        cancel_open_orders_for(contract)
        # Give broker a moment to settle cancels
        try:
            get_ib().waitOnUpdate(timeout=0.8)
        except Exception:
            pass

    tp_trade, trail_trade = _place_child_bracket_orders(
        contract, qty, entry_ref_price, trail_pct, tp_pct,
        purpose=f"attach"
    )
    if tp_trade and trail_trade:
        log.info(f"   🛡️  Attached bracket to {contract.symbol}: "
                 f"TP ${round(entry_ref_price*(1+tp_pct),2)} | Trail {trail_pct*100:.1f}%")
        return (str(tp_trade.order.orderId), str(trail_trade.order.orderId))
    log.error(f"   ❌ Failed to attach bracket to {contract.symbol}")
    return (None, None)


# ══════════════════════════════════════════════════════════════════════════
#  PORTFOLIO  (B1 + C1)
# ══════════════════════════════════════════════════════════════════════════

def get_all_positions() -> Dict[str, dict]:
    ib = get_ib()
    positions = {}
    for pos in ib.positions():
        sym = pos.contract.symbol
        positions[sym] = {
            "qty": float(pos.position),
            "avg_cost": float(pos.avgCost),
            "contract": pos.contract,
        }
    return positions


def get_account_summary() -> dict:
    """C1: Uses cycle-atomic collect_account_summary."""
    return collect_account_summary()


def flatten_all_positions(reason: str = "emergency") -> Dict[str, dict]:
    """
    R10 support. Returns per-symbol sell outcomes.

    H7 note: only attempted symbols appear. Caller in global_rsi_bot.py
    treats missing entries as "not found in IBKR at flatten time" — those
    are assumed already closed (since get_all_positions was empty for
    them) and removed from bot_positions.
    """
    log.critical(f"🚨 FLATTEN ALL triggered — reason: {reason}")
    outcomes: Dict[str, dict] = {}
    cancel_all_orders()
    _time.sleep(2)
    positions = get_all_positions()
    for sym, info in positions.items():
        qty = int(info["qty"])
        if qty <= 0:
            continue
        contract = info["contract"]
        try:
            cancel_open_orders_for(contract)
            result = sell_stock(contract, qty)
            if result and result.get("filled_qty", 0) > 0:
                filled = int(result.get("filled_qty") or 0)
                requested = int(result.get("requested_qty") or qty)
                # H-5: make partial-vs-full distinction explicit so the caller
                # can re-attach a bracket to the remainder instead of losing
                # track of unprotected shares.
                outcomes[sym] = {
                    "success": filled >= requested,
                    "partial": 0 < filled < requested,
                    "filled_qty": filled,
                    "requested_qty": requested,
                    "remaining_qty": max(0, requested - filled),
                    "avg_fill_price": result.get("avg_fill_price"),
                    "contract": contract,
                }
            else:
                outcomes[sym] = {
                    "success": False,
                    "partial": False,
                    "filled_qty": 0,
                    "requested_qty": qty,
                    "remaining_qty": qty,
                    "avg_fill_price": None,
                    "contract": contract,
                }
        except Exception as e:
            log.error(f"Flatten failed for {sym}: {e}")
            outcomes[sym] = {
                "success": False,
                "partial": False,
                "filled_qty": 0,
                "requested_qty": qty,
                "remaining_qty": qty,
                "avg_fill_price": None,
                "contract": contract,
                "error": str(e),
            }
    return outcomes
