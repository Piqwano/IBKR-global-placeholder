"""
IBKR Helpers — Connection, Data, Bracket Orders, Guards
=========================================================
Thread-safe connection management, event-driven trade waits with partial-fill
handling, bracket orders with server-side TP + trailing stop, market-hours
gating (including lunch breaks), and atomic post-fill TP repair.
"""

import asyncio
import logging
import math
import threading
import time as _time
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, List
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# ──────────────────────────────────────────────────────────────────────────
# CRITICAL: initialise an event loop BEFORE ib_insync is imported/constructed.
# On Python 3.12+ and some 3.11 setups, IB() construction will fail otherwise.
# ──────────────────────────────────────────────────────────────────────────
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ib_insync import IB, Stock, MarketOrder, LimitOrder, Order, Trade, util

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
    ATR_PERIOD, USE_BRACKET_ORDERS, REPAIR_TP_AFTER_FILL,
    DEFAULT_TRAILING_STOP, DEFAULT_TAKE_PROFIT,
)

log = logging.getLogger("ibkr-rsi")


# ══════════════════════════════════════════════════════════════════════════
#  THREAD-SAFE CONNECTION MANAGER
# ══════════════════════════════════════════════════════════════════════════

class IBConnectionManager:
    """Singleton IB connection. Thread-safe, auto-reconnecting."""

    def __init__(self):
        self._ib: Optional[IB] = None
        self._lock = threading.RLock()
        self._contract_cache: Dict[str, Stock] = {}
        self._regime_cache: Optional[Tuple[str, float]] = None
        self._regime_time: float = 0.0
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


_manager = IBConnectionManager()


def get_ib() -> IB:
    return _manager.get()


def disconnect():
    _manager.disconnect()


def is_connected() -> bool:
    return _manager.is_connected()


# ══════════════════════════════════════════════════════════════════════════
#  MARKET HOURS (with lunch-break support)
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

    # Lunch break (e.g. SEHK 12:00-13:00 HKT)
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

def get_daily_bars(contract: Stock, days: int = BARS_FOR_RSI) -> Optional[pd.DataFrame]:
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
        log.warning(f"Bars error {contract.symbol}/{contract.exchange}: {e}")
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


def analyze(contract: Stock) -> Optional[dict]:
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

    atr = calc_atr(highs, lows, closes) if len(closes) > ATR_PERIOD else None

    return {
        "rsi": rsi, "price": price,
        "vol_ok": vol_ok, "trend_ok": trend_ok, "ma20_ok": ma20_ok,
        "ma20": round(ma20, 4), "ma50": round(ma50, 4), "ma200": round(ma200, 4),
        "avg_volume": avg_vol, "atr": atr,
    }


# ══════════════════════════════════════════════════════════════════════════
#  MARKET REGIME
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

    if price > ma50 and price > ma200:
        regime = "BULL"
    elif price > ma200:
        regime = "CAUTION"
    else:
        regime = "BEAR"

    mult = REGIME_SIZE_MULTIPLIERS[regime]
    emoji = {"BULL": "🟢", "CAUTION": "🟡", "BEAR": "🔴"}[regime]
    log.info(f"{emoji} Market regime: {regime} | SPY ${price:.2f} | 50MA ${ma50:.2f} | 200MA ${ma200:.2f}")

    result = (regime, mult)
    _manager.set_regime_cache(result)
    return result


# ══════════════════════════════════════════════════════════════════════════
#  GUARDS
# ══════════════════════════════════════════════════════════════════════════

def cash_guard_check() -> Tuple[bool, float, float]:
    ib = get_ib()
    try:
        account_values = ib.accountSummary()
        cash = 0.0
        portfolio = 0.0
        cash_base, port_base = False, False
        for item in account_values:
            if item.tag == "TotalCashValue":
                if item.currency == "BASE":
                    cash = float(item.value); cash_base = True
                elif not cash_base and item.currency in ("USD", "AUD", "CAD", "SGD", "GBP", "EUR"):
                    cash = float(item.value)
            elif item.tag == "NetLiquidation":
                if item.currency == "BASE":
                    portfolio = float(item.value); port_base = True
                elif not port_base and item.currency in ("USD", "AUD", "CAD", "SGD", "GBP", "EUR"):
                    portfolio = float(item.value)

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
    """
    Wait for a trade to reach a terminal state.
    Returns (fully_filled, filled_qty).
    """
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
    """Cancel an order, swallowing any exception."""
    ib = get_ib()
    try:
        order = trade_or_order.order if hasattr(trade_or_order, 'order') else trade_or_order
        ib.cancelOrder(order)
    except Exception:
        pass


def _place_child_bracket_orders(contract: Stock, qty: int, entry_ref_price: float,
                                trail_pct: float, tp_pct: float,
                                oca_group_suffix: str = "") -> Tuple[Optional[Trade], Optional[Trade]]:
    """
    Place TP + trailing-stop as a standalone OCA pair.
    Used for bracket repair (partial fill resize / TP repair) and naked-position attach.
    Cleans up orphans if one placement fails.
    """
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
        # Clean up whatever we managed to place
        if tp_trade is not None:
            _safe_cancel(tp_trade)
        if trail_trade is not None:
            _safe_cancel(trail_trade)
        return None, None


