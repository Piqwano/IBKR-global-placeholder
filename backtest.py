"""
Backtest — Global RSI Bot
===========================
Historical simulation that mirrors the live bot's signal + sizing logic.
Uses yfinance for data.

Fill modes (config.BACKTEST_FILL_MODE):
  - "next_open":  signal on day T fills at T+1 open (rigorous, no look-ahead)
  - "same_close": signal on day T fills at T's close (simpler, slightly optimistic)

Alignment with live bot (v2.3.1 backtest hardening):
  - Wilder RSI/ATR math matches ibkr_helpers.calc_rsi / calc_atr exactly
    (SMA seed + recursion — previous pandas .ewm seed diverged on the first
    ~30 bars).
  - Position sizing applies the same volatility-adjusted scalar as
    global_rsi_bot._apply_vol_scalar (target VOL_TARGET_ANNUAL, clamped to
    [VOL_SCALAR_MIN, VOL_SCALAR_MAX]).
  - New buys are blocked when projected gross exposure exceeds
    MAX_GROSS_EXPOSURE_PCT of equity — mirrors try_buy's exposure ceiling.
  - Trailing-stop / take-profit fills use intraday high/low instead of just
    the close, and account for overnight gap-through:
        stop fires if low ≤ stop_level    → fill = min(stop_level, open)
        tp   fires if high ≥ tp_level     → fill = max(tp_level, open)
  - On a BEAR regime day, prior-day pending entries are dropped rather than
    silently filling; the live bot's scan_all_markets gates by regime too.
  - MAX_COMMISSION_PCT from config replaces the previously hard-coded 0.03.

Realistic costs: per-exchange commission + per-exchange bps slippage,
tracked symmetrically on entry AND exit.

FX-aware: positions are priced in their native currency; cash, equity, and
reported P&L are in the chosen base currency (--base-currency USD|AUD,
default USD). Non-base currencies pull FX series from yfinance (AUDUSD=X
etc.), forward-filled across weekends/holidays. Commissions are assumed
to already be in the base currency, matching the live bot's convention.
If your base is not USD or AUD, verify the EXCHANGE_COMMISSIONS constants
make sense for it.

Run:
    python backtest.py                                   # 3-year backtest
    python backtest.py --start 2020-01-01 --end 2024-12-31
    python backtest.py --capital 10000 --symbols AAPL MSFT NVDA
    python backtest.py --base-currency USD              # or AUD
"""

import argparse
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    print("Please `pip install yfinance` to run the backtest.")
    sys.exit(1)

from config import (
    STOCK_UNIVERSE, ASSET_CONFIG, CORR_GROUPS, MAX_PER_CORR_GROUP,
    RSI_PERIOD, RSI_OVERSOLD, RSI_OVERBOUGHT,
    USE_VOLUME_FILTER, USE_TREND_FILTER, USE_MA20_FILTER,
    POSITION_SIZE_PCT, MAX_POSITIONS, CASH_RESERVE_PCT,
    DEFAULT_TRAILING_STOP, DEFAULT_TAKE_PROFIT,
    REGIME_SIZE_MULTIPLIERS,
    EXCHANGE_COMMISSIONS, SLIPPAGE_BPS,
    SEHK_LOT_SIZES, ATR_PERIOD,
    DAILY_LOSS_LIMIT_PCT, MAX_DRAWDOWN_PCT,
    USE_ATR_STOPS, ATR_MULTIPLIER,
    BACKTEST_FILL_MODE,
    USE_VOL_ADJUSTED_SIZING, VOL_TARGET_ANNUAL,
    ATR_PERIOD_FOR_SIZING, VOL_SCALAR_MIN, VOL_SCALAR_MAX,
    MAX_GROSS_EXPOSURE_PCT, MAX_COMMISSION_PCT,
)

# ══════════════════════════════════════════════════════════════════════════
#  YFINANCE SYMBOL MAPPING
# ══════════════════════════════════════════════════════════════════════════

YF_SUFFIX = {
    "SMART": "",
    "ASX":   ".AX",
    "LSE":   ".L",
    "IBIS":  ".DE",
    "SBF":   ".PA",
    "AEB":   ".AS",
    "SEHK":  ".HK",
    "SGX":   ".SI",
}


def yf_ticker(symbol: str, exchange: str) -> str:
    if exchange == "SEHK":
        return f"{int(symbol):04d}.HK"
    return symbol.replace(".", "-") + YF_SUFFIX.get(exchange, "")


# ══════════════════════════════════════════════════════════════════════════
#  FX DATA (external-review follow-up: FX-aware P&L)
# ══════════════════════════════════════════════════════════════════════════

def yf_fx_ticker(from_ccy: str, to_ccy: str) -> str:
    """yfinance FX ticker: 'AUDUSD=X' means USD per 1 AUD."""
    return f"{from_ccy}{to_ccy}=X"


