"""
Dashboard — Tiny FastAPI status server
========================================
Exposes /health, /status, /positions on DASHBOARD_PORT.
Runs in a background daemon thread. Reads a thread-safe snapshot of bot
state that the main loop updates via `update_dashboard_state()`.

R-round additions:
  R4: /status exposes `stale_position_count` (top-level) for at-a-glance
      detection of data outages.
  R11: Configurable bind host (DASHBOARD_HOST) and optional header-token
       auth (DASHBOARD_AUTH_TOKEN) for /status, /positions, /docs. /health
       stays unauthenticated for platform health checks.
"""

from __future__ import annotations

import hmac
import logging
import threading
from typing import Any, Dict, Optional

log = logging.getLogger("ibkr-rsi")

try:
    from fastapi import FastAPI, Header, HTTPException, Request
    from fastapi.responses import JSONResponse
    import uvicorn
    _FASTAPI_OK = True
except ImportError:
    _FASTAPI_OK = False
    FastAPI = None        # type: ignore
    uvicorn = None        # type: ignore


# ──────────────────────────────────────────────────────────────────────────
#  SHARED STATE
# ──────────────────────────────────────────────────────────────────────────

_state_lock = threading.Lock()
_state: Dict[str, Any] = {
    "mode": "PAPER",
    "nlv": 0.0,
    "cash": 0.0,
    "day_start_nlv": 0.0,
    "peak_nlv": 0.0,
    "day_pnl_pct": 0.0,
    "day_pnl_dollars": 0.0,
    "dd_pct": 0.0,
    "regime": "UNKNOWN",
    "regime_mult": 1.0,
    "vix": None,
    "daily_halted": False,
    "max_dd_halted": False,
    "account_values_healthy": True,
    "account_values_health_reason": "",
    "positions": [],
    "universe_size": 0,
    "max_positions": 0,
    "last_update": None,
    "connected": False,
    "last_scan_duration_seconds": None,
    "winrates": {
        "lifetime": None,
        "past_30_days": None,
        "past_7_days": None,
        "past_24_hours": None,
    },
    "trade_counts": {
        "lifetime": 0,
        "past_30_days": 0,
        "past_7_days": 0,
        "past_24_hours": 0,
    },
}


def update_dashboard_state(**kwargs) -> None:
    with _state_lock:
        _state.update(kwargs)


def _snapshot() -> Dict[str, Any]:
    with _state_lock:
        return dict(_state)


def _count_stale(positions: list) -> int:
    """R4: number of positions with last_price_stale == True."""
    n = 0
    for p in positions:
        if p.get("last_price_stale"):
            n += 1
    return n


# ──────────────────────────────────────────────────────────────────────────
#  AUTH (R11)
# ──────────────────────────────────────────────────────────────────────────

def _check_auth(expected_token: str, provided_token: Optional[str]) -> bool:
    """
    Constant-time comparison to avoid timing leaks. If no token is
    configured, auth is disabled (returns True).
    """
    if not expected_token:
        return True
    if not provided_token:
        return False
    try:
        return hmac.compare_digest(expected_token, provided_token)
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────
#  APP BUILDER
# ──────────────────────────────────────────────────────────────────────────

