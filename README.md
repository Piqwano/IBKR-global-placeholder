# Global RSI Bot — IBKR (v2)

Mean-reversion bot targeting ~137 stocks across US, ASX, LSE, XETRA, Euronext
(Paris / Amsterdam), SEHK, and SGX. Buys RSI ≤ 40, exits at RSI ≥ 60, 6%
trailing stop, or 8% take-profit.

## What's new in v2

| Feature | Description |
|---------|-------------|
| **Vol-adjusted sizing** | Each position is scaled by `VOL_TARGET_ANNUAL / (ATR%·√252)`, clamped to `[0.4, 1.8]`. Low-vol names get boosted, high-vol names shrunk — roughly equalising per-position risk contribution. |
| **VIX-aware regime** | BULL = SPY > 200MA *and* VIX < 20. CAUTION = elevated VIX or SPY dip with tame VIX. BEAR = SPY < 200MA *and* VIX ≥ 22. Falls back to SPY-only if VIX fetch fails. |
| **Daily Discord summary** | One clean message per day at rollover: PnL $/%, DD%, positions, exposure %, regime, NLV. Per-trade notifications are unchanged. |
| **FastAPI dashboard** | `/health`, `/status`, `/positions`, `/docs` on port 8000 (Railway-exposed). Snapshot is written by the main loop — no extra IBKR calls from HTTP handlers. |

## v1 features retained

- **Server-side bracket orders** — atomic parent MKT + OCA TP + trailing stop.
- **Partial fill handling** — resizes OCA children to filled qty.
- **TP repair after fill** — replaces TP limit if drift > 0.2%.
- **Bracket re-attachment on restart** — naked positions get protected automatically.
- **Market-hours gating** with per-exchange lunch breaks.
- **Daily & max-drawdown halts** — −2% daily floor, −15% from peak flattens all.
- **Correlation caps** — 2 positions per correlation group.
- **State persistence** — atomic JSON write, reload on startup.

## Files

| File               | Role |
|--------------------|------|
| `config.py`        | All tunables — universe, sessions, risk limits, v2 settings. |
| `ibkr_helpers.py`  | IBKR connection mgmt, bars/prices, bracket orders, VIX fetch, regime. |
| `global_rsi_bot.py`| Main loop. Entry point. Vol sizing + daily summary live here. |
| `dashboard.py`     | Tiny FastAPI app. Background thread on port 8000. |
| `backtest.py`      | Historical backtest via yfinance (v1 logic; vol sizing not yet ported). |
| `Dockerfile`       | Bot container. |
| `docker-compose.yml` | Bot + `ghcr.io/gnzsnz/ib-gateway:stable`. |

## Quick start

### Paper trading

```bash
cp .env.example .env        # edit TWS_USERID/TWS_PASSWORD
docker compose up -d
docker compose logs -f rsi-bot

# Dashboard
curl localhost:8000/health
curl localhost:8000/status | jq
```

### Live trading

Edit `.env`:
```
TRADING_MODE=live
IB_GATEWAY_PORT=4003
```
Then `docker compose down && docker compose up -d`.
⚠️ IBKR live requires 2FA via the IBKR Key mobile app within ~3 min of each redeploy.

### Backtesting

```bash
pip install -r requirements.txt
python backtest.py                                   # 3 years, full universe
python backtest.py --us-only --capital 10000
```

## Env flags (no new env vars added in v2)

| Var                   | Default           | Purpose |
|-----------------------|-------------------|---------|
| `TRADING_MODE`        | `paper`           | `paper` or `live` |
| `IB_GATEWAY_PORT`     | `7497`            | `4004` paper, `4003` live |
| `IB_GATEWAY_HOST`     | `127.0.0.1`       | Gateway host (`ib-gateway` in compose) |
| `IB_CLIENT_ID`        | `10`              | IB API client ID |
| `IB_MARKET_DATA`      | `delayed`         | `delayed` (free) or `live` (paid) |
| `DAILY_RESET_TZ`      | `America/New_York`| Timezone for daily P&L rollover |
| `RESET_MAX_DD`        | *(empty)*         | Set to `1` to clear persisted max-DD halt |
| `BOT_STATE_FILE`      | `bot_state.json`  | State file path |
| `DISCORD_WEBHOOK`     | *(empty)*         | Webhook for trade + daily summary notifications |
| `PORT`                | `8000`            | Optional — if Railway sets `PORT`, dashboard uses it; else 8000 |

