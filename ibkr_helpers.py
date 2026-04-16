"""
IBKR Helpers — Connection, Data, Bracket Orders, Regime, Guards
=================================================================
Thread-safe connection management, event-driven trade waits with partial-fill
handling, bracket orders with server-side TP + trailing stop, market-hours
gating (including lunch breaks), atomic post-fill TP repair (in-place amend),
VIX-aware market regime with safe degradation, and execution lookup for
trade-history reconciliation.
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

# ──────────────────────────────────────────────────────────────────────────
# Event loop must exist BEFORE ib_insync is imported/constructed.
# ──────────────────────────────────────────────────────────────────────────
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
)

log = logging.getLogger("ibkr-rsi")


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
        self._vix_cache: Optional[Tuple[float, float]] = None   # (level, ts)
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
                self._contract_cache.clear()
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
#  ACCOUNT VALUE RESOLUTION  (B1)
# ══════════════════════════════════════════════════════════════════════════

def _resolve_account_value(account_values, tag: str,
                           allowed_currencies=("USD", "AUD", "CAD", "SGD", "GBP", "EUR")) -> float:
    """
    SAFETY (B1): Two-pass resolution to deterministically pick the BASE-currency
    value for a given accountSummary tag. IBKR returns one row per currency the
    account holds plus a BASE row — row order is NOT guaranteed. A single-pass
    "take last matching currency" loop could leave us with a per-currency value
    that excludes FX sub-accounts, under-reporting NLV/cash and mis-sizing
    positions.

    Pass 1: find BASE. If present, return it — always authoritative.
    Pass 2: fallback to first allowed per-currency entry ONLY if no BASE row exists.
    """
    # Pass 1 — BASE is always authoritative
    for item in account_values:
        if item.tag == tag and item.currency == "BASE":
            try:
                return float(item.value)
            except (TypeError, ValueError):
                log.error(f"Malformed BASE value for {tag}: {item.value!r}")
                return 0.0

    # Pass 2 — no BASE found, fall back to a known currency (and log it)
    for item in account_values:
        if item.tag == tag and item.currency in allowed_currencies:
            try:
                val = float(item.value)
                log.warning(
                    f"⚠️  No BASE row for {tag} in accountSummary — "
                    f"falling back to {item.currency} value {val}. "
                    f"Verify IBKR account config."
                )
                return val
            except (TypeError, ValueError):
                continue

    log.error(f"Could not resolve {tag} from accountSummary — returning 0.0 (conservative)")
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
    """Fetch bars and compute RSI, MAs, ATR(14) for stops, ATR(20) for sizing."""
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
    """
    Fetch the latest VIX daily close via CBOE index data. Returns None if
    unavailable — callers must fall back per B4 policy. Cached for
    REGIME_CACHE_TTL seconds.
    """
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
#  MARKET REGIME (SPY + VIX)  (B4)
# ══════════════════════════════════════════════════════════════════════════

def get_market_regime() -> Tuple[str, float]:
    """
    VIX-aware regime detector.

      BULL:    SPY > 200MA AND VIX < VIX_BULL_MAX
      CAUTION: SPY > 200MA AND VIX in [BULL_MAX, CAUTION_MAX_ABOVE]       (elevated but ok)
            OR SPY > 200MA AND VIX >  CAUTION_MAX_ABOVE                   (defensive)
            OR SPY ≤ 200MA AND VIX <  CAUTION_MAX_BELOW                   (pullback but tame)
      BEAR:    SPY ≤ 200MA AND VIX ≥ CAUTION_MAX_BELOW

    SAFETY (B4): When USE_VIX_IN_REGIME=True but the VIX fetch fails (CBOE
    data outage, market-data entitlement issue, connection blip), the
    fallback must NEVER return BULL. Historical precedent: during vol
    shocks (SVB March 2023, Aug 2024), VIX can spike >30 while SPY is still
    above its 50/200MA for 1–2 days. Old fallback would size 100% BULL
    into that. New fallback caps at CAUTION above 200MA and BEAR below —
    matches the stated conservative safety posture.

    If USE_VIX_IN_REGIME=False (explicit opt-out), the SPY-only 3-regime
    logic runs — this is a deliberate operator choice, distinct from a
    VIX fetch failure.
    """
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
        # Normal VIX-aware path
        if above_200 and vix < VIX_BULL_MAX:
            regime = "BULL"
        elif above_200 and vix <= VIX_CAUTION_MAX_ABOVE_200MA:
            regime = "CAUTION"
        elif above_200:
            regime = "CAUTION"  # SPY up but VIX elevated → defensive
        elif vix < VIX_CAUTION_MAX_BELOW_200MA:
            regime = "CAUTION"  # SPY down but VIX tame → still caution, not bear
        else:
            regime = "BEAR"
        extra = f" | VIX {vix:.2f}"
    elif USE_VIX_IN_REGIME:
        # SAFETY (B4): VIX was EXPECTED but unavailable. NEVER return BULL.
        # Cap at CAUTION above 200MA, BEAR below — conservative degraded mode.
        regime = "CAUTION" if above_200 else "BEAR"
        extra = " | VIX N/A (degraded: capped at CAUTION/BEAR)"
        log.warning(
            f"⚠️  VIX unavailable with USE_VIX_IN_REGIME=True — "
            f"regime capped at {regime} (no BULL allowed without VIX confirmation)"
        )
    else:
        # USE_VIX_IN_REGIME=False — operator has deliberately disabled VIX.
        # Run the original SPY-only 3-regime logic. Distinct from a VIX fetch
        # failure: this is a conscious configuration choice.
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
#  GUARDS  (B1)
# ══════════════════════════════════════════════════════════════════════════

def cash_guard_check() -> Tuple[bool, float, float]:
    """
    SAFETY (B1): Uses _resolve_account_value for deterministic BASE-currency
    resolution. Previous single-pass logic was non-deterministic w.r.t. IBKR's
    row order — could skip BASE and end up with a per-currency sub-account
    value, under-reporting NLV/cash and mis-sizing positions.
    """
    ib = get_ib()
    try:
        account_values = ib.accountSummary()
        cash = _resolve_account_value(account_values, "TotalCashValue")
        portfolio = _resolve_account_value(account_values, "NetLiquidation")

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


def _modify_tp_limit_price(tp_trade: Trade, new_limit_price: float) -> bool:
    """
    SAFETY (B2): Modify an existing TP limit order IN PLACE by re-placing it
    with the same orderId and an updated lmtPrice. IBKR treats same-orderId
    re-placement as an amendment, which means:
      - OCA group preserved  → trailing stop remains linked
      - Order stays live throughout → ZERO unprotected window
      - parentId / transmit flags untouched

    The previous cancel-and-replace approach opened a ~2-second window with
    no downside protection on every TP repair — this fires frequently on
    volatile names (TSLA, NVDA, HK stocks) where the fill price differs
    materially from the pre-placement estimate.

    Returns True on successful submission. Does NOT touch the trailing stop.
    """
    if tp_trade is None or tp_trade.order is None:
        return False
    ib = get_ib()
    try:
        tp_trade.order.lmtPrice = round(new_limit_price, 2)
        # Re-submit: same orderId, same contract → amendment, not a new order
        ib.placeOrder(tp_trade.contract, tp_trade.order)
        # Let the amendment land before returning
        ib.waitOnUpdate(timeout=1.5)
        return True
    except Exception as e:
        log.error(f"TP modify failed for {tp_trade.contract.symbol}: {e}")
        return False


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
        # Partial fill path — parent didn't complete. We must cancel-replace
        # the bracket because OCA qty cannot be modified in place; minimise
        # the window by placing the new bracket immediately after cancelling.
        log.warning(f"⚠️  {contract.symbol} PARTIAL fill {filled_qty}/{qty} — "
                    f"cancelling parent remainder and resizing bracket")
        _safe_cancel(parent_trade)
        _safe_cancel(tp_trade)
        _safe_cancel(trail_trade)
        ib.waitOnUpdate(timeout=1.5)

        actual_price = parent_trade.orderStatus.avgFillPrice or price
        new_tp, new_trail = _place_child_bracket_orders(
            contract, filled_qty, actual_price, trail_pct, tp_pct,
            oca_group_suffix=f"repair_{parent_trade.order.orderId}"
        )
        if new_tp is None or new_trail is None:
            log.critical(f"🚨 {contract.symbol}: partial-fill protective order failed — "
                         f"POSITION IS UNPROTECTED ({filled_qty} shares)")
        actual_qty = filled_qty
        tp_order_id = str(new_tp.order.orderId) if new_tp else None
        trail_order_id = str(new_trail.order.orderId) if new_trail else None

    else:
        actual_price = parent_trade.orderStatus.avgFillPrice or price
        actual_qty = int(parent_trade.orderStatus.filled or qty)
        tp_order_id = str(tp_trade.order.orderId)
        trail_order_id = str(trail_trade.order.orderId)

        # SAFETY (B2): Full fill path. If the TP limit price is materially off
        # vs the actual fill price, AMEND THE EXISTING TP in place via
        # _modify_tp_limit_price(). Do NOT cancel the OCA pair — cancelling
        # opens a window of no downside protection. Amending preserves OCA
        # linkage and keeps the position protected CONTINUOUSLY throughout
        # the repair.
        if REPAIR_TP_AFTER_FILL:
            current_tp_price = tp_trade.order.lmtPrice
            correct_tp = round(actual_price * (1 + tp_pct), 2)
            if current_tp_price and abs(correct_tp - current_tp_price) / current_tp_price > 0.002:
                log.info(f"  🔧 TP repair {contract.symbol}: ${current_tp_price:.2f} → "
                         f"${correct_tp:.2f} (in-place amend, trail untouched)")
                if _modify_tp_limit_price(tp_trade, correct_tp):
                    # tp_order_id unchanged — same order, amended in place
                    log.info(f"     ✅ TP amended in place (orderId {tp_order_id})")
                else:
                    # Amendment failed — position still has OLD TP + trail
                    # (protection is intact, just at a slightly off TP price).
                    # Log loudly so operator can intervene.
                    log.error(f"  ⚠️  TP amend failed for {contract.symbol}; "
                              f"leaving original TP @ ${current_tp_price:.2f} "
                              f"(position remains protected)")

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
    """
    Market sell. Returns dict with order_id, filled_qty, requested_qty, and
    avg_fill_price (used by the main loop to record closed-trade PnL).
    """
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
#  EXECUTION LOOKUP — for trade-history reconciliation  (B3)
# ══════════════════════════════════════════════════════════════════════════

def get_recent_sell_fill(contract: Stock,
                         lookback_seconds: int = 86400,
                         opened_after: Optional[datetime] = None) -> Optional[dict]:
    """
    Find SELL executions for this contract within a bounded window and combine
    all qualifying partial fills into a single exit record.

    SAFETY (B3): Previously this returned only the most recent fill, which:
      (a) under-reported qty when bracket fills split into 2+ partials
          (common on thin liquidity: HK small caps, ASX, low-volume ETFs)
      (b) risked pulling a stale fill from an EARLIER trade on the same
          contract within the 24h lookback — attributing last trade's exit
          to this trade's close, corrupting trade_history PnL.

    Fix:
      - If opened_after is provided (from pos["opened_at"]), use it as the
        hard lower bound. Fills before this timestamp CANNOT belong to the
        current position and are excluded.
      - Sum all qualifying fills; compute weighted-average exit price.

    Returns {"price": weighted_avg_px, "qty": total_qty,
             "exec_id": latest_exec_id, "time": latest_exec_time}
    or None if nothing qualifies.
    """
    ib = get_ib()
    try:
        fills = ib.fills()
    except Exception as e:
        log.warning(f"ib.fills() failed for {contract.symbol}: {e}")
        return None

    # Floor: position open time (if known) wins over the generic lookback.
    # Critical change — prevents cross-trade fill contamination.
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

    # Sum all partials; weighted-average price.
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
#  BRACKET RE-ATTACHMENT
# ══════════════════════════════════════════════════════════════════════════

def has_protective_orders(contract: Stock) -> bool:
    ib = get_ib()
    try:
        for t in ib.openTrades():
            if t.contract.conId == contract.conId and t.order.action == "SELL":
                return True
    except Exception as e:
        log.warning(f"has_protective_orders error for {contract.symbol}: {e}")
    return False


def attach_bracket_to_existing_position(
    contract: Stock, qty: int, entry_ref_price: float,
    trail_pct: float = DEFAULT_TRAILING_STOP,
    tp_pct: float = DEFAULT_TAKE_PROFIT
) -> Tuple[Optional[str], Optional[str]]:
    if has_protective_orders(contract):
        log.info(f"   ℹ️  {contract.symbol} already has protective orders — skipping attach")
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
    """
    SAFETY (B1): Uses _resolve_account_value for deterministic BASE-currency
    resolution for every requested tag. The main loop reads NLV from here
    for day rollover, loss limits, and DD tracking — every one of which
    depends on this being correct and deterministic.
    """
    ib = get_ib()
    summary = ib.accountSummary()
    wanted = ("NetLiquidation", "TotalCashValue", "BuyingPower",
              "UnrealizedPnL", "RealizedPnL")
    result = {}
    # Pre-compute the set of tags actually present so we only populate
    # the result dict for real tags (preserves caller .get(tag, 0) semantics).
    present_tags = {item.tag for item in summary}
    for tag in wanted:
        if tag in present_tags:
            result[tag] = _resolve_account_value(summary, tag)
    return result


def flatten_all_positions(reason: str = "emergency"):
    log.critical(f"🚨 FLATTEN ALL triggered — reason: {reason}")
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
            sell_stock(contract, qty)
        except Exception as e:
            log.error(f"Flatten failed for {sym}: {e}")
