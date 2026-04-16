# 🌍 Global RSI Bot v2.1 — IBKR

RSI-based mean-reversion bot targeting a ~79-stock universe across US, ASX,
LSE, XETRA, Paris, Amsterdam, HKEX, SGX, and Canada (via SMART).

---

## What's in v2.1

On top of v2 (vol-adjusted sizing, VIX-aware regime, daily Discord summary,
FastAPI dashboard), v2.1 adds:

- **Persistent trade history** stored in `bot_state.json` (FIFO-trimmed at
  10,000 records — ~3MB max).
- **Win-rate stats** across four windows (lifetime / 30d / 7d / 24h),
  surfaced on the `/status` dashboard and in the daily Discord summary.
- **External-close reconstruction** — when a bracket (TP or trailing stop)
  fills outside of our main loop, we pull the actual fill from
  `ib.fills()` to log an accurate closed-trade record.
- **Per-position `trail_pct` / `tp_pct` persistence** so exit-reason
  inference is stable across restarts.

---

## Files

| File                 | Role                                              |
|----------------------|---------------------------------------------------|
| `global_rsi_bot.py`  | Main loop, entries, exits, trade logging, state   |
| `ibkr_helpers.py`    | Connection, data, bracket orders, VIX regime      |
| `dashboard.py`       | FastAPI status server (port 8000)                 |
| `backtest.py`        | Historical simulation via yfinance                |
| `config.py`          | All thresholds, universe, per-asset config        |
| `Dockerfile`         | Python 3.12 slim image                            |
| `docker-compose.yml` | `ib-gateway` + `rsi-bot` services                 |
| `requirements.txt`   | Python dependencies                               |
| `.env.example`       | Env template — copy to `.env`                     |

---

## Quick start

### Paper (recommended first)

```bash
cp .env.example .env
# edit .env: TWS_USERID, TWS_PASSWORD, TRADING_MODE=paper, IB_GATEWAY_PORT=4004
docker compose up --build
```

Dashboard at <http://localhost:8000/status>.

### Live

```bash
# .env
TRADING_MODE=live
IB_GATEWAY_PORT=4003          # live mode uses socat port 4003
```

Then `docker compose up --build`. You'll need to complete IBKR 2FA in the
mobile app within ~2–3 min of gateway startup.

---

## Backtest

```bash
pip install -r requirements.txt
python backtest.py --start 2022-01-01 --end 2025-01-01 --capital 10000
python backtest.py --us-only --symbols AAPL MSFT NVDA
```

Writes `backtest_trades.csv` and `backtest_equity.csv`.

---

## Environment flags

| Var               | Default            | Purpose                                       |
|-------------------|--------------------|-----------------------------------------------|
| `TRADING_MODE`    | `paper`            | `paper` or `live`                             |
| `IB_GATEWAY_HOST` | `127.0.0.1`        | gateway host (compose: `ib-gateway`)          |
| `IB_GATEWAY_PORT` | `7497`             | `4004` paper / `4003` live                    |
| `IB_CLIENT_ID`    | `10`               | API client ID                                 |
| `IB_MARKET_DATA`  | `delayed`          | `live` or `delayed` (type 1 vs 3)             |
| `DAILY_RESET_TZ`  | `America/New_York` | when the daily rollover fires                 |
| `RESET_MAX_DD`    | _unset_            | `1` → clears persisted max-DD halt flag       |
| `BOT_STATE_FILE`  | `bot_state.json`   | where positions + trade history live          |
| `DISCORD_WEBHOOK` | _unset_            | Discord notifications (trades + daily summary)|
| `PORT`            | `8000`             | dashboard port (Railway sets this)            |

---

## Config (key defaults in `config.py`)

- `RSI_OVERSOLD = 40`, `RSI_OVERBOUGHT = 60`, period 14
- `DEFAULT_TRAILING_STOP = 6%`, `DEFAULT_TAKE_PROFIT = 8%`
- `POSITION_SIZE_PCT = 15%`, `MAX_POSITIONS = 5`, `CASH_RESERVE_PCT = 20%`
- `DAILY_LOSS_LIMIT_PCT = 2%`, `MAX_DRAWDOWN_PCT = 15%` (`FLATTEN_ON_MAX_DD = True`)
- `MAX_COMMISSION_PCT = 3%` — blocks live trades where estimated commission
  exceeds 3% of the position notional.
- Regime: `BULL` 100%, `CAUTION` 35%, `BEAR` 0% of base size
- Vol targeting: `VOL_TARGET_ANNUAL = 15%`, scalar clamped to `[0.4, 1.8]`
- `TRADE_HISTORY_MAX_SIZE = 10_000`

---

## Dashboard

Port 8000 (or `PORT` env var):

- `GET /health` — `{status, connected_to_ibkr, last_update}`
- `GET /status` — account + regime + open positions count + winrates
- `GET /positions` — full open-positions list with last price and PnL
- `GET /docs` — OpenAPI UI

Example `/status` winrate block:

```json
"winrates": {
  "lifetime":       54.2,
  "past_30_days":   57.1,
  "past_7_days":    50.0,
  "past_24_hours":  null
},
"trade_counts": { "lifetime": 48, "past_30_days": 21, "past_7_days": 6, "past_24_hours": 0 }
```

`null` = no closed trades in that window yet.

---

## Architecture notes

- **VIX fetch** uses `Index("VIX", "CBOE", "USD")` with `reqHistoricalData`.
  If the fetch fails the regime falls back to SPY-only (above 50MA+200MA →
  BULL, above 200MA → CAUTION, else BEAR).
- **Vol-scaling** is applied in `try_buy()` _after_ `POSITION_SIZE_PCT ×
  regime_mult`, so it multiplies through both other scalars.
- **Daily summary** is sent once per rollover (when `_today_in_reset_tz()`
  changes) from inside `roll_over_day_if_needed()` — before the day-state
  is reset, so it reports yesterday's P&L correctly.
- **Dashboard** runs in a daemon thread; state is updated once per scan
  cycle via `_push_dashboard_snapshot()` under a lock.
- **Trade logging**: RSI-triggered exits record directly from the sell
  result's `avg_fill_price`. Bracket / external closes use
  `get_recent_sell_fill()` with a 24h lookback, falling back to the last
  known market price (reason `external_close_estimated`).

---

## Known limitations

- Winrate = `pnl_pct > 0 / total closed trades`. Breakevens count as
  losses (pragmatic default).
- External-close reason is _inferred_ from price vs. thresholds — an OCA
  child that fills unusually at the boundary may be classified as
  `bracket_exit` rather than `take_profit`/`trailing_stop` specifically.
- Max 10,000 trades persisted; beyond that the oldest are FIFO-dropped.

---

## Safety

- Paper mode first, always.
- Bracket orders are server-side (OCA) — protection survives bot restarts.
- `save_state()` is atomic (`tmp` + `os.replace`).
- `FLATTEN_ON_MAX_DD=True` means the bot _will_ market-sell everything on
  a 15% drawdown. Set `RESET_MAX_DD=1` at startup to clear the halt.