def load_fx_data(base_ccy: str, currencies: List[str],
                 start: str, end: str,
                 trading_index: Optional[pd.DatetimeIndex] = None
                 ) -> Dict[str, pd.Series]:
    """
    Return {ccy: Series[date → rate(ccy→base_ccy)]}.
    Base currency maps to a constant 1.0 series. Non-base pairs pull
    yfinance FX and are forward-filled across weekends/holidays so every
    trading date has a valid rate.

    If a pair fails to load, fall back to a constant 1.0 series and log a
    loud warning — the backtest will still run but P&L for that currency
    won't be FX-corrected.
    """
    rates: Dict[str, pd.Series] = {}
    needed = {c for c in currencies if c and c != base_ccy}

    # Helper: build a trading-day index if one wasn't provided
    if trading_index is None:
        # Approximate — 5-day business days between start and end
        trading_index = pd.bdate_range(start=start, end=end)

    for ccy in needed:
        tkr = yf_fx_ticker(ccy, base_ccy)
        try:
            fx = yf.download(tkr, start=start, end=end, progress=False, auto_adjust=True)
            if fx.empty:
                raise ValueError("empty FX dataframe")
            if isinstance(fx.columns, pd.MultiIndex):
                fx.columns = fx.columns.get_level_values(0)
            fx.columns = [c.lower() for c in fx.columns]
            series = fx["close"].astype(float)
            # Reindex onto trading_index, forward-fill across missing days
            series = series.reindex(trading_index).ffill().bfill()
            if series.isna().any():
                raise ValueError("FX series has unfillable NaNs")
            rates[ccy] = series
            print(f"   ✅ FX {ccy}→{base_ccy}: {len(series)} days, last rate {series.iloc[-1]:.4f}")
        except Exception as e:
            print(
                f"   ⚠️  FX {ccy}→{base_ccy} ({tkr}) failed: {e} — "
                f"falling back to 1.0 (P&L for {ccy} positions WILL BE WRONG)"
            )
            rates[ccy] = pd.Series(1.0, index=trading_index)

    # Base currency is always 1.0 vs itself
    rates[base_ccy] = pd.Series(1.0, index=trading_index)
    return rates


def fx_rate(rates: Dict[str, pd.Series], ccy: str, date: pd.Timestamp) -> float:
    """Lookup rate; fall back to 1.0 if ccy or date missing (shouldn't happen
    after load_fx_data ffill, but defensive)."""
    s = rates.get(ccy)
    if s is None:
        return 1.0
    if date in s.index:
        return float(s.loc[date])
    # Use the last-known rate before `date`
    prior = s.loc[:date]
    if len(prior) == 0:
        return 1.0
    return float(prior.iloc[-1])


# ══════════════════════════════════════════════════════════════════════════
#  INDICATORS (Wilder RSI — matches live calc_rsi)
# ══════════════════════════════════════════════════════════════════════════

def _wilder_smooth(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder smoothing that exactly matches ibkr_helpers.calc_rsi / calc_atr:
    seed = SMA of the first `period` observations, then recursion
    smoothed_i = (smoothed_{i-1} * (period - 1) + values_i) / period."""
    out = np.full_like(values, np.nan, dtype=float)
    if len(values) < period:
        return out
    seed = np.mean(values[:period])
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


def rsi_series(closes: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Matches ibkr_helpers.calc_rsi exactly (Wilder SMA seed + recursion).

    The previous implementation used pandas `ewm(adjust=False)` which lacks
    the SMA warm-up, producing RSI values that diverged from the live bot
    by ~1–3 points in the first ~30 bars. Aligning the seed eliminates a
    material cause of backtest→live signal drift.
    """
    delta = closes.diff().to_numpy()
    # calc_rsi operates on np.diff(closes), so index 0 of `delta` is np.nan.
    deltas = delta[1:]
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = _wilder_smooth(gains, period)
    avg_loss = _wilder_smooth(losses, period)
    rs = np.divide(
        avg_gain, avg_loss,
        out=np.full_like(avg_gain, np.nan),
        where=avg_loss > 0,
    )
    rsi_vals = 100.0 - (100.0 / (1.0 + rs))
    # Where losses were all zero, live calc_rsi returns 100.
    zero_loss = (avg_loss == 0) & ~np.isnan(avg_gain)
    rsi_vals = np.where(zero_loss, 100.0, rsi_vals)
    # Prepend NaN for the diff() we dropped so index aligns back with closes.
    out = pd.Series(np.concatenate([[np.nan], rsi_vals]), index=closes.index)
    return out.fillna(50)


def atr_series(high: pd.Series, low: pd.Series, close: pd.Series,
               period: int = ATR_PERIOD) -> pd.Series:
    """Matches ibkr_helpers.calc_atr (Wilder smoothing of True Range)."""
    prev_close = close.shift(1)
    tr_df = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1)
    tr = tr_df.max(axis=1).to_numpy()
    # calc_atr operates on tr starting at index 1 (i.e. drops the first row
    # where prev_close is NaN). Mirror that shape.
    tr_valid = tr[1:]
    smoothed = _wilder_smooth(tr_valid, period)
    out = pd.Series(np.concatenate([[np.nan], smoothed]), index=close.index)
    return out


