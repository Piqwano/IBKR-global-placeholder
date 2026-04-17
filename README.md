# Global RSI Bot — IBKR (v2.1 + R-hardening)

Mean-reversion bot targeting ~137 stocks across US, ASX, LSE, XETRA,
Euronext (Paris / Amsterdam), SEHK, SGX, and SMART-routed Canadian
duals. Buys RSI ≤ 40, exits at RSI ≥ 60, 6% trailing stop, or 8%
take-profit.

## Version history

| Round | Scope |
|---|---|
| **v2** | Vol-adjusted sizing, VIX-aware regime, FastAPI dashboard, daily Discord summary |
| **v2.1 (B1–B4)** | BASE-currency account resolution, TP repair via in-place amend, position-aware fill lookup, safe VIX fallback |
| **v2.1 (H1–H4)** | Dashboard price staleness, trade-history NaN/Inf sanitisation, partial-sell defensive reconcile, ADOPT_ORPHAN gate |
| **v2.1 + R** | See "R-round" table below |

## R-round hardening

| ID | Severity | Change |
|---|---|---|
| **R1** | HIGH | `_resolve_account_value` sets a module-level health flag. `try_buy` blocks new buys while degraded. Flag surfaces on `/status`. |
| **R2** | MED | `_modify_tp_limit_price` polls order status post-submit. If the amend is rejected (tick-size edge), the stored `tp_order_id` stays pointing at the original (still-live) TP. |
| **R3** | LOW | `_record_external_close` correctly clamps `exit_qty = min(tracked, fill_qty)` and warns if `fill_qty > tracked_qty` (state drift). |
| **R4** | MED | `/status` exposes top-level `stale_position_count` + `stale_positions_symbols` so staleness is visible in aggregate. |
| **R5** | LOW | `compute_winrates` counts unparseable `closed_at` records once upfront, then filters a pre-validated list per window. |
| **R6** | **CRITICAL** | `_reconcile_post_sell` prefers arithmetic remainder when IBKR returns 0 but `orig_qty - filled_qty > 0`. Previously trusted IBKR's 0, which on a race could leave shares untracked. |
| **R7** | **CRITICAL** | `buy_stock_bracket` partial-fill branch now places the new reduced-qty bracket BEFORE cancelling the old one. Closes the ~1.5s unprotected window that B2's own philosophy rejected. |
| **R8** | MED | `has_protective_orders` filters on active order statuses (Submitted/PreSubmitted/Accepted) AND requires summed qty ≥ required. Prevents orphan-adoption path from skipping re-attachment when an old order is cancelled-but-not-yet-cleared. |
| **R9** | MED | `_verify_order_live` polls `orderStatus` after every placement/amendment (up to 2s). Catches silent rejections before the caller trusts the order ID. |
| **R10** | MED | Max-DD flatten removes only positions where `sell_stock` confirmed close. Leftovers stay in `bot_positions` for next-cycle reconcile instead of being silently forgotten. |
| **R11** | HIGH | Dashboard now supports `DASHBOARD_HOST` (bind address) and `DASHBOARD_AUTH_TOKEN` (header auth). `/health` stays unauthenticated; `/status`, `/positions`, `/docs` require `X-Auth-Token` when token is set. |
| **R12** | LOW | Reconnect clears `_regime_cache` and `_vix_cache` in addition to `_contract_cache`. |
| **R13** | LOW | `scan_all_markets` logs `scan_duration_seconds` and surfaces on `/status`. Warns if scan exceeds 50% of `SCAN_INTERVAL_SECS`. |
| — | LOW | Discord non-critical rate-limit (`DISCORD_NON_CRITICAL_RATE_PER_MIN`=15). Critical alerts (max-DD, daily loss, rejected orders, incomplete flatten) always go through. |

## Files

