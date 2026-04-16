"""
IBKR Helpers — Connection, Data, Bracket Orders, Regime, Guards
=================================================================
Thread-safe connection management, event-driven trade waits with partial-fill
handling, bracket orders with server-side TP + trailing stop, market-hours
gating, atomic post-fill TP repair (in-place amend), VIX-aware market
regime with safe degradation, execution lookup for trade-history
reconciliation.

R-round hardening added on top of B1–B4 / H1–H4:
  R1: account_values_healthy flag set by _resolve_account_value; buy gate
  R2: _modify_tp_limit_price verifies status post-submit
  R7: buy_stock_bracket partial-fill places new bracket BEFORE cancelling old
  R8: has_protective_orders filters on active statuses AND qty coverage
  R9: _verify_order_live polls orderStatus after placement
  R12: _manager clears regime + VIX caches on successful reconnect
"""

import asyncio
import logging
import math
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple, List
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ib_insync import IB, Stock, Index, MarketOrder, LimitOrder, Order, Trade, util

from config import (
    IB_HOST, IB_PORT, IB_CLIENT_ID,
    CASH_RESERVE_PCT, MAX_POSITIONS, MAX_PER_CORR_GROUP,
    CORR_GROUPS, BARS_FOR_RSI, RSI_PERIOD, SEHK_LOT_SIZES,
    MARKET_DATA_LIVE, REGIME_CACHE_TTL,
    MARKET_DATA_SNAPSHOT_WAIT, MARKET_DATA_BATCH_WAIT,
    CONNECTION_TIMEOUT, TRADE_FILL_TIMEOUT,
    RECONNECT_INITIAL_DELAY, RECONNECT_MAX_DELAY,
    RECONNECT_BACKOFF_FACTOR, RECONNECT_MAX_ATTEMPTS,
    EXCHANGE_SESSIONS, MARKET_HOURS_GRACE_MINS,
    ATR_PERIOD, ATR_PERIOD_FOR_SIZING,
    USE_BRACKET_ORDERS, REPAIR_TP_AFTER_FILL,
    DEFAULT_TRAILING_STOP, DEFAULT_TAKE_PROFIT,
    USE_VIX_IN_REGIME,
    VIX_BULL_MAX, VIX_CAUTION_MAX_ABOVE_200MA, VIX_CAUTION_MAX_BELOW_200MA,
    ORDER_PLACEMENT_VERIFY_SECS,
)

log = logging.getLogger("ibkr-rsi")


# ══════════════════════════════════════════════════════════════════════════
#  ACCOUNT-VALUE HEALTH FLAG  (R1)
# ══════════════════════════════════════════════════════════════════════════
# Module-level flag set by _resolve_account_value. The main loop's try_buy
# consults this via account_values_healthy() before sizing any new position.
# False means the last BASE-currency resolution for NLV or cash fell back
# to a per-currency value (degraded) OR failed outright — we block buys
# until the next accountSummary returns cleanly.
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
        self._connected = False
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
                self._connected = True

                # R12: clear ALL caches that could carry stale state across
                # a reconnect. Previously only _contract_cache was cleared;
                # regime/VIX could still return up-to-30-min-old values,
                # which is especially dangerous after a long outage where
                # the market may have materially moved.
                self._contract_cache.clear()
                self._regime_cache = None
                self._regime_time = 0.0
                self._vix_cache = None
                log.info("   Caches cleared: contracts, regime, VIX")
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
        with self._lock:
            self._connected = False

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
            self._connected = False

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
        with self._lock:
            if self._vix_cache and (_time.time() - self._vix_cache[1]) < REGIME_CACHE_TTL:
                return self._vix_cache[0]
            return None

    def set_vix_cache(self, level: float):
        with self._lock:
            self._vix_cache = (level, _time.time())


_manager = IBConnectionManager()


def get_ib() -> IB:
    return _manager.get()


def disconnect():
    _manager.disconnect()


def is_connected() -> bool:
    return _manager.is_connected()


# ══════════════════════════════════════════════════════════════════════════
#  ACCOUNT VALUE RESOLUTION  (B1 + R1)
# ══════════════════════════════════════════════════════════════════════════

