# Global RSI Bot — IBKR (v2.3)

Mean-reversion RSI bot targeting ~79 stocks across US, ASX, UK, EU, HK, Canada (via SMART-routed duals), and Singapore. Server-side bracket orders (market parent + OCA take-profit + trailing stop). Volatility-adjusted position sizing, SPY+VIX regime filter, daily loss limit, max-drawdown auto-flatten, atomic state persistence, FastAPI dashboard.

> ⚠️  Real money is at risk whenever `TRADING_MODE=live`. Read this README end-to-end, then paper-trade at the same deposit size and currency as the intended live account for at least 48–72 hours before any live promotion.

---

## v2.3 changes vs v2.2

### Critical correctness fixes
- **C1** — Account-values health flag is now cycle-atomic. Previously any clean tag within `accountSummary` would silently un-poison the flag set by an earlier degraded tag, defeating R1's purpose. `collect_account_summary()` now resolves all tags and sets the module flag once per cycle.
- **C2** — `_place_bracket` has full rollback on any mid-sequence exception. No more half-submitted brackets leaving unprotected positions.
- **C3** — `_reconcile_post_sell` corroborates IBKR=0 / arithmetic>0 disagreements with recent fills before protecting a remainder. Prevents phantom-share re-attach when `_wait_for_fill` times out on a sell that actually completed.
- **C4 / M8** — `has_protective_orders` now verifies that BOTH an active TP (LMT SELL) AND a trailing stop (TRAIL SELL) cover the required qty. Half-protected positions (bare TP or bare trail from old sessions) are now detected and upgraded to a fresh full bracket instead of being silently accepted.

