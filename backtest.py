"""
Backtest — Global RSI Bot
===========================
Vectorized historical simulation using the exact same signal logic as the
live bot. Uses yfinance for data.

Fill modes (config.BACKTEST_FILL_MODE):
  - "next_open":  signal on day T fills at T+1 open (rigorous, no look-ahead)
  - "same_close": signal on day T fills at T's close (simpler, slightly optimistic)

Realistic costs: per-exchange commission + per-exchange bps slippage,
tracked symmetrically on entry AND exit.

Run:
    python backtest.py                                   # 3-year backtest
    python backtest.py --start 2020-01-01 --end 2024-12-31
    python backtest.py --capital 10000 --symbols AAPL MSFT NVDA
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
#  INDICATORS (Wilder RSI — matches live calc_rsi)
# ══════════════════════════════════════════════════════════════════════════

def rsi_series(closes: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)


def atr_series(high: pd.Series, low: pd.Series, close: pd.Series, period: int = ATR_PERIOD) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()


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
            df["exchange"] = exchange
            df["currency"] = currency
            df["name"] = name
            df["next_open"] = df["open"].shift(-1)

            data[symbol] = df.dropna(subset=["rsi"])
        except Exception as e:
            print(f"   ⚠️  {symbol} ({yt}) failed: {e}")

    print(f"✅ Loaded data for {len(data)}/{len(symbols)} symbols")
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
    entry_date: pd.Timestamp
    entry_price: float
    qty: int
    peak: float
    trailing_stop_pct: float
    take_profit_pct: float
    rsi_exit: float
    entry_commission: float
    entry_slippage: float

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
    starting_capital: float
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

    def __post_init__(self):
        self.cash = self.starting_capital
        self.peak_equity = self.starting_capital
        self.day_start_equity = self.starting_capital

    def calc_qty(self, exchange: str, symbol: str, amount: float, price: float) -> int:
        if exchange == "SGX":
            return ((int(amount / price)) // 100) * 100
        if exchange == "SEHK":
            lot = SEHK_LOT_SIZES.get(symbol, 100)
            return ((int(amount / price)) // lot) * lot
        return int(amount / price)

    def costs(self, exchange: str, notional: float) -> Tuple[float, float]:
        commission = EXCHANGE_COMMISSIONS.get(exchange, 5.0)
        slippage = notional * (SLIPPAGE_BPS.get(exchange, 10) / 10_000)
        return commission, slippage

    def mark_to_market(self, data: Dict[str, pd.DataFrame], today: pd.Timestamp) -> float:
        equity = self.cash
        for sym, pos in self.positions.items():
            df = data.get(sym)
            if df is not None and today in df.index:
                equity += pos.qty * df.loc[today, "close"]
            else:
                equity += pos.qty * pos.entry_price
        return equity

    def open_position(self, symbol: str, exchange: str, day: pd.Timestamp,
                      fill_price: float, amount: float, cfg: dict) -> bool:
        qty = self.calc_qty(exchange, symbol, amount, fill_price)
        if qty < 1:
            return False
        notional = qty * fill_price
        commission, slippage = self.costs(exchange, notional)
        effective_price = fill_price * (1 + SLIPPAGE_BPS.get(exchange, 10) / 10_000)
        total_cost = qty * effective_price + commission
        if total_cost > self.cash:
            return False

        self.cash -= total_cost
        self.positions[symbol] = Position(
            symbol=symbol, exchange=exchange,
            entry_date=day, entry_price=effective_price,
            qty=qty, peak=effective_price,
            trailing_stop_pct=cfg["trailing_stop"],
            take_profit_pct=cfg["take_profit"],
            rsi_exit=cfg["rsi_exit"],
            entry_commission=commission,
            entry_slippage=slippage,
        )
        return True

    def close_position(self, symbol: str, day: pd.Timestamp, price: float, reason: str):
        pos = self.positions.pop(symbol)
        notional = pos.qty * price
        commission, slippage = self.costs(pos.exchange, notional)
        effective_price = price * (1 - SLIPPAGE_BPS.get(pos.exchange, 10) / 10_000)
        proceeds = pos.qty * effective_price - commission
        self.cash += proceeds

        pnl = proceeds - (pos.qty * pos.entry_price) - pos.entry_commission
        pnl_pct = (effective_price - pos.entry_price) / pos.entry_price * 100

        self.trades.append(Trade(
            symbol=symbol, exchange=pos.exchange,
            entry_date=pos.entry_date, exit_date=day,
            entry_price=pos.entry_price, exit_price=effective_price,
            qty=pos.qty, pnl=pnl, pnl_pct=pnl_pct,
            reason=reason,
            commission=pos.entry_commission + commission,
            slippage=pos.entry_slippage + slippage,
        ))


# ══════════════════════════════════════════════════════════════════════════
#  MAIN BACKTEST
# ══════════════════════════════════════════════════════════════════════════

def run_backtest(data: Dict[str, pd.DataFrame], regime_df: pd.DataFrame,
                 starting_capital: float,
                 fill_mode: str = BACKTEST_FILL_MODE) -> Backtester:

    bt = Backtester(starting_capital=starting_capital, fill_mode=fill_mode)

    all_dates = set(regime_df.index)
    for df in data.values():
        all_dates |= set(df.index)
    all_dates = sorted(all_dates)

    for today in all_dates:
        if bt.last_day is None or today.date() != bt.last_day.date():
            bt.day_start_equity = bt.mark_to_market(data, today)
        bt.last_day = today

        # ── 0. Process pending entries from PREVIOUS bar (next_open mode) ──
        if fill_mode == "next_open" and bt.pending_entries:
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
                bt.open_position(sym, pe["exchange"], today, fill_price, pe["amount"], pe["cfg"])
            bt.pending_entries = []

        # ── 1. Check exits on open positions ──────────────────────────────
        for sym in list(bt.positions.keys()):
            df = data.get(sym)
            if df is None or today not in df.index:
                continue
            row = df.loc[today]
            price = float(row["close"])
            pos = bt.positions[sym]

            if price > pos.peak:
                pos.peak = price

            if price <= pos.current_stop():
                bt.close_position(sym, today, pos.current_stop(), "trailing_stop")
            elif price >= pos.tp_price():
                bt.close_position(sym, today, pos.tp_price(), "take_profit")
            elif float(row["rsi"]) >= pos.rsi_exit:
                bt.close_position(sym, today, price, "rsi_overbought")

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
        if today not in regime_df.index:
            continue
        regime = regime_df.loc[today, "regime"]
        mult = REGIME_SIZE_MULTIPLIERS[regime]
        if mult == 0:
            continue

        cash_pct = bt.cash / equity if equity > 0 else 0
        if cash_pct < CASH_RESERVE_PCT:
            continue

        # ── 4. Scan for entries ───────────────────────────────────────────
        pending_symbols = {pe["symbol"] for pe in bt.pending_entries}

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
                continue
            if USE_TREND_FILTER and price <= float(row["ma200"]):
                continue
            if USE_MA20_FILTER and price <= float(row["ma20"]):
                continue

            held = set(bt.positions.keys()) | pending_symbols
            violated = False
            for grp_syms in CORR_GROUPS.values():
                if sym in grp_syms and sum(1 for s in held if s in grp_syms) >= MAX_PER_CORR_GROUP:
                    violated = True
                    break
            if violated:
                continue

            amount = equity * POSITION_SIZE_PCT * mult
            if amount < 10:
                continue

            cfg = ASSET_CONFIG.get(sym, {}).copy()
            cfg.setdefault("trailing_stop", DEFAULT_TRAILING_STOP)
            cfg.setdefault("take_profit", DEFAULT_TAKE_PROFIT)
            cfg.setdefault("rsi_exit", RSI_OVERBOUGHT)

            if USE_ATR_STOPS and not math.isnan(float(row["atr"])):
                atr_pct = (ATR_MULTIPLIER * float(row["atr"])) / price
                cfg["trailing_stop"] = max(cfg["trailing_stop"], atr_pct)

            exchange = df["exchange"].iloc[0]
            est_comm = EXCHANGE_COMMISSIONS.get(exchange, 10)
            if est_comm / amount > 0.03:
                continue

            if fill_mode == "next_open":
                next_open = row.get("next_open")
                if pd.isna(next_open):
                    continue
                bt.pending_entries.append({
                    "symbol": sym, "exchange": exchange,
                    "amount": amount, "cfg": cfg,
                })
                pending_symbols.add(sym)
            else:
                bt.open_position(sym, exchange, today, price, amount, cfg)

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

    print("\n" + "═" * 72)
    print("  📊 BACKTEST RESULTS")
    print("═" * 72)
    print(f"  Period          : {dates[0].date()} → {dates[-1].date()} ({years:.2f} yrs)")
    print(f"  Fill mode       : {bt.fill_mode}")
    print(f"  Starting capital: ${bt.starting_capital:,.2f}")
    print(f"  Ending capital  : ${final:,.2f}")
    print(f"  Total return    : {total_return:+.2f}%")
    print(f"  CAGR            : {cagr:+.2f}%")
    print(f"  Sharpe (ann.)   : {sharpe:.2f}")
    print(f"  Max drawdown    : {max_dd:.2f}%")
    print()
    print(f"  Trades          : {len(bt.trades)}")
    print(f"  Win rate        : {win_rate:.1f}%")
    print(f"  Avg win         : {avg_win:+.2f}%")
    print(f"  Avg loss        : {avg_loss:+.2f}%")
    print(f"  Profit factor   : {profit_factor:.2f}")
    print()
    print(f"  Total commission: ${total_comm:,.2f}  (entry + exit)")
    print(f"  Total slippage  : ${total_slip:,.2f}  (entry + exit)")
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
    args = parser.parse_args()

    universe = STOCK_UNIVERSE
    if args.us_only:
        universe = [u for u in universe if u[1] == "SMART"]
    if args.symbols:
        wanted = set(args.symbols)
        universe = [u for u in universe if u[0] in wanted]

    print(f"🧪 Backtest | {args.start} → {args.end} | ${args.capital:,.0f} starting capital")
    print(f"   Universe : {len(universe)} symbols")
    print(f"   Fill mode: {args.fill_mode}")
    print(f"   Filters  : Vol={USE_VOLUME_FILTER} Trend200={USE_TREND_FILTER} MA20={USE_MA20_FILTER}")
    print(f"   ATR stops: {USE_ATR_STOPS} (mult={ATR_MULTIPLIER})")
    print()

    data = load_data(universe, args.start, args.end)
    if not data:
        print("No data loaded.")
        sys.exit(1)

    regime_df = load_regime_data(args.start, args.end)
    bt = run_backtest(data, regime_df, args.capital, fill_mode=args.fill_mode)
    print_metrics(bt)
    save_trades_csv(bt)
    save_equity_csv(bt)


if __name__ == "__main__":
    main()