def _resolve_account_value(account_values, tag: str,
                           allowed_currencies=("USD", "AUD", "CAD", "SGD", "GBP", "EUR")) -> float:
    """
    B1: Deterministic two-pass resolution: BASE first, then allowed currency.
    R1: Updates the module-level health flag. If BASE resolved cleanly and
        we haven't seen any failure in this resolution, health is True; any
        fallback or failure flips it False and the main loop's try_buy
        will block new buys until the next accountSummary call restores it.

    NOTE: because this is called per-tag, one degraded tag will poison
    subsequent clean tags in the same accountSummary cycle. That's the
    intended conservative behaviour — if any tag is degraded we don't
    trust the snapshot.
    """
    # Pass 1 — BASE is always authoritative
    for item in account_values:
        if item.tag == tag and item.currency == "BASE":
            try:
                val = float(item.value)
                # Clean BASE resolution — mark health True (only if still True)
                _set_account_health(True, f"BASE resolved cleanly for {tag}")
                return val
            except (TypeError, ValueError):
                log.error(f"Malformed BASE value for {tag}: {item.value!r}")
                _set_account_health(False, f"malformed BASE value for {tag}: {item.value!r}")
                return 0.0

    # Pass 2 — no BASE, fall back
    for item in account_values:
        if item.tag == tag and item.currency in allowed_currencies:
            try:
                val = float(item.value)
                msg = (f"No BASE row for {tag} in accountSummary — "
                       f"falling back to {item.currency} {val}")
                log.warning(f"⚠️  {msg}. Verify IBKR account config.")
                # R1: degraded — new buys blocked until BASE comes back
                _set_account_health(False, msg)
                return val
            except (TypeError, ValueError):
                continue

    msg = f"Could not resolve {tag} from accountSummary"
    log.error(f"{msg} — returning 0.0 (conservative)")
    _set_account_health(False, msg)
    return 0.0


# ══════════════════════════════════════════════════════════════════════════
#  MARKET HOURS
# ══════════════════════════════════════════════════════════════════════════

def is_market_open(exchange: str, now: Optional[datetime] = None) -> Tuple[bool, str]:
    sess = EXCHANGE_SESSIONS.get(exchange)
    if not sess:
        return True, "unknown exchange — deferring to IBKR"

    tz = ZoneInfo(sess["tz"])
    now = (now or datetime.now(tz=ZoneInfo("UTC"))).astimezone(tz)

    if now.weekday() not in sess["days"]:
        return False, f"{exchange} closed (weekend, local {now.strftime('%a %H:%M')})"

    grace = timedelta(minutes=MARKET_HOURS_GRACE_MINS)
    open_dt = now.replace(hour=sess["open"].hour, minute=sess["open"].minute,
                          second=0, microsecond=0) - grace
    close_dt = now.replace(hour=sess["close"].hour, minute=sess["close"].minute,
                           second=0, microsecond=0) + grace

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
    contract = Stock(symbol, exchange, currency)
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
    ib = get_ib()
    try:
        ticker = ib.reqMktData(contract, "", True, False)
        ib.sleep(MARKET_DATA_SNAPSHOT_WAIT)
        price = _extract_price(ticker)
        ib.cancelMktData(contract)
        return round(price, 4) if price else None
    except Exception as e:
        log.warning(f"Price error {contract.symbol}: {e}")
        return None


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

    for contract in contracts.values():
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
#  GUARDS  (B1 + R1)
# ══════════════════════════════════════════════════════════════════════════

def cash_guard_check() -> Tuple[bool, float, float]:
    """
    B1: Deterministic BASE-currency resolution.
    R1: If account_values_healthy() is False, block the buy — we don't
        trust the snapshot. _resolve_account_value has already logged why.
    """
    ib = get_ib()
    try:
        account_values = ib.accountSummary()
        cash = _resolve_account_value(account_values, "TotalCashValue")
        portfolio = _resolve_account_value(account_values, "NetLiquidation")

        healthy, reason = account_values_healthy()
        if not healthy:
            log.info(f"  🚫 Cash guard: account-values degraded ({reason}) — blocking buy")
            return False, 0.0, 0.0

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
# Statuses that indicate terminal non-fill (cancelled or rejected)
_TERMINAL_DEAD_STATUSES = {"Cancelled", "ApiCancelled", "Inactive"}


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
    ib = get_ib()
    deadline = _time.time() + timeout
    total_qty = int(trade.order.totalQuantity)

    while _time.time() < deadline:
        status = trade.orderStatus.status
        filled_qty = int(trade.orderStatus.filled or 0)

        if status == "Filled" or filled_qty >= total_qty:
            return True, filled_qty

        if status in ("Cancelled", "ApiCancelled", "Inactive") and filled_qty == 0:
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


# ══════════════════════════════════════════════════════════════════════════
#  ORDER VERIFICATION  (R9)
# ══════════════════════════════════════════════════════════════════════════

