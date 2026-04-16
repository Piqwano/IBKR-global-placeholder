# Global RSI Bot — IBKR

Mean-reversion bot targeting ~137 stocks across US, ASX, LSE, XETRA, Euronext
(Paris / Amsterdam), SEHK, and SGX. Buys RSI ≤ 40, exits at RSI ≥ 60, 6%
trailing stop, or 8% take-profit.

## Key features

- **Server-side bracket orders** — every buy submits an atomic parent market
  order + OCA take-profit limit + OCA trailing stop. If the bot crashes, TP
  and trailing stops remain active at IBKR.
- **Partial fill handling** — if a market order partial-fills, the parent
  remainder is cancelled and the OCA children are re-sized to the actual
  filled quantity.
- **TP repair after fill** — once the parent market order fills, the TP limit
  is recalculated against the actual fill price and replaced if it drifted
  more than 0.2% from the pre-fill estimate.
- **Bracket re-attachment on restart** — any position found at startup that
  is missing a protective sell order gets one attached.
- **Market-hours gating** — per-exchange sessions with lunch breaks (e.g.
  SEHK 12:00–13:00 HKT).
- **Daily & max-drawdown halts** — −2% daily floor stops new buys for the
  day, −15% from peak NLV flattens everything and requires `RESET_MAX_DD=1`
  to resume.
- **State persistence** — positions and halt flags are persisted to
  `bot_state.json` atomically and reloaded on startup.
- **Regime-aware sizing** — BULL 100%, CAUTION 35%, BEAR 0%.
- **Correlation caps** — no more than 2 positions per correlation group
  (e.g. us_big_tech, au_miners).

## Files

| File               | Role |
|--------------------|------|
| `config.py`        | All tunables — thresholds, universe, sessions, risk limits. |
| `ibkr_helpers.py`  | IBKR connection mgmt, price/bars fetching, bracket orders. |
| `global_rsi_bot.py`| Main loop. Entry point. |
| `backtest.py`      | Historical backtest using yfinance data. |
| `Dockerfile`       | Bot container. |
| `docker-compose.yml` | Bot + `ghcr.io/gnzsnz/ib-gateway:stable`. |

## Quick start

### 1. Paper trading (recommended first)

```bash
cp .env.example .env
# edit .env — set TWS_USERID, TWS_PASSWORD, keep TRADING_MODE=paper
docker compose up -d
docker compose logs -f rsi-bot
```

### 2. Live trading

Edit `.env`:
```
TRADING_MODE=live
IB_GATEWAY_PORT=4003
```

Redeploy:
```bash
docker compose down
docker compose up -d
```

⚠️ **IBKR live mode requires 2FA approval via the IBKR Key mobile app within
2–3 minutes of each redeploy.** Have the app ready.

### 3. Backtesting

```bash
pip install -r requirements.txt
python backtest.py                                   # last 3 years, full universe
python backtest.py --start 2020-01-01 --capital 10000
python backtest.py --us-only --symbols AAPL MSFT NVDA
python backtest.py --fill-mode same_close            # legacy close-of-signal-day fill
```

Outputs:
- `backtest_trades.csv` — per-trade detail with commission + slippage
- `backtest_equity.csv` — daily equity curve

## Env flags

| Var                   | Default           | Purpose |
|-----------------------|-------------------|---------|
| `TRADING_MODE`        | `paper`           | `paper` or `live` |
| `IB_GATEWAY_PORT`     | `7497`            | `4004` paper, `4003` live (via socat) |
| `IB_GATEWAY_HOST`     | `127.0.0.1`       | Gateway host (`ib-gateway` in docker compose) |
| `IB_MARKET_DATA`      | `delayed`         | `delayed` (free) or `live` (paid subscription) |
| `DAILY_RESET_TZ`      | `America/New_York`| Timezone for day roll-over |
| `RESET_MAX_DD`        | *(empty)*         | Set to `1` to clear a persisted max-DD halt on restart |
| `BOT_STATE_FILE`      | `bot_state.json`  | State file path |
| `DISCORD_WEBHOOK`     | *(empty)*         | Optional webhook for trade notifications |

## Architecture notes

### Bracket flow

1. `buy_stock_bracket` places `[parent_MKT, tp_LMT, trail_TRAIL]`. Parent and
   first child have `transmit=False`; the trailing stop has `transmit=True`,
   which atomically activates all three at IBKR.
2. `_wait_for_fill` waits for terminal status of the parent.
3. Three outcomes:
   - **Zero fill / rejected** → cancel children, return None
   - **Partial fill** → cancel parent remainder and both children, place a
     new correctly-sized OCA pair
   - **Full fill** → optionally repair TP price (if slippage drift > 0.2%)
     by cancelling BOTH old children first, then placing a new OCA pair
     (brief ~2s unprotected window, accepted to avoid duplicate trailing
     stops racing in parallel).

### Exit flow

- **Bracket exit** (TP hit, trail hit, or manual IBKR close): the reconcile
  phase of `check_exits` notices the position is gone from IBKR and pops it.
- **RSI ≥ 60 exit**: cancel OCA siblings first, then market-sell. If the
  sell is partial, wait 1.5s, query remaining qty, and re-attach a fresh
  bracket.

### Halts

- **Daily loss limit** (−2%): halts new buys, preserves positions. Resets on
  next `DAILY_RESET_TZ` midnight.
- **Max drawdown** (−15% from NLV peak): flattens all, cancels everything,
  persists `hit_max_dd=True` to state file. Requires `RESET_MAX_DD=1` env
  var on restart to clear.

## Known limitations

- LSE / some EU exchanges have tick-size grids finer than $0.01; the TP
  limit price rounds to 2 decimals. If IBKR rejects on tick mismatch, the
  order logs a reject — position is entered without bracket protection only
  in the partial-fill-failed edge case.
- Backtest uses yfinance data and merges trading days across markets;
  signals that land on local holidays can silently drop. Live bot is
  unaffected.
- The RSI-exit vs bracket-fill path has a brief window where a bracket
  could fill while the bot is evaluating the RSI. The bot self-heals via
  reconciliation on the next iteration.

## Safety

Start in paper mode. Let the bot run for at least one full week of scans
and observe:
- state file is being updated
- bracket orders are visible in the IBKR TWS/Web UI
- daily rollover happens at NY midnight
- restart reconciliation correctly attaches brackets to existing positions

Then switch to live with a small deposit.
