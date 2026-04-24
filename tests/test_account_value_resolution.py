"""
Unit tests for ibkr_helpers._resolve_account_value_raw — the function that
decides whether account values from IBKR's accountSummary are "healthy"
enough to trade on.

Three canonical paths:
  1. BASE row present    → authoritative, healthy.
  2. ACCOUNT_BASE_CURRENCY override matches an IBKR-returned row → healthy
                           (double-confirmed by user declaration).
  3. No BASE, no override match → fallback, unhealthy (surfaces silent
                                   single-currency mismatches).

Reproduces the production scenario where a small AUD retail account has
no multi-currency activity, so IBKR never emits a BASE row — the old
behaviour failed the startup self-test on a perfectly well-configured
account.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@dataclass
class _FakeItem:
    """Stand-in for ib_insync's AccountValue NamedTuple — only the fields
    _resolve_account_value_raw reads."""
    tag: str
    currency: str
    value: str


def _set_env_base(ccy: Optional[str]):
    """Set ACCOUNT_BASE_CURRENCY env and re-exec the config + ibkr_helpers
    imports so the new value is picked up. Required because the function
    reads the env through config at call time."""
    if ccy is None:
        os.environ.pop("ACCOUNT_BASE_CURRENCY", None)
    else:
        os.environ["ACCOUNT_BASE_CURRENCY"] = ccy
    # Force re-import so config.ACCOUNT_BASE_CURRENCY sees the new env.
    for mod in ("config", "ibkr_helpers"):
        sys.modules.pop(mod, None)


def test_base_row_is_healthy():
    """Path 1: when IBKR emits a BASE row, use it and mark healthy."""
    _set_env_base(None)
    from ibkr_helpers import _resolve_account_value_raw

    values = [
        _FakeItem("NetLiquidation", "USD", "1000.0"),
        _FakeItem("NetLiquidation", "BASE", "1500.0"),
    ]
    result = _resolve_account_value_raw(values, "NetLiquidation")
    assert result.value == 1500.0, f"expected 1500, got {result.value}"
    assert result.healthy is True
    assert "BASE clean" in result.reason


def test_override_match_is_healthy_no_base_row():
    """Path 2: no BASE row, but ACCOUNT_BASE_CURRENCY matches an emitted
    row — treat as healthy. This is the AUD-single-currency scenario that
    prompted the fix."""
    _set_env_base("AUD")
    from ibkr_helpers import _resolve_account_value_raw

    values = [
        _FakeItem("NetLiquidation", "AUD", "2383.54"),
        _FakeItem("BuyingPower",   "AUD", "2383.54"),
    ]
    result = _resolve_account_value_raw(values, "NetLiquidation")
    assert result.value == 2383.54, f"expected 2383.54, got {result.value}"
    assert result.healthy is True, (
        f"expected healthy when override matches emitted currency, "
        f"got reason={result.reason!r}"
    )
    assert "ACCOUNT_BASE_CURRENCY override" in result.reason


def test_override_mismatch_is_unhealthy():
    """Path 3a: user declared USD but IBKR only returns AUD — this is a
    real silent-mismatch risk and MUST stay unhealthy."""
    _set_env_base("USD")
    from ibkr_helpers import _resolve_account_value_raw

    values = [
        _FakeItem("NetLiquidation", "AUD", "2383.54"),
    ]
    result = _resolve_account_value_raw(values, "NetLiquidation")
    # Value still returned for observability, but flagged unhealthy so
    # new buys are blocked.
    assert result.value == 2383.54
    assert result.healthy is False
    assert "fell back to AUD" in result.reason


def test_no_override_and_no_base_is_unhealthy():
    """Path 3b: user didn't declare a base and IBKR gave no BASE row —
    fallback resolves a value but flags unhealthy (same as before the fix)."""
    _set_env_base(None)
    from ibkr_helpers import _resolve_account_value_raw

    values = [
        _FakeItem("NetLiquidation", "AUD", "2383.54"),
    ]
    result = _resolve_account_value_raw(values, "NetLiquidation")
    assert result.value == 2383.54
    assert result.healthy is False


def test_base_row_preferred_over_override_match():
    """Precedence: if IBKR emits BOTH a BASE row and a matching-override
    row, BASE wins (it's the more-authoritative path)."""
    _set_env_base("AUD")
    from ibkr_helpers import _resolve_account_value_raw

    values = [
        _FakeItem("NetLiquidation", "AUD",  "2383.54"),
        _FakeItem("NetLiquidation", "BASE", "2400.00"),  # slightly different
    ]
    result = _resolve_account_value_raw(values, "NetLiquidation")
    assert result.value == 2400.00, "BASE row should win over override match"
    assert result.healthy is True
    assert "BASE clean" in result.reason


def test_malformed_base_value_is_unhealthy():
    """Path 1 edge: BASE row is malformed → unhealthy, no silent
    fallback to a non-BASE row (that would hide corruption)."""
    _set_env_base("AUD")
    from ibkr_helpers import _resolve_account_value_raw

    values = [
        _FakeItem("NetLiquidation", "BASE", "not-a-number"),
        _FakeItem("NetLiquidation", "AUD",  "2383.54"),
    ]
    result = _resolve_account_value_raw(values, "NetLiquidation")
    assert result.healthy is False
    assert "malformed" in result.reason


def test_completely_missing_tag_is_unhealthy_with_zero_value():
    """No matching rows at all — return (0, unhealthy) with a clear reason.
    Callers must not trade on a zero NLV."""
    _set_env_base("AUD")
    from ibkr_helpers import _resolve_account_value_raw

    values = [
        _FakeItem("SomeOtherTag", "AUD", "100.0"),
    ]
    result = _resolve_account_value_raw(values, "NetLiquidation")
    assert result.value == 0.0
    assert result.healthy is False
    assert "could not resolve" in result.reason


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  [PASS] {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  [FAIL] {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  [ERROR] {fn.__name__}: {type(e).__name__}: {e}")
    if failed:
        print(f"\n{failed} test(s) failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed.")