# ══════════════════════════════════════════════════════════════════════════
#  DATA LOADING
# ══════════════════════════════════════════════════════════════════════════

def load_data(symbols: List[Tuple[str, str, str, str]], start: str, end: str) -> Dict[str, pd.DataFrame]:
    data = {}
    print(f"📥 Downloading {len(symbols)} symbols from yfinance...")
    for symbol, exchange, currency, name in symbols:
        yt = yf_ticker(symbol, exchange)
        try:
            df = yf.download(yt, start=start, end=end, progress=False, auto_adjust=True)
            if df.empty or len(df) < RSI_PERIOD + 30:
                print(f"   ⚠️  {symbol} ({yt}): insufficient data, skipping")
                continue

            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df.columns = [c.lower() for c in df.columns]

            df["rsi"] = rsi_series(df["close"])
            df["ma20"] = df["close"].rolling(20, min_periods=1).mean()
            df["ma200"] = df["close"].rolling(200, min_periods=1).mean()
            df["vol_avg20"] = df["volume"].rolling(20, min_periods=1).mean()
            df["atr"] = atr_series(df["high"], df["low"], df["close"])
            # ATR for sizing — matches live `analyze()` which computes two
            # ATRs: short (ATR_PERIOD for stops) and long (ATR_PERIOD_FOR_SIZING
            # for the volatility-adjusted position scalar).
            df["atr_sizing"] = atr_series(
                df["high"], df["low"], df["close"],
                period=ATR_PERIOD_FOR_SIZING,
            )
            df["exchange"] = exchange
            df["currency"] = currency
            df["name"] = name
            df["next_open"] = df["open"].shift(-1)

            data[symbol] = df.dropna(subset=["rsi"])
        except Exception as e:
            print(f"   ⚠️  {symbol} ({yt}) failed: {e}")

    coverage = len(data) / max(len(symbols), 1)
    print(f"✅ Loaded data for {len(data)}/{len(symbols)} symbols "
          f"({coverage*100:.0f}% coverage)")
    if coverage < 0.5:
        print(f"   🚨 WARNING: <50% of universe loaded — results will not be "
              f"representative. Check yfinance availability for missing tickers.")
    return data


def load_regime_data(start: str, end: str) -> pd.DataFrame:
    spy = yf.download("SPY", start=start, end=end, progress=False, auto_adjust=True)
    if isinstance(spy.columns, pd.MultiIndex):
        spy.columns = spy.columns.get_level_values(0)
    spy.columns = [c.lower() for c in spy.columns]
    spy["ma50"] = spy["close"].rolling(50, min_periods=1).mean()
    spy["ma200"] = spy["close"].rolling(200, min_periods=1).mean()

    def regime(row):
        if row["close"] > row["ma50"] and row["close"] > row["ma200"]:
            return "BULL"
        if row["close"] > row["ma200"]:
            return "CAUTION"
        return "BEAR"

    spy["regime"] = spy.apply(regime, axis=1)
    return spy[["close", "regime"]]


# ══════════════════════════════════════════════════════════════════════════
#  BACKTEST STATE
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class Position:
    symbol: str
    exchange: str
    currency: str                  # native contract ccy; prices below are in this ccy
    entry_date: pd.Timestamp
    entry_price: float             # native currency
    qty: int
    peak: float                    # native currency
    trailing_stop_pct: float
    take_profit_pct: float
    rsi_exit: float
    entry_commission: float        # base currency (assumed; see module docstring)
    entry_slippage: float          # base currency

    def current_stop(self) -> float:
        return self.peak * (1 - self.trailing_stop_pct)

    def tp_price(self) -> float:
        return self.entry_price * (1 + self.take_profit_pct)


@dataclass
class Trade:
    symbol: str
    exchange: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: int
    pnl: float
    pnl_pct: float
    reason: str
    commission: float     # entry + exit (symmetric)
    slippage: float       # entry + exit (symmetric)