def _verify_order_live(trade: Trade, timeout: float = ORDER_PLACEMENT_VERIFY_SECS,
                       label: str = "") -> bool:
    """
    R9: Poll orderStatus for up to `timeout` seconds to verify a freshly-
    placed or amended order actually became active. Returns True if the
    order reached an active status (Submitted/PreSubmitted/Accepted) OR
    filled (rare but possible for amends). Returns False if the order
    landed in Cancelled/Inactive OR we timed out in a pre-transmit state.

    Used after placements and amendments to catch silent rejections
    (bad tick size, exchange closed, etc.) before the caller trusts the
    orderId.
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
        if status in _ACTIVE_ORDER_STATUSES or status == "Filled":
            return True
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


# ══════════════════════════════════════════════════════════════════════════
#  TP AMEND  (B2 + R2)
# ══════════════════════════════════════════════════════════════════════════

def _modify_tp_limit_price(tp_trade: Trade, new_limit_price: float) -> bool:
    """
    B2: In-place amend of TP limit preserving OCA linkage — no unprotected
    window.
    R2: Verify post-submit that the amended order is still live. If the
    amendment is rejected (tick-size violation, etc.) the ORIGINAL order
    remains intact and protective; this function returns False so the
    caller knows NOT to trust the "new" state. Caller is expected to
    leave the stored tp_order_id pointing at the original (still-live)
    order.
    """
    if tp_trade is None or tp_trade.order is None:
        return False
    ib = get_ib()
    try:
        tp_trade.order.lmtPrice = round(new_limit_price, 2)
        # Same orderId + contract → IBKR treats as amendment, not new order
        ib.placeOrder(tp_trade.contract, tp_trade.order)
        # R2: verify it actually landed cleanly
        if not _verify_order_live(tp_trade, label="TP amend"):
            log.error(
                f"⚠️  TP amendment did NOT verify active for {tp_trade.contract.symbol}; "
                f"original TP order may have been modified BUT status is uncertain. "
                f"Original OCA linkage preserved — position remains protected. "
                f"Manual inspection recommended."
            )
            return False
        return True
    except Exception as e:
        log.error(f"TP modify failed for {tp_trade.contract.symbol}: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════
#  BRACKET PLACEMENT
# ══════════════════════════════════════════════════════════════════════════

def _place_child_bracket_orders(contract: Stock, qty: int, entry_ref_price: float,
                                trail_pct: float, tp_pct: float,
                                oca_group_suffix: str = "") -> Tuple[Optional[Trade], Optional[Trade]]:
    ib = get_ib()
    oca = f"OCA_{contract.symbol}_{oca_group_suffix or int(_time.time())}"
    tp_price = round(entry_ref_price * (1 + tp_pct), 2)

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

        # R9: verify both children actually went live
        tp_ok = _verify_order_live(tp_trade, label="child TP")
        trail_ok = _verify_order_live(trail_trade, label="child trail")
        if not (tp_ok and trail_ok):
            log.error(
                f"🚨 Child bracket for {contract.symbol} placement unverified "
                f"(tp_ok={tp_ok}, trail_ok={trail_ok}) — cancelling both to avoid "
                f"ambiguous state"
            )
            if tp_trade is not None:
                _safe_cancel(tp_trade)
            if trail_trade is not None:
                _safe_cancel(trail_trade)
            return None, None
        return tp_trade, trail_trade
    except Exception as e:
        log.error(f"Child bracket placement failed for {contract.symbol}: {e}")
        if tp_trade is not None:
            _safe_cancel(tp_trade)
        if trail_trade is not None:
            _safe_cancel(trail_trade)
        return None, None


def _place_bracket(contract: Stock, qty: int, entry_price_est: float,
                   trail_pct: float, tp_pct: float) -> Optional[Tuple[Trade, Trade, Trade]]:
    ib = get_ib()
    parent_id = ib.client.getReqId()
    tp_id = ib.client.getReqId()
    trail_id = ib.client.getReqId()
    oca_group = f"OCA_{contract.symbol}_{parent_id}_{int(_time.time())}"

    tp_price = round(entry_price_est * (1 + tp_pct), 2)

    parent = MarketOrder("BUY", qty)
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
    trail.tif = "GTC"
    trail.transmit = True

    parent_trade = None
    tp_trade = None
    trail_trade = None
    try:
        parent_trade = ib.placeOrder(contract, parent)
        tp_trade = ib.placeOrder(contract, tp_order)
        trail_trade = ib.placeOrder(contract, trail)
        return parent_trade, tp_trade, trail_trade
    except Exception as e:
        log.error(f"Bracket placement failed for {contract.symbol}: {e}")
        for t in (parent_trade, tp_trade, trail_trade):
            if t is not None:
                _safe_cancel(t)
        return None


# ══════════════════════════════════════════════════════════════════════════
#  BUY — BRACKET  (R7 — partial-fill place-before-cancel)
# ══════════════════════════════════════════════════════════════════════════

def buy_stock_bracket(contract: Stock, amount: float,
                      trail_pct: float, tp_pct: float) -> Optional[dict]:
    """
    R7 critical change: on partial fill, place the NEW reduced-qty bracket
    BEFORE cancelling the old parent's children. Previously we cancelled
    first then replaced, opening a ~1.5s window where the filled shares
    were unprotected. Now:

      1. Detect partial fill (parent incomplete, filled_qty > 0).
      2. Place a new child OCA bracket sized to `filled_qty`.
      3. Verify it's live (R9).
      4. ONLY THEN cancel the original parent remainder and its OCA children.

    Brief (1–2s) overlap where both old-oversized and new-correct brackets
    exist is harmless: worst case one of them fires and the other gets
    cancelled by the OCA group (old children) or rejected by IBKR for
    over-selling. A gap in protection is unacceptable; a double-cover is
    tolerable.
    """
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
        # ── R7: PLACE-BEFORE-CANCEL on partial fill ───────────────────
        log.warning(
            f"⚠️  {contract.symbol} PARTIAL fill {filled_qty}/{qty} — "
            f"R7: placing new correctly-sized bracket BEFORE cancelling old"
        )
        actual_price = parent_trade.orderStatus.avgFillPrice or price

        # Step 1: place the new reduced-qty bracket first
        new_tp, new_trail = _place_child_bracket_orders(
            contract, filled_qty, actual_price, trail_pct, tp_pct,
            oca_group_suffix=f"repair_{parent_trade.order.orderId}"
        )

        if new_tp is None or new_trail is None:
            # New bracket failed to go live — we have NOT yet cancelled the
            # old oversized bracket, so the old TP+trail are still covering
            # the partial fill (oversized, will reject excess at fill time
            # but the protective direction is intact). Better than nothing.
            log.critical(
                f"🚨 {contract.symbol}: NEW partial-fill bracket failed to place/verify. "
                f"Leaving OLD oversized bracket in place — it WILL protect the "
                f"{filled_qty} filled shares but will over-quote. Cancelling only "
                f"the parent remainder. Manual review required."
            )
            _safe_cancel(parent_trade)
            # Intentionally do NOT cancel tp_trade / trail_trade — they are
            # the current protection. An OCA over-qty sell will be rejected
            # or partially filled by IBKR; that's safer than no protection.
            actual_qty = filled_qty
            tp_order_id = str(tp_trade.order.orderId) if tp_trade else None
            trail_order_id = str(trail_trade.order.orderId) if trail_trade else None
        else:
            # Step 2: new bracket is LIVE. NOW safe to cancel parent + old children.
            log.info(
                f"   ✅ New partial-fill bracket live "
                f"(TP#{new_tp.order.orderId}, Trail#{new_trail.order.orderId}); "
                f"cancelling old oversized bracket"
            )
            _safe_cancel(parent_trade)
            _safe_cancel(tp_trade)
            _safe_cancel(trail_trade)
            # Short settle wait — OCA accept/reject takes milliseconds
            ib.waitOnUpdate(timeout=0.5)
            actual_qty = filled_qty
            tp_order_id = str(new_tp.order.orderId)
            trail_order_id = str(new_trail.order.orderId)

    else:
        # ── Full fill path ────────────────────────────────────────────
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
                    # R2: amend failed or didn't verify. tp_order_id stays
                    # pointing at the ORIGINAL order which IBKR confirmed
                    # was live before the amend attempt. Position remains
                    # protected at the old TP price.
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
    ib = get_ib()
    price = get_current_price(contract)
    if not price or price <= 0:
        return None
    qty = _calc_quantity(contract, amount, price)
    if qty is None:
        return None
    try:
        order = MarketOrder("BUY", qty)
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

        avg_fill_raw = trade.orderStatus.avgFillPrice
        try:
            avg_fill = float(avg_fill_raw) if avg_fill_raw else None
            if avg_fill is not None and avg_fill <= 0:
                avg_fill = None
        except (TypeError, ValueError):
            avg_fill = None

        log.info(f"🔴 SELL {contract.symbol:<8} qty:{filled_qty}/{qty} | {contract.exchange} | "
                 f"Order:{trade.order.orderId} @ ${avg_fill_raw}")
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
#  EXECUTION LOOKUP  (B3 + R3 — clamp fix in caller)
# ══════════════════════════════════════════════════════════════════════════

def get_recent_sell_fill(contract: Stock,
                         lookback_seconds: int = 86400,
                         opened_after: Optional[datetime] = None) -> Optional[dict]:
    """
    B3: Summed weighted-average SELL fills with opened_after floor to
    prevent cross-trade contamination. Returns {"price", "qty", "exec_id",
    "time"} or None.
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
#  BRACKET RE-ATTACHMENT  (R8)
# ══════════════════════════════════════════════════════════════════════════