| File | Role |
|---|---|
| `config.py` | Tunables, universe, sessions, risk limits. |
| `ibkr_helpers.py` | IBKR connection, bars/prices, bracket orders, VIX, regime, health flag. |
| `global_rsi_bot.py` | Main loop. Entry point. Vol sizing, daily summary, Discord notify. |
| `dashboard.py` | FastAPI app on a background daemon thread. |
| `backtest.py` | Historical backtest via yfinance (v2 logic; vol sizing not yet ported — unchanged from your existing file, keep as-is). |
| `Dockerfile` | Bot container. |
| `docker-compose.yml` | Bot + `ghcr.io/gnzsnz/ib-gateway:stable`. |
| `.env.example` | Environment variable template. |

## Quick start

### Paper trading

```bash
cp .env.example .env        # edit TWS_USERID/TWS_PASSWORD + DASHBOARD_AUTH_TOKEN
docker compose up -d
docker compose logs -f rsi-bot

# Dashboard (replace TOKEN with whatever you set in .env)
curl localhost:8000/health                                        # no auth
curl -H "X-Auth-Token: TOKEN" localhost:8000/status | jq          # auth required
curl -H "X-Auth-Token: TOKEN" localhost:8000/positions | jq       # auth required
```

### Live trading

Edit `.env`:

```bash
TRADING_MODE=live
IB_GATEWAY_PORT=4003          # live mode socat port (paper is 4004)
DASHBOARD_AUTH_TOKEN=<generate with: openssl rand -hex 32>
```

Then `docker compose down && docker compose up -d`.

⚠️ IBKR live requires 2FA via the IBKR Key mobile app within ~3 min of
each redeploy. The gateway image logs prompts to `docker compose logs
-f ib-gateway`.

### Backtesting

```bash
pip install -r requirements.txt
python backtest.py                                   # 3 years, full universe
python backtest.py --us-only --capital 10000
```

## Env vars

| Var | Default | Purpose |
|---|---|---|
| `TRADING_MODE` | `paper` | `paper` or `live` |
| `IB_GATEWAY_PORT` | `7497` | `4004` paper, `4003` live |
| `IB_GATEWAY_HOST` | `127.0.0.1` | Gateway host (`ib-gateway` in compose) |
| `IB_CLIENT_ID` | `10` | IB API client ID |
| `IB_MARKET_DATA` | `delayed` | `delayed` (free) or `live` (paid) |
| `DAILY_RESET_TZ` | `America/New_York` | Timezone for daily P&L rollover |
| `RESET_MAX_DD` | *(empty)* | Set to `1` to clear persisted max-DD halt |
| `BOT_STATE_FILE` | `bot_state.json` | State file path |
| `ADOPT_ORPHAN` | *(empty)* | Set to `1` to adopt pre-existing IBKR positions |
| `DISCORD_WEBHOOK` | *(empty)* | Webhook for trade + daily summary |
| `PORT` | `8000` | Dashboard port (Railway sets this automatically) |
| `DASHBOARD_HOST` | `0.0.0.0` | **R11**: Bind address. Set to `127.0.0.1` for loopback-only |
| `DASHBOARD_AUTH_TOKEN` | *(empty)* | **R11**: Header token for `/status`, `/positions`, `/docs` |

## Dashboard endpoints

| Endpoint | Auth | Content |
|---|---|---|
| `GET /health` | none | `{"status": "ok", "connected_to_ibkr": bool, "last_update": "..."}` |
| `GET /status` | token | NLV, cash, day PnL, drawdown, regime, VIX, halts, **stale_position_count**, **last_scan_duration_seconds**, **account_values_healthy**, winrates |
| `GET /positions` | token | List of open positions with entry, last price, stale flag, PnL |
| `GET /docs` | token | Interactive Swagger UI |

Auth is the header `X-Auth-Token: <your token>`. Constant-time
comparison via `hmac.compare_digest`. Configure the token via the
`DASHBOARD_AUTH_TOKEN` env var.

**If you deploy without a token, you get a loud warning on startup and
the token check is skipped.** Do this only for local dev; never on
Railway or any publicly reachable host.

## Architecture notes

### Account-values health flag (R1)