# ══════════════════════════════════════════════════════════════════════════
#  BRACKET ORDER (parent + OCA children)
# ══════════════════════════════════════════════════════════════════════════

def _place_bracket(contract: Stock, qty: int, entry_price_est: float,
                   trail_pct: float, tp_pct: float) -> Optional[Tuple[Trade, Trade, Trade]]:
    """
    Parent market BUY (transmit=False) + OCA TP limit + OCA trailing stop (transmit=True).
    All three submit atomically. Cleans up orphans if placement fails mid-sequence.
    """
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
        # Clean up any orphans
        for t in (parent_trade, tp_trade, trail_trade):
            if t is not None:
                _safe_cancel(t)
        return None


def buy_stock_bracket(contract: Stock, amount: float,
                      trail_pct: float, tp_pct: float) -> Optional[dict]:
    """
    Buy with bracket. Handles full fill, partial fill (resize bracket), and TP repair.
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

    # ── Case 1: zero fill (reject/cancel) ────────────────────────────────
    if not filled and filled_qty == 0:
        reason = parent_trade.log[-1].message if parent_trade.log else "Unknown"
        status = parent_trade.orderStatus.status
        log.error(f"❌ BUY {contract.symbol} REJECTED — {status}: {reason}")
        _safe_cancel(tp_trade)
        _safe_cancel(trail_trade)
        return None

    # ── Case 2: partial fill — resize bracket children to filled qty ─────
    if not filled and filled_qty > 0:
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

    # ── Case 3: full fill — optionally repair TP price ───────────────────
    else:
        actual_price = parent_trade.orderStatus.avgFillPrice or price
        actual_qty = int(parent_trade.orderStatus.filled or qty)
        tp_order_id = str(tp_trade.order.orderId)
        trail_order_id = str(trail_trade.order.orderId)

        if REPAIR_TP_AFTER_FILL:
            current_tp_price = tp_trade.order.lmtPrice
            correct_tp = round(actual_price * (1 + tp_pct), 2)
            # Only repair if drift is meaningful (>0.2%)
            if current_tp_price and abs(correct_tp - current_tp_price) / current_tp_price > 0.002:
                log.info(f"  🔧 TP repair {contract.symbol}: ${current_tp_price:.2f} → ${correct_tp:.2f}")
                try:
                    # Cancel BOTH old orders first — avoids window with duplicate
                    # trailing stops active across different OCA groups.
                    _safe_cancel(tp_trade)
                    _safe_cancel(trail_trade)
                    ib.waitOnUpdate(timeout=2.0)
                    # Position is briefly unprotected here (~2s). Acceptable trade-off
                    # vs keeping two trailing stops racing in parallel.
                    new_tp, new_trail = _place_child_bracket_orders(
                        contract, actual_qty, actual_price, trail_pct, tp_pct,
                        oca_group_suffix=f"tprepair_{parent_trade.order.orderId}"
                    )
                    if new_tp and new_trail:
                        tp_order_id = str(new_tp.order.orderId)
                        trail_order_id = str(new_trail.order.orderId)
                    else:
                        log.critical(f"🚨 {contract.symbol}: TP repair failed to place "
                                     f"new orders — POSITION IS UNPROTECTED")
                        tp_order_id = None
                        trail_order_id = None
                except Exception as e:
                    log.warning(f"TP repair failed for {contract.symbol}: {e}")

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
    """Plain market buy without bracket. Only used if USE_BRACKET_ORDERS=False."""
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
    Market sell. Returns dict with order_id and filled_qty (or None on total reject).
    On partial fill, cancels the remainder so it doesn't linger.
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

        log.info(f"🔴 SELL {contract.symbol:<8} qty:{filled_qty}/{qty} | {contract.exchange} | "
                 f"Order:{trade.order.orderId} @ ${trade.orderStatus.avgFillPrice}")
        return {
            "order_id": str(trade.order.orderId),
            "filled_qty": filled_qty,
            "requested_qty": qty,
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
#  BRACKET RE-ATTACHMENT
# ══════════════════════════════════════════════════════════════════════════

def has_protective_orders(contract: Stock) -> bool:
    """Check if there's any resting SELL order on this contract (TP or stop)."""
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
    """
    Attach an OCA TP + trailing-stop to an existing naked long position.
    Returns (tp_order_id, trail_order_id) or (None, None).
    """
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
#  PORTFOLIO
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
    result = {}
    wanted = ("NetLiquidation", "TotalCashValue", "BuyingPower",
              "UnrealizedPnL", "RealizedPnL")
    for item in summary:
        if item.tag in wanted:
            if item.currency == "BASE":
                result[item.tag] = float(item.value)
            elif item.tag not in result and item.currency in ("USD", "AUD", "CAD", "SGD", "GBP", "EUR"):
                result[item.tag] = float(item.value)
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