def has_protective_orders(contract: Stock, required_qty: Optional[int] = None) -> bool:
    """
    R8: A position is "protected" only if active SELL orders cover at least
    `required_qty` (or >= 1 share if qty not specified). Previously this
    counted cancelled/inactive orders because `ib.openTrades()` can include
    stale rows briefly after cancellation. That could cause orphan adoption
    to SKIP attaching a bracket, leaving a real position unprotected.

    Now we filter on active statuses AND sum qty to verify real coverage.
    """
    ib = get_ib()
    try:
        active_sell_qty = 0
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
            active_sell_qty += q

        if required_qty is not None and required_qty > 0:
            return active_sell_qty >= required_qty
        return active_sell_qty > 0
    except Exception as e:
        log.warning(f"has_protective_orders error for {contract.symbol}: {e}")
        # Conservative: on error, assume NOT protected so reattach path runs
        return False


def attach_bracket_to_existing_position(
    contract: Stock, qty: int, entry_ref_price: float,
    trail_pct: float = DEFAULT_TRAILING_STOP,
    tp_pct: float = DEFAULT_TAKE_PROFIT
) -> Tuple[Optional[str], Optional[str]]:
    # R8: pass qty so partial coverage doesn't count as protected
    if has_protective_orders(contract, required_qty=qty):
        log.info(f"   ℹ️  {contract.symbol} already has active protective orders "
                 f"covering >= {qty} shares — skipping attach")
        return (None, None)

    tp_trade, trail_trade = _place_child_bracket_orders(
        contract, qty, entry_ref_price, trail_pct, tp_pct,
        oca_group_suffix=f"attach_{int(_time.time())}"
    )
    if tp_trade and trail_trade:
        log.info(f"   🛡️  Attached bracket to {contract.symbol}: "
                 f"TP ${round(entry_ref_price*(1+tp_pct),2)} | Trail {trail_pct*100:.1f}%")
        return (str(tp_trade.order.orderId), str(trail_trade.order.orderId))
    log.error(f"   ❌ Failed to attach bracket to {contract.symbol}")
    return (None, None)


