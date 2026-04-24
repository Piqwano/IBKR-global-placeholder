"""
Synthetic-data backtest harness (network-free).

Patches yfinance with a generator that returns realistic OHLC + volume for
every symbol the backtest asks for. Exists so we can exercise backtest.py
end-to-end in environments without outbound yfinance access (sandboxes, CI).

Not a substitute for a real yfinance run — results are driven entirely by
the RNG seed below. But it WILL surface runtime bugs, divergences between
backtest sizing vs live sizing, fill-logic errors, NaN propagation, etc.

Usage:
    python tests/synth_backtest_runner.py
    python tests/synth_backtest_runner.py --start 2022-01-01 --end 2024-12-31

Adjust SEED for deterministic variation.
"""
from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SEED = 42
_rng = np.random.default_rng(SEED)


def _synth_ohlc(start: str, end: str, start_price: float, drift: float,
                vol: float, volume_base: float) -> pd.DataFrame:
    """Geometric Brownian motion with realistic large-cap intraday H/L."""
    idx = pd.bdate_range(start=start, end=end)
    n = len(idx)
    if n == 0:
        return pd.DataFrame()
    daily_drift = drift / 252
    daily_vol = vol / np.sqrt(252)
    shocks = _rng.normal(daily_drift, daily_vol, size=n)
    closes = start_price * np.exp(np.cumsum(shocks))
    # Opens: small overnight gap (~0.3× daily vol)
    opens = np.concatenate([[start_price], closes[:-1]]) * np.exp(
        _rng.normal(0, daily_vol / 3, size=n)
    )
    # Intraday range — calibrated to real large-cap data: typical daily
    # true-range is ~0.6-0.9× the close-to-close stdev, not 1.5×. The old
    # 1.5× factor here made intraday spreads so wide that a 6% trailing
    # stop got hit on almost every trade and masked signal quality.
    intraday = np.abs(_rng.normal(0, daily_vol * 0.6, size=n))
    highs = np.maximum(opens, closes) * (1 + intraday)
    lows = np.minimum(opens, closes) * (1 - intraday)
    volumes = _rng.lognormal(
        mean=np.log(max(volume_base, 1.0)), sigma=0.3, size=n
    )
    return pd.DataFrame({
        "Open": opens,
        "High": highs,
        "Low": lows,
        "Close": closes,
        "Volume": volumes,
    }, index=idx)


# Per-ticker deterministic params so repeat runs produce the same data.
def _params_for_ticker(tkr: str) -> tuple[float, float, float, float]:
    h = abs(hash(tkr)) % (10 ** 8)
    r2 = np.random.default_rng(h)
    start_price = float(r2.uniform(15, 400))
    drift = float(r2.uniform(0.04, 0.14))   # 4–14% annualized
    vol = float(r2.uniform(0.14, 0.32))     # 14–32% — real large-cap range
    volume_base = float(r2.uniform(1e5, 5e7))
    return start_price, drift, vol, volume_base


def _install_yfinance_stub():
    mod = types.ModuleType("yfinance")

    def download(tkr, start=None, end=None, progress=False, auto_adjust=True, **kwargs):
        # FX tickers like "AUDUSD=X" — return a slowly drifting series around 0.65
        if isinstance(tkr, str) and tkr.endswith("=X"):
            idx = pd.bdate_range(start=start, end=end)
            n = len(idx)
            base = {"AUDUSD=X": 0.66, "GBPUSD=X": 1.27, "EURUSD=X": 1.09,
                    "HKDUSD=X": 0.128, "SGDUSD=X": 0.745,
                    "USDAUD=X": 1.52, "USDGBP=X": 0.79, "USDEUR=X": 0.92,
                    "USDHKD=X": 7.82, "USDSGD=X": 1.34}.get(tkr, 1.0)
            shocks = _rng.normal(0, 0.004, size=n)
            closes = base * np.exp(np.cumsum(shocks))
            return pd.DataFrame({
                "Open": closes, "High": closes * 1.001,
                "Low": closes * 0.999, "Close": closes,
                "Volume": np.zeros(n),
            }, index=idx)

        # Equities and SPY regime
        sp, dr, vv, vb = _params_for_ticker(tkr)
        # SPY: make it uptrending so regime is mostly BULL — easier to validate
        if tkr == "SPY":
            sp, dr, vv = 450.0, 0.09, 0.16
        return _synth_ohlc(start, end, sp, dr, vv, vb)

    mod.download = download  # type: ignore[attr-defined]
    sys.modules["yfinance"] = mod


def main():
    _install_yfinance_stub()
    # Import backtest after the stub is in place so its `import yfinance as yf`
    # binds to the shim.
    import backtest as bt

    # Inject argv so backtest.main() parses defaults we want
    argv_bak = sys.argv[:]
    sys.argv = [
        "backtest.py",
        "--start", (datetime.now() - timedelta(days=365 * 2)).strftime("%Y-%m-%d"),
        "--end", datetime.now().strftime("%Y-%m-%d"),
        "--capital", "10000",
        "--base-currency", "USD",
    ]
    # Respect user CLI overrides
    if len(argv_bak) > 1:
        sys.argv = ["backtest.py"] + argv_bak[1:]
    try:
        bt.main()
    finally:
        sys.argv = argv_bak


if __name__ == "__main__":
    main()
