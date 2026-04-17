"""
Dashboard — FastAPI read-only observability (v2.3)
====================================================
Serves a snapshot of bot state via HTTP for operator visibility.

Endpoints:
  GET /health      — unauthenticated, for platform liveness checks
  GET /status      — auth-required (if DASHBOARD_AUTH_TOKEN set), JSON summary
  GET /positions   — auth-required, open positions snapshot
  GET /winrates    — auth-required, winrates + counts
  GET /trades      — auth-required, recent closed trades (last 200)

v2.3:
  - Bind host configurable (DASHBOARD_HOST)
  - Auth token enforced on all endpoints except /health
  - State is updated atomically via update_dashboard_state()
  - Running on a separate thread so main-loop I/O never blocks on HTTP
"""

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    from fastapi import FastAPI, Header, HTTPException, status
    from fastapi.responses import JSONResponse
    import uvicorn
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

log = logging.getLogger("ibkr-rsi")

# ══════════════════════════════════════════════════════════════════════════
#  SHARED STATE  (updated by main loop, read by HTTP handlers)
# ══════════════════════════════════════════════════════════════════════════

_state_lock = threading.RLock()
_state: Dict[str, Any] = {
    "mode": "UNKNOWN",
    "connected": False,
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
    "winrates": {},
    "trade_counts": {},
    "excluded_adopted_counts": {},
    "last_scan_duration_seconds": None,
    "last_update": None,
    "recent_trades": [],
}

# Optional: recent-trades ring buffer, populated externally via
# update_recent_trades(). Decoupled from main state for efficient access.
_recent_trades: List[Dict[str, Any]] = []
_recent_trades_max = 200


def update_dashboard_state(**kwargs):
    """Thread-safe atomic state replace. Called by main loop each cycle."""
    with _state_lock:
        _state.update(kwargs)


def update_recent_trades(trades: List[Dict[str, Any]]):
    """Push a fresh slice of the most recent closed trades (main loop)."""
    with _state_lock:
        _recent_trades.clear()
        _recent_trades.extend(trades[-_recent_trades_max:])


def _snapshot() -> Dict[str, Any]:
    with _state_lock:
        return dict(_state)


def _snapshot_trades() -> List[Dict[str, Any]]:
    with _state_lock:
        return list(_recent_trades)


# ══════════════════════════════════════════════════════════════════════════
#  AUTH
# ══════════════════════════════════════════════════════════════════════════

_AUTH_TOKEN: str = ""


def _check_auth(x_auth_token: Optional[str]):
    if not _AUTH_TOKEN:
        return  # no token configured — open access (but we warn at startup)
    if not x_auth_token or x_auth_token != _AUTH_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Auth-Token header",
        )


# ══════════════════════════════════════════════════════════════════════════
#  FASTAPI APP
# ══════════════════════════════════════════════════════════════════════════

def _build_app() -> "FastAPI":
    app = FastAPI(
        title="IBKR Global RSI Bot Dashboard",
        version="2.3",
        docs_url=None,
        redoc_url=None,
    )

    @app.get("/health")
    def health():
        snap = _snapshot()
        # /health intentionally unauthenticated for platform liveness probes.
        # It returns minimal info — no positions, no financials.
        return {
            "ok": True,
            "connected": snap.get("connected"),
            "last_update": snap.get("last_update"),
            "account_values_healthy": snap.get("account_values_healthy"),
            "daily_halted": snap.get("daily_halted"),
            "max_dd_halted": snap.get("max_dd_halted"),
        }

    @app.get("/status")
    def get_status(x_auth_token: Optional[str] = Header(default=None)):
        _check_auth(x_auth_token)
        snap = _snapshot()
        snap.pop("positions", None)  # separate endpoint
        snap.pop("recent_trades", None)
        return snap

    @app.get("/positions")
    def get_positions(x_auth_token: Optional[str] = Header(default=None)):
        _check_auth(x_auth_token)
        snap = _snapshot()
        return {
            "last_update": snap.get("last_update"),
            "count": len(snap.get("positions", [])),
            "max_positions": snap.get("max_positions"),
            "positions": snap.get("positions", []),
        }

    @app.get("/winrates")
    def get_winrates(x_auth_token: Optional[str] = Header(default=None)):
        _check_auth(x_auth_token)
        snap = _snapshot()
        return {
            "winrates": snap.get("winrates", {}),
            "counts": snap.get("trade_counts", {}),
            # Trades excluded from short-window winrates because the
            # position was adopted via ADOPT_ORPHAN (entry = avg_cost,
            # not a real signal fill). Lifetime + 30d are unaffected.
            "excluded_adopted_counts": snap.get("excluded_adopted_counts", {}),
            "last_update": snap.get("last_update"),
        }

    @app.get("/trades")
    def get_trades(x_auth_token: Optional[str] = Header(default=None)):
        _check_auth(x_auth_token)
        trades = _snapshot_trades()
        return {
            "count": len(trades),
            "trades": trades,
        }

    return app


# ══════════════════════════════════════════════════════════════════════════
#  SERVER BOOTSTRAP
# ══════════════════════════════════════════════════════════════════════════

_server_thread: Optional[threading.Thread] = None


def start_dashboard(port: int, host: str = "0.0.0.0", auth_token: str = ""):
    """
    Start the dashboard in a daemon thread. Safe to call once.

    If FastAPI/uvicorn are not installed, dashboard is disabled silently.
    If auth_token is empty, logs a warning (production should always set it).
    """
    global _server_thread, _AUTH_TOKEN

    if not _FASTAPI_AVAILABLE:
        log.warning("Dashboard disabled: fastapi/uvicorn not installed")
        return

    if _server_thread is not None and _server_thread.is_alive():
        log.info("Dashboard already running")
        return

    _AUTH_TOKEN = auth_token or ""

    if not _AUTH_TOKEN:
        log.warning(
            "⚠️  Dashboard AUTH_TOKEN is empty — /status, /positions, /winrates, "
            "/trades are OPEN. Set DASHBOARD_AUTH_TOKEN in env for production. "
            "Generate with: openssl rand -hex 32"
        )
    else:
        log.info(f"🔒 Dashboard auth token configured ({len(_AUTH_TOKEN)} chars)")

    if host == "0.0.0.0":
        log.warning(
            f"🌐 Dashboard binding to 0.0.0.0:{port} — reachable from any "
            f"interface. Restrict via DASHBOARD_HOST=127.0.0.1 if behind a "
            f"reverse proxy or private network."
        )

    app = _build_app()

    def _serve():
        try:
            config = uvicorn.Config(
                app,
                host=host,
                port=port,
                log_level="warning",
                access_log=False,
                loop="asyncio",
            )
            server = uvicorn.Server(config)
            server.run()
        except Exception as e:
            log.error(f"Dashboard server failed: {e}")

    _server_thread = threading.Thread(
        target=_serve, daemon=True, name="dashboard-http"
    )
    _server_thread.start()
    log.info(f"📊 Dashboard listening on http://{host}:{port}")