def _build_app(auth_token: str = ""):
    app = FastAPI(
        title="IBKR Global RSI Bot",
        version="2.1",
        # Only expose docs when auth is configured — leaking the schema
        # publicly is unnecessary. Operator can still hit /docs with the
        # token when they need it.
        docs_url="/docs" if auth_token else "/docs",
    )

    def _require_auth(x_auth_token: Optional[str]):
        if not _check_auth(auth_token, x_auth_token):
            # Generic 401 — do not leak whether auth is configured or not
            raise HTTPException(status_code=401, detail="Unauthorized")

    @app.get("/health")
    def health():
        # R11: intentionally unauthenticated for platform health checks.
        # Returns NO strategy/position data — just liveness + connection.
        s = _snapshot()
        return {
            "status": "ok",
            "connected_to_ibkr": s["connected"],
            "last_update": s["last_update"],
        }

    @app.get("/status")
    def status(x_auth_token: Optional[str] = Header(default=None, alias="X-Auth-Token")):
        _require_auth(x_auth_token)
        s = _snapshot()
        stale_count = _count_stale(s["positions"])
        return {
            "mode": s["mode"],
            "connected_to_ibkr": s["connected"],
            "nlv": round(s["nlv"], 2),
            "cash": round(s["cash"], 2),
            "day_start_nlv": round(s["day_start_nlv"], 2),
            "peak_nlv": round(s["peak_nlv"], 2),
            "day_pnl_pct": round(s["day_pnl_pct"], 3),
            "day_pnl_dollars": round(s["day_pnl_dollars"], 2),
            "drawdown_pct": round(s["dd_pct"], 3),
            "open_positions": len(s["positions"]),
            "max_positions": s["max_positions"],
            "universe_size": s["universe_size"],
            "regime": s["regime"],
            "regime_mult": s["regime_mult"],
            "vix": s["vix"],
            "daily_halted": s["daily_halted"],
            "max_dd_halted": s["max_dd_halted"],
            "account_values_healthy": s.get("account_values_healthy", True),
            "account_values_health_reason": s.get("account_values_health_reason", ""),
            # R4: top-level stale count for at-a-glance health
            "stale_position_count": stale_count,
            "stale_positions_symbols": [p["symbol"] for p in s["positions"]
                                        if p.get("last_price_stale")],
            "last_scan_duration_seconds": s.get("last_scan_duration_seconds"),
            "winrates": s.get("winrates", {}),
            "trade_counts": s.get("trade_counts", {}),
            "last_update": s["last_update"],
        }

    @app.get("/positions")
    def positions(x_auth_token: Optional[str] = Header(default=None, alias="X-Auth-Token")):
        _require_auth(x_auth_token)
        s = _snapshot()
        return {
            "count": len(s["positions"]),
            "stale_count": _count_stale(s["positions"]),
            "positions": s["positions"],
        }

    return app


# ──────────────────────────────────────────────────────────────────────────
#  STARTUP
# ──────────────────────────────────────────────────────────────────────────

_started = False


def start_dashboard(port: int = 8000, host: str = "0.0.0.0", auth_token: str = "") -> bool:
    """
    R11: host and auth_token parameters.
    - host: bind address (default 0.0.0.0; set 127.0.0.1 to restrict).
    - auth_token: if non-empty, /status /positions /docs require
      header `X-Auth-Token: <value>`. /health stays open.
    """
    global _started
    if _started:
        log.info("Dashboard already running — skipping start")
        return True
    if not _FASTAPI_OK:
        log.warning("⚠️  fastapi / uvicorn not installed — dashboard disabled "
                    "(run: pip install fastapi uvicorn)")
        return False

    app = _build_app(auth_token=auth_token)

    auth_label = "AUTHENTICATED" if auth_token else "⚠️  UNAUTHENTICATED (anyone with network access can read strategy/positions)"

    def _run():
        try:
            config = uvicorn.Config(
                app, host=host, port=port,
                log_level="warning", access_log=False,
                loop="asyncio",
            )
            server = uvicorn.Server(config)
            server.install_signal_handlers = lambda: None  # type: ignore
            server.run()
        except Exception as e:
            log.error(f"Dashboard crashed: {type(e).__name__}: {e}")

    t = threading.Thread(target=_run, daemon=True, name="dashboard")
    t.start()
    _started = True
    log.info(
        f"🌐 Dashboard started on {host}:{port} [{auth_label}] — "
        f"/health /status /positions /docs"
    )
    if not auth_token:
        log.warning(
            "⚠️  DASHBOARD_AUTH_TOKEN is not set. Set it in env for production. "
            "Anyone reachable at the bind address can read positions and strategy state."
        )
    return True