## v2 configuration (in `config.py`)

```python
# Vol-adjusted sizing
USE_VOL_ADJUSTED_SIZING = True
VOL_TARGET_ANNUAL = 0.15
ATR_PERIOD_FOR_SIZING = 20
VOL_SCALAR_MIN = 0.4
VOL_SCALAR_MAX = 1.8

# VIX-aware regime
USE_VIX_IN_REGIME = True
VIX_BULL_MAX = 20.0
VIX_CAUTION_MAX_ABOVE_200MA = 25.0
VIX_CAUTION_MAX_BELOW_200MA = 22.0

# Daily summary
SEND_DAILY_SUMMARY = True

# Dashboard
DASHBOARD_ENABLED = True
DASHBOARD_PORT = int(os.getenv("PORT", "8000"))
```

## Dashboard endpoints

- `GET /health` → `{"status": "ok", "connected_to_ibkr": bool, "last_update": "..."}`
- `GET /status` → NLV, cash, day PnL $/%, drawdown %, regime, VIX, halts
- `GET /positions` → list of open positions with entry, last price, PnL
- `GET /docs` → interactive Swagger UI

## Architecture notes

### VIX fetch

Uses `Index("VIX", "CBOE", "USD")` via `reqHistoricalData` (1-day bars, 5-day
lookback, whatToShow="TRADES"). Cached for 30 min via the connection
manager. If the fetch returns empty or errors (typical: no CBOE data
subscription), `get_market_regime()` silently falls back to the v1 SPY-only
logic (above 50MA+200MA → BULL, above 200MA → CAUTION, else BEAR).

### Vol-scaling application point

Applied **after** `base_amount = portfolio × POSITION_SIZE_PCT × regime_mult`
and **before** the commission filter. Clamping to `[0.4, 1.8]` prevents
outsized sizing on truly flat (low-vol) names and avoids shrinking
genuinely high-vol names to meaningless dollar amounts. The commission
filter therefore checks the adjusted amount.

### Daily summary timing

Sent from `roll_over_day_if_needed` **before** `day_state` is reset, so the
message uses yesterday's `start_nlv` and date. First run (where
`day_state["date"] is None`) suppresses the summary. Subsequent rollovers
fire exactly once per day at the first scan crossing `DAILY_RESET_TZ`
midnight.

### Dashboard concurrency

`uvicorn.Server` runs in a daemon thread. `install_signal_handlers` is
no-op'd because Python only allows signal installation from the main
thread. The main loop writes to `dashboard._state` under a `threading.Lock`
on every scan iteration; HTTP handlers read a dict snapshot under the
same lock. No extra IBKR calls happen from the HTTP thread — everything
served is cached state.

## Known limitations

- LSE tick-size grids finer than $0.01 may occasionally reject TP limits.
- yfinance backtest merges trading days across markets; local-holiday
  signals can silently drop. Live bot unaffected.
- Vol-scaling not yet applied in `backtest.py` — compare expected vs
  realised after a week of paper trading.

## Safety

Run paper for at least a week. Watch for:
- Daily summary arriving at the configured `DAILY_RESET_TZ` midnight
- `/status` endpoint responding with expected values
- Vol-scalar log lines on buys (`📐 SYMBOL: ATR20=... → scalar=...`)
- Regime lines showing VIX level (`Regime: BULL | SPY $... | VIX 15.3`)

Then switch to live with a small deposit.
