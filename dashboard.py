"""
Dashboard — Tiny FastAPI status server
========================================
Exposes /health, /status, /positions on port DASHBOARD_PORT.
Runs in a background daemon thread. Reads a thread-safe snapshot of bot
state that the main loop updates via `update_dashboard_state()`.

/status includes winrate stats across four time windows:
  lifetime, past_30_days, past_7_days, past_24_hours.

Degrades gracefully if fastapi / uvicorn aren't installed.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

log = logging.getLogger("ibkr-rsi")

try:
    from fastapi import FastAPI
    import uvicorn
    _FASTAPI_OK = True
except ImportError:
    _FASTAPI_OK = False
    FastAPI = None        # type: ignore
    uvicorn = None        # type: ignore


# ──────────────────────────────────────────────────────────────────────────
#  SHARED STATE — updated by the bot thread, read by the HTTP handlers.
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
    "positions": [],           # list of dicts (no contract objects)
    "universe_size": 0,
    "max_positions": 0,
    "last_update": None,
    "connected": False,
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
    """Atomically merge kwargs into the shared state dict."""
    with _state_lock:
        _state.update(kwargs)


def _snapshot() -> Dict[str, Any]:
    with _state_lock:
        return dict(_state)


# ──────────────────────────────────────────────────────────────────────────
#  APP BUILDER
# ──────────────────────────────────────────────────────────────────────────

def _build_app():
    app = FastAPI(title="IBKR Global RSI Bot", version="2.1", docs_url="/docs")

    @app.get("/health")
    def health():
        s = _snapshot()
        return {
            "status": "ok",
            "connected_to_ibkr": s["connected"],
            "last_update": s["last_update"],
        }

    @app.get("/status")
    def status():
        s = _snapshot()
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
            "winrates": s.get("winrates", {
                "lifetime": None,
                "past_30_days": None,
                "past_7_days": None,
                "past_24_hours": None,
            }),
            "trade_counts": s.get("trade_counts", {
                "lifetime": 0,
                "past_30_days": 0,
                "past_7_days": 0,
                "past_24_hours": 0,
            }),
            "last_update": s["last_update"],
        }

    @app.get("/positions")
    def positions():
        s = _snapshot()
        return {"count": len(s["positions"]), "positions": s["positions"]}

    return app


# ──────────────────────────────────────────────────────────────────────────
#  STARTUP
# ──────────────────────────────────────────────────────────────────────────

_started = False


def start_dashboard(port: int = 8000) -> bool:
    """
    Start the dashboard in a background daemon thread.
    Safe to call once at bot startup. Returns True on success.
    """
    global _started
    if _started:
        log.info("Dashboard already running — skipping start")
        return True
    if not _FASTAPI_OK:
        log.warning("⚠️  fastapi / uvicorn not installed — dashboard disabled "
                    "(run: pip install fastapi uvicorn)")
        return False

    app = _build_app()

    def _run():
        try:
            config = uvicorn.Config(
                app, host="0.0.0.0", port=port,
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
    log.info(f"🌐 Dashboard started on port {port} — /health /status /positions /docs")
    return True
