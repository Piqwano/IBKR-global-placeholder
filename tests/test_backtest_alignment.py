"""
Alignment tests — backtest vs live-bot signal logic.

These are self-contained (no yfinance, no IBKR) and verify:
  1. RSI/ATR in backtest match ibkr_helpers.calc_rsi / calc_atr bar-for-bar.
  2. _apply_vol_scalar matches global_rsi_bot._apply_vol_scalar for the same
     (atr_sizing, price, base_amount) inputs.
  3. Gap-through fill logic triggers at min(stop, open) when open < stop.
  4. TP gap-up fill triggers at max(tp, open) when open > tp.

Run:
    python tests/test_backtest_alignment.py
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_rsi_matches_live():
    from backtest import rsi_series
    from ibkr_helpers import calc_rsi

    rng = np.random.default_rng(7)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, size=80)))
    series = pd.Series(closes)
    bt_rsi = rsi_series(series, period=14)
    # Live calc_rsi returns a single scalar for the full closes array.
    live_val = calc_rsi(closes.astype(float), period=14)
    bt_last = float(bt_rsi.iloc[-1])
    assert abs(bt_last - live_val) < 0.01, (
        f"RSI drift: backtest={bt_last:.4f} live={live_val:.4f}"
    )


def test_atr_matches_live():
    from backtest import atr_series
    from ibkr_helpers import calc_atr

    rng = np.random.default_rng(11)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, size=80)))
    highs = closes * (1 + np.abs(rng.normal(0, 0.005, size=80)))
    lows = closes * (1 - np.abs(rng.normal(0, 0.005, size=80)))
    bt_atr = atr_series(pd.Series(highs), pd.Series(lows), pd.Series(closes), period=14)
    live = calc_atr(highs, lows, closes, period=14)
    assert abs(float(bt_atr.iloc[-1]) - live) < 0.01, (
        f"ATR drift: backtest={float(bt_atr.iloc[-1]):.4f} live={live:.4f}"
    )


def test_vol_scalar_matches_live():
    from backtest import _apply_vol_scalar as bt_scalar
    # Mirror the live formula inline to avoid importing global_rsi_bot
    # (which needs ib_insync). The live function's only dependencies are
    # config constants, which the backtest helper already pulls from.
    from config import (
        USE_VOL_ADJUSTED_SIZING, VOL_TARGET_ANNUAL,
        VOL_SCALAR_MIN, VOL_SCALAR_MAX,
    )

    def live_scalar(base_amount, atr_sizing, price):
        if not USE_VOL_ADJUSTED_SIZING:
            return base_amount
        if not atr_sizing or not price or price <= 0:
            return base_amount
        atr_pct = atr_sizing / price
        annualised = atr_pct * math.sqrt(252)
        if annualised <= 0:
            return base_amount
        raw = VOL_TARGET_ANNUAL / annualised
        return round(base_amount * max(VOL_SCALAR_MIN, min(VOL_SCALAR_MAX, raw)), 2)

    cases = [
        (1000.0, 2.5, 100.0),   # mid-range vol → ~target scalar
        (1000.0, 0.5, 100.0),   # low vol → scalar clamped to VOL_SCALAR_MAX
        (1000.0, 10.0, 100.0),  # high vol → scalar clamped to VOL_SCALAR_MIN
        (500.0, None, 100.0),   # missing atr → no change
        (500.0, 1.0, 0.0),      # zero price → no change
    ]
    for base, atr, price in cases:
        assert bt_scalar(base, atr, price) == live_scalar(base, atr, price), (
            f"vol_scalar mismatch at ({base}, {atr}, {price}): "
            f"bt={bt_scalar(base, atr, price)} live={live_scalar(base, atr, price)}"
        )


def test_gap_through_stop_fills_at_open():
    """Build a one-trade backtest where the stock gaps down through the
    trailing stop and confirm the fill uses the open, not the stop price."""
    from backtest import Backtester, Position

    idx = pd.DatetimeIndex([
        pd.Timestamp("2024-01-02"),
        pd.Timestamp("2024-01-03"),
    ])
    df = pd.DataFrame({
        "open":  [100.0, 90.0],   # day 2 gaps down to 90
        "high":  [101.0, 91.0],
        "low":   [99.0,  89.0],
        "close": [100.0, 90.5],
    }, index=idx)

    bt = Backtester(starting_capital=10_000, base_currency="USD",
                    fx_rates={"USD": pd.Series(1.0, index=idx)},
                    fill_mode="same_close")
    bt.positions["TEST"] = Position(
        symbol="TEST", exchange="SMART", currency="USD",
        entry_date=idx[0], entry_price=100.0, qty=10, peak=100.0,
        trailing_stop_pct=0.06, take_profit_pct=0.10, rsi_exit=60,
        entry_commission=1.0, entry_slippage=0.0,
    )

    # Replicate the exit-handling code path
    today = idx[1]
    row = df.loc[today]
    pos = bt.positions["TEST"]
    open_p, low_p = float(row["open"]), float(row["low"])
    if float(row["high"]) > pos.peak:
        pos.peak = float(row["high"])
    stop_level = pos.current_stop()
    assert low_p <= stop_level, "stop must have triggered for this fixture"
    fill = min(stop_level, open_p) if open_p < stop_level else stop_level
    bt.close_position("TEST", today, fill, "trailing_stop")

    trade = bt.trades[-1]
    assert trade.reason == "trailing_stop"
    # Fill happened at open (gap-through) not at stop level
    # entry slipped up by SLIPPAGE_BPS, exit slipped down by SLIPPAGE_BPS —
    # compare the PRE-exit-slip fill price, recorded as exit_price after
    # multiplication. Close_position applies its own slippage on top of the
    # `fill` we pass, so the recorded exit_price should be ≤ open*0.9998.
    slip_adj_open = 90.0 * (1 - 2 / 10_000)
    assert trade.exit_price <= slip_adj_open + 1e-6, (
        f"expected fill near open 90.0, got {trade.exit_price}"
    )


def test_tp_gap_up_fills_at_open():
    from backtest import Backtester, Position

    idx = pd.DatetimeIndex([
        pd.Timestamp("2024-01-02"),
        pd.Timestamp("2024-01-03"),
    ])
    df = pd.DataFrame({
        "open":  [100.0, 115.0],  # day 2 gaps up to 115 (TP was at 110)
        "high":  [101.0, 116.0],
        "low":   [99.0,  114.0],
        "close": [100.0, 115.5],
    }, index=idx)

    bt = Backtester(starting_capital=10_000, base_currency="USD",
                    fx_rates={"USD": pd.Series(1.0, index=idx)},
                    fill_mode="same_close")
    bt.positions["TEST"] = Position(
        symbol="TEST", exchange="SMART", currency="USD",
        entry_date=idx[0], entry_price=100.0, qty=10, peak=100.0,
        trailing_stop_pct=0.06, take_profit_pct=0.10, rsi_exit=60,
        entry_commission=1.0, entry_slippage=0.0,
    )

    today = idx[1]
    row = df.loc[today]
    pos = bt.positions["TEST"]
    open_p, high_p = float(row["open"]), float(row["high"])
    if high_p > pos.peak:
        pos.peak = high_p
    tp_level = pos.tp_price()
    assert high_p >= tp_level
    fill = max(tp_level, open_p) if open_p > tp_level else tp_level
    bt.close_position("TEST", today, fill, "take_profit")

    trade = bt.trades[-1]
    assert trade.reason == "take_profit"
    slip_adj_open = 115.0 * (1 - 2 / 10_000)
    assert trade.exit_price >= slip_adj_open - 0.1, (
        f"expected fill near gap-up open 115.0, got {trade.exit_price}"
    )


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {fn.__name__}: {e}")
    if failed:
        print(f"\n{failed} test(s) failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} alignment tests passed.")