### High-severity fixes
- **H1 / M5** — `_state_lock` (RLock) guards all reads/writes of `bot_positions`, `trade_history`, `_last_prices`, `_last_price_ts`, `day_state`, `dd_state`. Dashboard snapshots are now consistent.
- **H2** — `_wait_for_fill` treats `PendingCancel` as terminal-dead. Closes a rare unprotected window.
- **H3** — `save_state` sends a critical Discord alert when trade-history serialisation fails. Silent degradation is no longer possible.
- **H6** — OCA group names use `uuid.uuid4().hex[:8]` suffix. Eliminates collision risk during mass reconcile.
- **H7** — `check_max_drawdown` treats symbols absent from IBKR at flatten time as already-closed (removes from state without recording a trade, since there's no fill price to attribute). Only true flatten failures are reported as "left behind."
- **H8** — `_verify_order_live` distinguishes insta-filled children from active ones. A TP/trail that fills during the verification window is flagged as protection-already-gone, not success.

### Medium / observability
- **M2** — Market-hours grace is split: zero on open-side (no pre-market risk), 5 min on close-side.
- **M7** — VIX gets its own tighter cache TTL (`VIX_CACHE_TTL=120`, vs `REGIME_CACHE_TTL=1800`).
- **M9** — Hard CRITICAL warning if the bot starts with `USE_BRACKET_ORDERS=False`.

### Startup self-test (new)
- Pre-flight checks before entering main loop. Fail-fast on: unhealthy account-values, SPY contract not qualifying, state file not writable, LIVE mode without `DASHBOARD_AUTH_TOKEN`, LIVE mode with `USE_BRACKET_ORDERS=False`. Set `STARTUP_SELF_TEST=0` to skip (dev only — never production).

### Ops & hygiene
- **L2** — `Dockerfile` pinned to `python:3.11.9-slim-bookworm`.
- **L3** — `docker-compose.yml` binds gateway ports to `127.0.0.1` only (bot reaches gateway via compose network — unaffected).
- **M6** — `backtest.py` excluded from production image (`.dockerignore`).
- Non-root user (`rsi`, uid 1001) in Dockerfile.
- `tini` as PID 1 for clean signal handling.

---

## Configuration summary

| Parameter | Value | Note |
|---|---|---|
| RSI oversold / overbought | 40 / 60 | Backtest-optimal |
| Default trailing stop | 6% | Floor — per-asset overrides allowed |
| Default take-profit | 8% | Per-asset overrides allowed |
| Max concurrent positions | 5 | |
| Base position size | 15% NLV | × regime × vol-scalar |
| Cash reserve floor | 20% | Buys blocked below |
| Max commission % | 3% of position | AUD throughout |
| Daily loss limit | 2% | Hard halt for the day |
| Max drawdown | 20% | Flatten + halt until `RESET_MAX_DD=1` (widened from 15% in external-review response to let brackets work first at small-account scale) |
| Daily reset TZ | America/New_York | Design: single-TZ window across all exchanges — see below |
| Scan interval | 15 min | |
| SPY regime cache | 30 min | |
| VIX cache | 2 min | v2.3 M7 — much tighter than SPY |

### Note on the single-TZ daily window (M4)
The daily loss-limit window rolls over at NY midnight by default. The bot trades on 8+ exchanges across many timezones — an ASX-morning loss followed by a NY-morning loss both count against the same "day" even though they span two Sydney calendar days. This is a **design choice**, not a bug: a single calendar window provides a clean, deterministic loss-limit envelope regardless of where trades happen. If you prefer a different reference, change `DAILY_RESET_TZ`.

### Exposure ceiling sanity (M3)
`MAX_POSITIONS × POSITION_SIZE_PCT × max(REGIME_MULT) × VOL_SCALAR_MAX = 5 × 0.15 × 1.0 × 1.8 = 1.35` — theoretically exceeds 100% NLV. The **cash-reserve guard in `cash_guard_check`** prevents actual over-deployment (buys are blocked once cash drops below 20% of NLV), but operators with larger accounts may want to reduce `VOL_SCALAR_MAX` or `POSITION_SIZE_PCT` for additional margin. The startup self-test logs this explicitly.

---

## Deployment

### Railway (recommended)
1. Create service from this repo.
2. Set env vars per `.env.example`:
   - `TWS_USERID`, `TWS_PASSWORD`
   - `TRADING_MODE=live` or `paper`
   - **`IB_GATEWAY_PORT=4003`** for live (socat port for live mode), **`4004`** for paper
   - `DASHBOARD_AUTH_TOKEN` — REQUIRED for live (openssl rand -hex 32)
   - `DISCORD_WEBHOOK` — strongly recommended
3. Deploy. On each redeploy of the gateway service, have the IBKR Key mobile app ready — 2FA prompt within 2–3 min or login fails.

### Local
```bash
cp .env.example .env
# Edit .env with your credentials
docker compose up --build
```

### Health check
- `GET /health` — unauthenticated, returns `{ok, connected, last_update, ...}`.
- `GET /status`, `/positions`, `/winrates`, `/trades` — require `X-Auth-Token: <your token>` header if `DASHBOARD_AUTH_TOKEN` set.

---

## Operations playbook

### Startup sequence
1. Load state from `bot_state.json` (positions + trade history + peak NLV + day state).
2. Run self-test (`STARTUP_SELF_TEST=1`, default). Aborts on failures.
3. Reconcile IBKR positions vs state:
   - Tracked and present → keep, re-attach bracket if half-protected (C4/M8).
   - Untracked orphans → skip (default) or adopt (if `ADOPT_ORPHAN=1`).
4. Enter main loop.

### Orphan adoption
If the bot restarts with IBKR positions it didn't know about (manual trades, previous session state loss):
- **Default**: orphans are ignored, logged as warnings, critical Discord alert sent. Operator can manually close them in IBKR.
- **With `ADOPT_ORPHAN=1`**: bot claims them, re-attaches a fresh bracket using current avg cost as the entry reference. **Unset the flag before next restart** — it's a one-shot.

### Halting and resuming
- **Daily loss limit** auto-clears at the next NY midnight rollover.
- **Max drawdown halt** persists across restarts. To resume: set `RESET_MAX_DD=1`, restart, then unset.
- **Operator kill-switch `HALT_NEW_BUYS=1`** — pauses new entries while letting exits, reconcile, bracket re-attach, dashboard snapshots and state persistence continue as normal. Use before FOMC, during post-incident review, or any time you want to quiesce entries without tearing the bot down. Orthogonal to the automatic daily-loss and max-drawdown halts. Flag is read at startup: set the env var and restart to engage, unset and restart to resume. Logged as `CRITICAL` in the startup banner and `WARNING` each cycle it's in effect.
- **Manual halt**: stop the service. State is persisted and will resume cleanly.

### Investigating a halt
1. Check Discord critical channel.
2. GET `/status` — `daily_halted` and `max_dd_halted` flags explain which limit hit.
3. Review `bot_state.json` — contains last-known positions, day state, peak NLV.

### Fill price discrepancies
The v2.3 C3 fix means: when IBKR reports a position closed but arithmetic suggests shares remain, the bot queries recent fills before deciding what to do. If this fires, you'll see a clear log message like:

```
🔍 AAPL reconcile disagreement (IBKR=0 vs arith=5) — corroborating via recent fills
✅ AAPL C3 corroboration: fills show 100 sold (≥ orig 100) — trusting IBKR's 0 remaining
```

If corroboration fails (no fills data, or fills agree with arithmetic), you get a `🚨` critical alert — **review immediately**. The bot will err on the side of protecting shares, but manual IBKR position inspection is warranted.

---

## Known limitations

- **IBKR fills are session-scoped.** After a reconnect, fills from before the disconnect are gone. An RSI-exit sell immediately before a disconnect will fall back to `external_close_estimated` in trade history. This understates accuracy of P&L attribution but does not affect position safety.
- **MCP tool only connected to the Alpaca Railway project.** IBKR env vars must be set via the Railway dashboard UI manually.
- **Commission filter at small accounts.** At ~$800 AUD, only SMART (~$1.55) and SGX (~$3.00) clear the 3% commission-of-position ceiling. As the account grows, more markets unlock automatically (ASX, LSE, SEHK, etc.).
- **Korea / India removed** — account type lacks market permissions. Do not re-add without verifying permissions in IBKR.
- **Canadian TSE** — use US-listed duals via SMART routing (ABX → GOLD). Direct TSE incurs a costly market-data subscription.

---

## Testing before live

1. Paper trade for 48–72 hours with `TRADING_MODE=paper` and the same deposit size+currency as the intended live account.
2. Monitor in Discord: `account_values_healthy` transitions, any `🚨` critical alerts, C3 reconciliation messages, bracket re-attach events.
3. Verify `GET /trades` shows reasonable win rate and P&L progression.
4. Confirm `/health` never returns `connected: false` for more than a few minutes.
5. Only after a clean run, promote: set `TRADING_MODE=live`, `IB_GATEWAY_PORT=4003`, `DASHBOARD_AUTH_TOKEN`, redeploy, and re-authenticate via IBKR Key app.

---

## File layout

```
.
├── config.py              — all parameters, thresholds, universe
├── ibkr_helpers.py        — connection, data, brackets, regime, guards
├── global_rsi_bot.py      — main loop, state, scan, entry, exits
├── dashboard.py           — FastAPI observability
├── backtest.py            — offline historical tester (NOT in prod image)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── .gitignore
├── .dockerignore
└── README.md
```