# ══════════════════════════════════════════════════════════════════════════
#  PORTFOLIO  (B1)
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
    ib = get_ib()
    summary = ib.accountSummary()
    wanted = ("NetLiquidation", "TotalCashValue", "BuyingPower",
              "UnrealizedPnL", "RealizedPnL")
    result = {}
    present_tags = {item.tag for item in summary}
    for tag in wanted:
        if tag in present_tags:
            result[tag] = _resolve_account_value(summary, tag)
    return result


def flatten_all_positions(reason: str = "emergency") -> Dict[str, dict]:
    """
    R10 support: returns a dict of per-symbol sell outcomes so the caller
    (max-DD handler in global_rsi_bot) can remove ONLY confirmed-closed
    positions from its in-memory dict. Previously this returned None and
    the caller blindly cleared all bot_positions.

    Returns: { symbol: {"success": bool, "filled_qty": int,
                        "requested_qty": int, "avg_fill_price": float|None} }
    Any symbol NOT in the returned dict was never attempted (e.g. because
    the flatten aborted before reaching it).
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
                outcomes[sym] = {
                    "success": True,
                    "filled_qty": int(result.get("filled_qty") or 0),
                    "requested_qty": int(result.get("requested_qty") or qty),
                    "avg_fill_price": result.get("avg_fill_price"),
                }
            else:
                outcomes[sym] = {
                    "success": False,
                    "filled_qty": 0,
                    "requested_qty": qty,
                    "avg_fill_price": None,
                }
        except Exception as e:
            log.error(f"Flatten failed for {sym}: {e}")
            outcomes[sym] = {
                "success": False,
                "filled_qty": 0,
                "requested_qty": qty,
                "avg_fill_price": None,
                "error": str(e),
            }
    return outcomes