@dataclass
class Backtester:
    """
    cash, equity_curve, trade.pnl, peak_equity, day_start_equity are all
    in BASE currency. Position.entry_price/peak are in NATIVE currency.
    Conversions happen at open, mark-to-market, and close via fx_rates.
    """
    starting_capital: float
    base_currency: str = "USD"
    fx_rates: Dict[str, pd.Series] = field(default_factory=dict)
    fill_mode: str = "next_open"
    cash: float = 0.0
    positions: Dict[str, Position] = field(default_factory=dict)
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[Tuple[pd.Timestamp, float]] = field(default_factory=list)
    peak_equity: float = 0.0
    day_start_equity: float = 0.0
    last_day: Optional[pd.Timestamp] = None
    halted: bool = False
    pending_entries: List[dict] = field(default_factory=list)
    # Observability — counts of why candidate entries were rejected. Mirrors
    # the reasons try_buy / scan_all_markets log in the live bot so backtest
    # ergonomics match operator-facing diagnostics.
    rejection_counts: Dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        self.cash = self.starting_capital
        self.peak_equity = self.starting_capital
        self.day_start_equity = self.starting_capital

    # ── FX helpers ────────────────────────────────────────────────────
    def fx(self, ccy: str, date: pd.Timestamp) -> float:
        """Rate converting 1 unit of `ccy` into base currency on `date`."""
        return fx_rate(self.fx_rates, ccy, date)

    def native_to_base(self, native_amount: float, ccy: str, date: pd.Timestamp) -> float:
        return native_amount * self.fx(ccy, date)

    def base_to_native(self, base_amount: float, ccy: str, date: pd.Timestamp) -> float:
        rate = self.fx(ccy, date)
        return base_amount / rate if rate > 0 else base_amount

    def calc_qty(self, exchange: str, symbol: str, amount: float, price: float) -> int:
        """amount and price both in native currency."""
        if exchange == "SGX":
            return ((int(amount / price)) // 100) * 100
        if exchange == "SEHK":
            lot = SEHK_LOT_SIZES.get(symbol, 100)
            return ((int(amount / price)) // lot) * lot
        return int(amount / price)

    def costs(self, exchange: str, notional_native: float, ccy: str,
              date: pd.Timestamp) -> Tuple[float, float]:
        """
        Commission: assumed in BASE currency (matches live/README convention).
        Slippage: computed from notional which is converted to base here.
        Returns (commission_base, slippage_base).
        """
        commission_base = EXCHANGE_COMMISSIONS.get(exchange, 5.0)
        notional_base = self.native_to_base(notional_native, ccy, date)
        slippage_base = notional_base * (SLIPPAGE_BPS.get(exchange, 10) / 10_000)
        return commission_base, slippage_base

    def mark_to_market(self, data: Dict[str, pd.DataFrame], today: pd.Timestamp) -> float:
        """Sum of cash (base) + each position value in native × FX rate to base."""
        equity = self.cash
        for sym, pos in self.positions.items():
            df = data.get(sym)
            if df is not None and today in df.index:
                native_value = pos.qty * df.loc[today, "close"]
            else:
                native_value = pos.qty * pos.entry_price
            equity += self.native_to_base(native_value, pos.currency, today)
        return equity

    def gross_exposure_base(self, data: Dict[str, pd.DataFrame],
                            today: pd.Timestamp) -> float:
        """Sum of position notionals (qty × last-known close) in base currency.

        Mirrors global_rsi_bot._current_gross_exposure_base so the backtest
        enforces MAX_GROSS_EXPOSURE_PCT the same way live does.
        """
        total = 0.0
        for sym, pos in self.positions.items():
            df = data.get(sym)
            if df is not None and today in df.index:
                mark_native = float(df.loc[today, "close"])
            else:
                mark_native = pos.entry_price
            if mark_native <= 0:
                continue
            total += self.native_to_base(
                pos.qty * mark_native, pos.currency, today,
            )
        return total

    def open_position(self, symbol: str, exchange: str, currency: str,
                      day: pd.Timestamp,
                      fill_price_native: float, amount_base: float,
                      cfg: dict) -> bool:
        """
        fill_price_native: price of the stock in its native currency
        amount_base: target investment amount in BASE currency
        """
        # Convert the target base-amount into native for quantity calc
        amount_native = self.base_to_native(amount_base, currency, day)
        qty = self.calc_qty(exchange, symbol, amount_native, fill_price_native)
        if qty < 1:
            return False

        notional_native = qty * fill_price_native
        commission_base, slippage_base = self.costs(exchange, notional_native, currency, day)

        # Apply slippage in native units (price moves against us)
        slippage_mult = 1 + SLIPPAGE_BPS.get(exchange, 10) / 10_000
        effective_price_native = fill_price_native * slippage_mult

        # Total cost in BASE: (qty × effective_native × fx) + commission_base
        total_cost_base = self.native_to_base(qty * effective_price_native, currency, day) + commission_base
        if total_cost_base > self.cash:
            return False

        self.cash -= total_cost_base
        self.positions[symbol] = Position(
            symbol=symbol, exchange=exchange, currency=currency,
            entry_date=day, entry_price=effective_price_native,
            qty=qty, peak=effective_price_native,
            trailing_stop_pct=cfg["trailing_stop"],
            take_profit_pct=cfg["take_profit"],
            rsi_exit=cfg["rsi_exit"],
            entry_commission=commission_base,
            entry_slippage=slippage_base,
        )
        return True

    def close_position(self, symbol: str, day: pd.Timestamp,
                       price_native: float, reason: str):
        pos = self.positions.pop(symbol)
        notional_native = pos.qty * price_native
        commission_base, slippage_base = self.costs(pos.exchange, notional_native, pos.currency, day)

        slippage_mult = 1 - SLIPPAGE_BPS.get(pos.exchange, 10) / 10_000
        effective_price_native = price_native * slippage_mult

        proceeds_base = self.native_to_base(pos.qty * effective_price_native, pos.currency, day) - commission_base
        self.cash += proceeds_base

        # PnL attribution in base currency.
        #  - entry cost base: qty × entry_price_native × fx(entry_date) + entry_commission_base
        #  - exit proceeds base: proceeds_base (already net of exit commission)
        #  - pnl_base = exit_proceeds - entry_cost
        entry_cost_base = (
            self.native_to_base(pos.qty * pos.entry_price, pos.currency, pos.entry_date)
            + pos.entry_commission
        )
        pnl = proceeds_base - entry_cost_base

        # pnl_pct is still meaningful as a NATIVE-price return — independent of FX.
        pnl_pct = (effective_price_native - pos.entry_price) / pos.entry_price * 100

        self.trades.append(Trade(
            symbol=symbol, exchange=pos.exchange,
            entry_date=pos.entry_date, exit_date=day,
            entry_price=pos.entry_price, exit_price=effective_price_native,
            qty=pos.qty, pnl=pnl, pnl_pct=pnl_pct,
            reason=reason,
            commission=pos.entry_commission + commission_base,
            slippage=pos.entry_slippage + slippage_base,
        ))


# ══════════════════════════════════════════════════════════════════════════
#  VOLATILITY-ADJUSTED SIZING (mirrors global_rsi_bot._apply_vol_scalar)
# ══════════════════════════════════════════════════════════════════════════

def _apply_vol_scalar(base_amount: float, atr_sizing: Optional[float],
                      price: float) -> float:
    """Exact port of global_rsi_bot._apply_vol_scalar so backtest sizing
    matches live sizing. Without this, the backtest systematically
    over-sizes high-vol names and under-sizes low-vol names relative to
    what the live bot would do.
    """
    if not USE_VOL_ADJUSTED_SIZING:
        return base_amount
    if atr_sizing is None or not price or price <= 0:
        return base_amount
    if not math.isfinite(atr_sizing) or atr_sizing <= 0:
        return base_amount

    atr_pct = atr_sizing / price
    annualised_vol = atr_pct * math.sqrt(252)
    if annualised_vol <= 0:
        return base_amount

    raw_scalar = VOL_TARGET_ANNUAL / annualised_vol
    vol_scalar = max(VOL_SCALAR_MIN, min(VOL_SCALAR_MAX, raw_scalar))
    return round(base_amount * vol_scalar, 2)


# ══════════════════════════════════════════════════════════════════════════
#  MAIN BACKTEST
# ══════════════════════════════════════════════════════════════════════════

def _bump(counts: Dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def run_backtest(data: Dict[str, pd.DataFrame], regime_df: pd.DataFrame,
                 fx_rates: Dict[str, pd.Series],
                 starting_capital: float,
                 base_currency: str = "USD",
                 fill_mode: str = BACKTEST_FILL_MODE) -> Backtester:

    bt = Backtester(
        starting_capital=starting_capital,
        base_currency=base_currency,
        fx_rates=fx_rates,
        fill_mode=fill_mode,
    )

    all_dates = set(regime_df.index)
    for df in data.values():
        all_dates |= set(df.index)
    all_dates = sorted(all_dates)

    for today in all_dates:
        if bt.last_day is None or today.date() != bt.last_day.date():
            bt.day_start_equity = bt.mark_to_market(data, today)
        bt.last_day = today

        # Today's regime is needed twice (entry gate and pending-fill gate).
        today_regime = None
        today_mult = 0.0
        if today in regime_df.index:
            today_regime = regime_df.loc[today, "regime"]
            today_mult = REGIME_SIZE_MULTIPLIERS[today_regime]

        # ── 0. Process pending entries from PREVIOUS bar (next_open mode) ──
        # Live behaviour: try_buy checks regime / daily-halt / max-DD before
        # placing the order. A BULL-day signal that wakes up on a BEAR-day
        # would NOT fill in live, because scan_all_markets gates by regime
        # first. Enforce the same gating here.
        if fill_mode == "next_open" and bt.pending_entries:
            pending_gated = bt.halted or today_mult == 0
            if not pending_gated:
                for pe in bt.pending_entries:
                    sym = pe["symbol"]
                    if sym in bt.positions:
                        continue
                    if len(bt.positions) >= MAX_POSITIONS:
                        continue
                    df = data.get(sym)
                    if df is None or today not in df.index:
                        continue
                    fill_price = float(df.loc[today, "open"])
                    if math.isnan(fill_price):
                        continue
                    bt.open_position(
                        sym, pe["exchange"], pe["currency"], today,
                        fill_price, pe["amount"], pe["cfg"],
                    )
            bt.pending_entries = []

        # ── 1. Check exits on open positions ──────────────────────────────
        # Realistic fills: use intraday high/low, account for gap-through on
        # the stop (open < stop) and gap-up on TP (open > tp). Peak tracks
        # the daily HIGH (not close) so the trailing stop behaves like the
        # server-side IBKR trail that sees ticks, not close-prints.
        for sym in list(bt.positions.keys()):
            df = data.get(sym)
            if df is None or today not in df.index:
                continue
            row = df.loc[today]
            open_p = float(row["open"])
            high_p = float(row["high"])
            low_p = float(row["low"])
            close_p = float(row["close"])
            pos = bt.positions[sym]

            # Peak update BEFORE the stop check — the intraday high becomes
            # the new peak candidate for today's trail calc.
            if high_p > pos.peak:
                pos.peak = high_p
            stop_level = pos.current_stop()
            tp_level = pos.tp_price()

            # Priority: if both TP and stop could trigger intraday we don't
            # know which fired first on real data. Be conservative and assume
            # stop fired first (worst-case for the bot).
            if low_p <= stop_level:
                # Gap-through: open already below the trail → fill at open.
                fill = min(stop_level, open_p) if open_p < stop_level else stop_level
                bt.close_position(sym, today, fill, "trailing_stop")
            elif high_p >= tp_level:
                # Gap-up: open already above TP → fill at open (positive slip).
                fill = max(tp_level, open_p) if open_p > tp_level else tp_level
                bt.close_position(sym, today, fill, "take_profit")
            elif float(row["rsi"]) >= pos.rsi_exit:
                bt.close_position(sym, today, close_p, "rsi_overbought")

        # ── 2. Mark-to-market & loss limit checks ─────────────────────────
        equity = bt.mark_to_market(data, today)
        if equity > bt.peak_equity:
            bt.peak_equity = equity
        dd = (equity - bt.peak_equity) / bt.peak_equity if bt.peak_equity else 0
        day_pnl = (equity - bt.day_start_equity) / bt.day_start_equity if bt.day_start_equity else 0

        bt.equity_curve.append((today, equity))

        if dd <= -MAX_DRAWDOWN_PCT and not bt.halted:
            bt.halted = True
            for sym in list(bt.positions.keys()):
                df = data.get(sym)
                if df is not None and today in df.index:
                    bt.close_position(sym, today, float(df.loc[today, "close"]), "max_drawdown_flatten")

        day_halted = day_pnl <= -DAILY_LOSS_LIMIT_PCT

        if bt.halted or day_halted:
            continue

        # ── 3. Regime + sizing ────────────────────────────────────────────
        if today_regime is None or today_mult == 0:
            continue
        mult = today_mult

        cash_pct = bt.cash / equity if equity > 0 else 0
        if cash_pct < CASH_RESERVE_PCT:
            continue

        # Pre-compute gross exposure once per day (MAX_GROSS_EXPOSURE_PCT).
        current_exposure_base = bt.gross_exposure_base(data, today)
        exposure_cap_base = equity * MAX_GROSS_EXPOSURE_PCT

        # ── 4. Scan for entries ───────────────────────────────────────────
        pending_symbols = {pe["symbol"] for pe in bt.pending_entries}
        # Track base-currency amount already promised by today's pending
        # entries so that exposure-cap enforcement sees the full projected
        # state, not just already-filled positions.
        promised_base = sum(
            pe["amount"] for pe in bt.pending_entries
            if pe["symbol"] not in bt.positions
        )

        for sym, df in data.items():
            if sym in bt.positions or sym in pending_symbols:
                continue
            if len(bt.positions) + len(bt.pending_entries) >= MAX_POSITIONS:
                break
            if today not in df.index:
                continue

            row = df.loc[today]
            rsi = float(row["rsi"])
            price = float(row["close"])
            if math.isnan(rsi) or math.isnan(price):
                continue
            if rsi > RSI_OVERSOLD:
                continue

            if USE_VOLUME_FILTER and float(row["volume"]) <= float(row["vol_avg20"]):
                _bump(bt.rejection_counts, "volume_filter")
                continue
            if USE_TREND_FILTER and price <= float(row["ma200"]):
                _bump(bt.rejection_counts, "trend200_filter")
                continue
            if USE_MA20_FILTER and price <= float(row["ma20"]):
                _bump(bt.rejection_counts, "ma20_filter")
                continue

            held = set(bt.positions.keys()) | pending_symbols
            violated = False
            for grp_syms in CORR_GROUPS.values():
                if sym in grp_syms and sum(1 for s in held if s in grp_syms) >= MAX_PER_CORR_GROUP:
                    violated = True
                    break
            if violated:
                _bump(bt.rejection_counts, "correlation")
                continue

            # Base sizing in BASE currency.
            base_amount = equity * POSITION_SIZE_PCT * mult
            # Apply vol-adjusted scalar (mirrors live _apply_vol_scalar).
            atr_sizing_raw = row.get("atr_sizing")
            try:
                atr_sizing = float(atr_sizing_raw) if atr_sizing_raw is not None else None
            except (TypeError, ValueError):
                atr_sizing = None
            if atr_sizing is not None and not math.isfinite(atr_sizing):
                atr_sizing = None
            amount = _apply_vol_scalar(base_amount, atr_sizing, price)
            if amount < 10:
                _bump(bt.rejection_counts, "amount_below_min")
                continue

            # Gross-exposure cap (MAX_GROSS_EXPOSURE_PCT). This runs AFTER
            # vol-scalar so the check sees the real intended deployment.
            projected = current_exposure_base + promised_base + amount
            if projected > exposure_cap_base:
                _bump(bt.rejection_counts, "gross_exposure_cap")
                continue

            cfg = ASSET_CONFIG.get(sym, {}).copy()
            cfg.setdefault("trailing_stop", DEFAULT_TRAILING_STOP)
            cfg.setdefault("take_profit", DEFAULT_TAKE_PROFIT)
            cfg.setdefault("rsi_exit", RSI_OVERBOUGHT)

            if USE_ATR_STOPS and not math.isnan(float(row["atr"])):
                atr_pct = (ATR_MULTIPLIER * float(row["atr"])) / price
                cfg["trailing_stop"] = max(cfg["trailing_stop"], atr_pct)

            exchange = df["exchange"].iloc[0]
            currency = df["currency"].iloc[0]
            est_comm = EXCHANGE_COMMISSIONS.get(exchange, 10)
            if amount <= 0 or est_comm / amount > MAX_COMMISSION_PCT:
                _bump(bt.rejection_counts, "commission_too_high")
                continue

            if fill_mode == "next_open":
                next_open = row.get("next_open")
                if pd.isna(next_open):
                    _bump(bt.rejection_counts, "no_next_open_bar")
                    continue
                bt.pending_entries.append({
                    "symbol": sym, "exchange": exchange, "currency": currency,
                    "amount": amount, "cfg": cfg,
                })
                pending_symbols.add(sym)
                promised_base += amount
            else:
                opened = bt.open_position(sym, exchange, currency, today, price, amount, cfg)
                if opened:
                    current_exposure_base += amount

    return bt


# ══════════════════════════════════════════════════════════════════════════
#  METRICS
# ══════════════════════════════════════════════════════════════════════════

def print_metrics(bt: Backtester):
    if not bt.equity_curve:
        print("No data.")
        return

    dates = [d for d, _ in bt.equity_curve]
    equity = np.array([e for _, e in bt.equity_curve])
    returns = np.diff(equity) / equity[:-1]

    final = equity[-1]
    total_return = (final / bt.starting_capital - 1) * 100
    days = (dates[-1] - dates[0]).days
    years = days / 365.25 if days > 0 else 1
    cagr = ((final / bt.starting_capital) ** (1 / years) - 1) * 100 if years > 0 else 0

    sharpe = (np.mean(returns) / np.std(returns) * np.sqrt(252)) if len(returns) > 1 and np.std(returns) > 0 else 0

    peak = np.maximum.accumulate(equity)
    drawdowns = (equity - peak) / peak
    max_dd = drawdowns.min() * 100

    wins = [t for t in bt.trades if t.pnl > 0]
    losses = [t for t in bt.trades if t.pnl <= 0]
    win_rate = len(wins) / len(bt.trades) * 100 if bt.trades else 0
    avg_win = np.mean([t.pnl_pct for t in wins]) if wins else 0
    avg_loss = np.mean([t.pnl_pct for t in losses]) if losses else 0
    profit_factor = (sum(t.pnl for t in wins) / abs(sum(t.pnl for t in losses))) if losses and sum(t.pnl for t in losses) != 0 else float('inf')

    total_comm = sum(t.commission for t in bt.trades)
    total_slip = sum(t.slippage for t in bt.trades)

    base = bt.base_currency
    print("\n" + "═" * 72)
    print("  📊 BACKTEST RESULTS")
    print("═" * 72)
    print(f"  Period          : {dates[0].date()} → {dates[-1].date()} ({years:.2f} yrs)")
    print(f"  Fill mode       : {bt.fill_mode}")
    print(f"  Base currency   : {base} (all P&L / equity / cash denominated here)")
    print(f"  Starting capital: {base} {bt.starting_capital:,.2f}")
    print(f"  Ending capital  : {base} {final:,.2f}")
    print(f"  Total return    : {total_return:+.2f}%")
    print(f"  CAGR            : {cagr:+.2f}%")
    print(f"  Sharpe (ann.)   : {sharpe:.2f}")
    print(f"  Max drawdown    : {max_dd:.2f}%")
    print()
    print(f"  Trades          : {len(bt.trades)}")
    print(f"  Win rate        : {win_rate:.1f}%")
    print(f"  Avg win         : {avg_win:+.2f}%  (native-price return, FX-independent)")
    print(f"  Avg loss        : {avg_loss:+.2f}%  (native-price return, FX-independent)")
    print(f"  Profit factor   : {profit_factor:.2f}")
    print()
    print(f"  Total commission: {base} {total_comm:,.2f}  (entry + exit)")
    print(f"  Total slippage  : {base} {total_slip:,.2f}  (entry + exit)")
    print("═" * 72)

    print("\n  Exit reason breakdown:")
    from collections import Counter
    for reason, count in Counter(t.reason for t in bt.trades).most_common():
        reason_trades = [t for t in bt.trades if t.reason == reason]
        avg_pnl = np.mean([t.pnl_pct for t in reason_trades])
        print(f"    {reason:<25} {count:>4} trades  avg {avg_pnl:+.2f}%")

    print("\n  Top 5 winners:")
    by_sym = {}
    for t in bt.trades:
        by_sym.setdefault(t.symbol, []).append(t.pnl)
    sym_totals = {s: sum(p) for s, p in by_sym.items()}
    for sym, pnl in sorted(sym_totals.items(), key=lambda x: -x[1])[:5]:
        print(f"    {sym:<8} ${pnl:+,.2f} ({len(by_sym[sym])} trades)")
    print("\n  Bottom 5 losers:")
    for sym, pnl in sorted(sym_totals.items(), key=lambda x: x[1])[:5]:
        print(f"    {sym:<8} ${pnl:+,.2f} ({len(by_sym[sym])} trades)")

    if bt.rejection_counts:
        print("\n  Signal-rejection breakdown (RSI-eligible candidates "
              "blocked by downstream filters):")
        for reason, count in sorted(
            bt.rejection_counts.items(), key=lambda x: -x[1]
        ):
            print(f"    {reason:<22} {count:>5}")
    print()


def save_trades_csv(bt: Backtester, path: str = "backtest_trades.csv"):
    df = pd.DataFrame([{
        "symbol": t.symbol, "exchange": t.exchange,
        "entry_date": t.entry_date, "exit_date": t.exit_date,
        "entry_price": t.entry_price, "exit_price": t.exit_price,
        "qty": t.qty, "pnl": t.pnl, "pnl_pct": t.pnl_pct,
        "reason": t.reason, "commission": t.commission, "slippage": t.slippage,
    } for t in bt.trades])
    df.to_csv(path, index=False)
    print(f"  💾 Trades saved → {path}")


def save_equity_csv(bt: Backtester, path: str = "backtest_equity.csv"):
    df = pd.DataFrame(bt.equity_curve, columns=["date", "equity"])
    df.to_csv(path, index=False)
    print(f"  💾 Equity curve → {path}")


# ══════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Backtest the Global RSI Bot")
    parser.add_argument("--start", default=(datetime.now() - timedelta(days=365*3)).strftime("%Y-%m-%d"))
    parser.add_argument("--end",   default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--capital", type=float, default=10_000.0)
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--us-only", action="store_true")
    parser.add_argument("--fill-mode", choices=["next_open", "same_close"],
                        default=BACKTEST_FILL_MODE)
    parser.add_argument("--base-currency", default="USD",
                        help="Base currency for cash/equity/P&L (e.g. USD, AUD). "
                             "Non-base currencies pull FX from yfinance.")
    args = parser.parse_args()

    universe = STOCK_UNIVERSE
    if args.us_only:
        universe = [u for u in universe if u[1] == "SMART"]
    if args.symbols:
        wanted = set(args.symbols)
        universe = [u for u in universe if u[0] in wanted]

    base_ccy = args.base_currency.upper()
    print(f"🧪 Backtest | {args.start} → {args.end} | {base_ccy} {args.capital:,.0f} starting capital")
    print(f"   Universe : {len(universe)} symbols")
    print(f"   Fill mode: {args.fill_mode}")
    print(f"   Base ccy : {base_ccy}")
    print(f"   Filters  : Vol={USE_VOLUME_FILTER} Trend200={USE_TREND_FILTER} MA20={USE_MA20_FILTER}")
    print(f"   ATR stops: {USE_ATR_STOPS} (mult={ATR_MULTIPLIER})")
    print()

    data = load_data(universe, args.start, args.end)
    if not data:
        print("No data loaded.")
        sys.exit(1)

    # Build a trading-day index from the union of all loaded symbols — FX data
    # will be reindexed onto this so every trading day has a valid rate.
    union_idx = sorted({d for df in data.values() for d in df.index})
    trading_idx = pd.DatetimeIndex(union_idx) if union_idx else None

    currencies_in_universe = sorted({u[2] for u in universe})
    print(f"   Currencies in universe: {currencies_in_universe}")
    print(f"📥 Loading FX rates → {base_ccy}...")
    fx_rates = load_fx_data(base_ccy, currencies_in_universe,
                             args.start, args.end, trading_index=trading_idx)

    regime_df = load_regime_data(args.start, args.end)
    bt = run_backtest(data, regime_df, fx_rates, args.capital,
                      base_currency=base_ccy, fill_mode=args.fill_mode)
    print_metrics(bt)
    save_trades_csv(bt)
    save_equity_csv(bt)


if __name__ == "__main__":
    main()