`_resolve_account_value` now maintains a module-level flag. It flips
False whenever BASE resolution fails or falls back to a per-currency
value, and flips True on a clean BASE resolution. `try_buy` calls
`account_values_healthy()` before sizing; if unhealthy, the buy is
skipped with a log line. This is defence-in-depth on top of
`cash_guard_check`'s existing check. The flag is also surfaced on
`/status` as `account_values_healthy` + `account_values_health_reason`.

### Partial-fill bracket repair (R7)

Previously, partial fills on a bracket parent cancelled the oversized
OCA children and then placed a correctly-sized replacement — opening
a ~1.5s window where filled shares had no TP and no trailing stop.
Now:

1. Detect partial fill.
2. Place new reduced-qty child bracket (`_place_child_bracket_orders`).
3. Verify it's live via `_verify_order_live`.
4. ONLY THEN cancel the old parent + old children.

Worst case: ~2s of double-cover where both the old oversized and new
correct brackets exist. If one fires, the other gets cancelled by the
OCA group (old children) or rejected by IBKR (over-sell). A gap is
unacceptable; double-cover is tolerable.

### Post-sell reconciliation (R6)

`_reconcile_post_sell` now uses a decision matrix:

| IBKR report | Arithmetic remainder | Action |
|---|---|---|
| `None` (query failed) | any | Use arithmetic (existing H3 behaviour) |
| `0` | `0` | Trust: full close |
| `0` | `> 0` | **Prefer arithmetic** — protect unknown shares, critical notify |
| `> 0` | any | Use IBKR value (authoritative when positive) |

The third row is the R6 change. IBKR's 0 may be a stale read taken
just before the partial-fill event propagated. Arithmetic has a hard
floor: we asked for N, only M filled, so N−M are somewhere. Keep the
bracket on them.

### Order verification (R9)

`_verify_order_live` polls `orderStatus` for up to
`ORDER_PLACEMENT_VERIFY_SECS` (default 2s) after any placement or
amendment. Returns True only if the order reaches an active status
(Submitted / PreSubmitted / Accepted) or Filled. Returns False on
Cancelled / Inactive / timeout. Used in:

- `_modify_tp_limit_price` (TP amend)
- `_place_child_bracket_orders` (attach + partial-repair paths)

### Max-DD flatten (R10)

`flatten_all_positions` now returns
`{symbol: {success, filled_qty, requested_qty, avg_fill_price}}`.
`check_max_drawdown` iterates this and removes only positions where
`success=True AND filled_qty > 0`. Leftovers:

- Stay in `bot_positions` (preserve state).
- No trade recorded (we didn't actually close them).
- Trigger a critical Discord alert listing unconfirmed symbols.
- Get picked up on next cycle by `check_exits` / orphan path.

### Discord rate limiting

Non-critical notifications (buy, sell, generic exit) are dropped if more
than `DISCORD_NON_CRITICAL_RATE_PER_MIN` (default 15) have gone out in
the last 60s. Critical alerts (max-DD, daily loss limit, account-values
degraded, incomplete flatten, order rejection) always go through.
Prevents a cascade of position-closing notifies from crowding out the
"MAX DD HIT" alert the operator needs to see.

## Known limitations (unchanged)

- LSE tick-size grids finer than $0.01 may occasionally reject TP
  limits. R9 now surfaces these instead of silently trusting the
  rejected order ID.
- yfinance backtest merges trading days across markets; local-holiday
  signals can silently drop. Live bot unaffected.
- No US-holiday calendar; the bot runs scans on MLK Day etc. and
  simply gets no signals. Safe but noisy logs.
- Vol-scaling not yet applied in `backtest.py`.

## Safety

Run paper for at least 48–72 hours. Watch for:

- `/status` showing `account_values_healthy: true` consistently.
- `/status` showing `stale_position_count: 0`.
- `/status` showing `last_scan_duration_seconds` well under
  `SCAN_INTERVAL_SECS` (default 900).
- Daily summary arriving at `DAILY_RESET_TZ` midnight.
- `_manager` log lines showing "Caches cleared: contracts, regime, VIX"
  on every reconnect.
- Critical Discord alerts on any incomplete flatten or reconcile
  disagreement — if these fire during paper, investigate before going
  live.

Then switch to live with a small deposit.
