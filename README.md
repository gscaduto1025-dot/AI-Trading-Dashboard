# TradeScope AI

TradeScope AI is a stock and options **analysis** platform for day and swing traders. It:
- watches a watchlist and finds key support/resistance levels;
- detects five day-trading and five swing-trading setups with deterministic rules;
- builds complete trade plans (entry, stop, targets, reward/risk, quality score), and says
  **NO_TRADE** when the rules are not met;
- scans the whole watchlist on every new candle and raises alerts;
- has Claude explain each setup, checking every number against the data;
- evaluates option contracts for a setup;
- backtests the same rules on history;
- keeps a trade log of your actual and paper trades.

> **No autonomous or real-money trading.** TradeScope AI never places orders. It has no
> order, brokerage-execution or position endpoint, and a test asserts that no such route
> exists. Alpaca is used read-only for market data. The trade log is a manual record of
> trades you make elsewhere. Nothing here is financial advice.

> **Demo data.** Without Alpaca API keys the app runs on simulated **DEMO DATA**. It is
> labelled everywhere: a banner, the watchlist, chart watermarks, setups, alerts and
> backtests. Option prices and Greeks are never simulated; options show **OPTIONS DATA
> UNAVAILABLE** in demo mode.

## Contents

1. [Setup](#setup)
2. [Configuration (API keys and settings)](#configuration)
3. [Start commands](#start-commands)
4. [Features](#features)
5. [Tests and integration checks](#tests-and-integration-checks)
6. [Known limitations](#known-limitations)
7. [Hosting requirements](#hosting-requirements)
8. [Final checklist](#final-checklist)

Detailed references:
- [docs/ENGINES.md](docs/ENGINES.md): every engine's rules, formulas and tests;
- [docs/BACKTESTING.md](docs/BACKTESTING.md): backtest method, assumptions and limitations;
- [docs/API.md](docs/API.md): all HTTP routes;
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md): running it reliably 24/7.

## Setup

**Requirements:**
- Python 3.11 or newer (developed and tested with 3.13);
- Node.js 20.9 or newer (tested with 22);
- npm;
- optionally, an Alpaca account (market data) and an Anthropic API key (Claude explanations).

```bash
git clone <this repo> && cd AI-Trading-Dashboard
./scripts/dev.sh
```

On first run `scripts/dev.sh`:
- creates `backend/.venv` and installs `backend/requirements-dev.txt`;
- runs `npm install` in `frontend/`;
- copies `backend/.env.example` to `backend/.env`;
- starts both servers.

Open <http://localhost:3000>. The backend's interactive API docs are at
<http://localhost:8000/docs>.

To set it up by hand:

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt     # requirements.txt for production
cp .env.example .env && chmod 600 .env
cd ../frontend && npm install
```

## Configuration

All configuration lives in **`backend/.env`**. The file is git-ignored; never commit it.
Real environment variables override it, which is useful with a secrets manager. Keys are
sent only to their own provider; they are never logged, returned by the API or shown in the
browser.

**Alpaca market data** (read-only). Get keys at <https://app.alpaca.markets> under *API
Keys*; paper-account keys work.

```dotenv
ALPACA_API_KEY_ID=your-key-id
ALPACA_API_SECRET_KEY=your-secret-key
ALPACA_FEED=iex                 # iex (free, IEX only) | sip (Algo Trader Plus) | delayed_sip (free, 15-min delay)
ALPACA_OPTIONS_FEED=indicative  # indicative (free, not executable quotes) | opra (Algo Trader Plus + OPRA agreement)
```

If your plan doesn't cover the chosen feed, Alpaca answers 403 and the dashboard shows
**MARKET DATA UNAVAILABLE** or **OPTIONS DATA UNAVAILABLE** with Alpaca's reason. The app
never falls back to demo data and never shows an old price as current.

**Claude explanations:**

```dotenv
ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_MODEL=claude-opus-5-5     # default
CLAUDE_EFFORT=medium             # low | medium | high | xhigh | max
```

**Everything else:**

| Variable | Default | Purpose |
| --- | --- | --- |
| `ALPACA_ADJUSTMENT` | `split` | corporate-action adjustment for bars (`raw`, `split`, `dividend`, `all`) |
| `ALPACA_TRADING_URL` | paper API | read-only reference data only: market calendar and option contract list. Use `https://api.alpaca.markets` for live-account keys. No order endpoint is ever called. |
| `ALPACA_RATE_LIMIT_PER_MIN` / `ALPACA_MAX_RETRIES` / `ALPACA_TIMEOUT_SECONDS` | 190 / 3 / 15 | client-side rate limit (free plan allows 200/min), retries, timeout |
| `WATCHLIST` | 9 tickers | initial watchlist (first start only; edited in the app afterwards) |
| `SCAN_INTERVAL_SECONDS` | 30 | how often the server checks for newly closed candles (`0` disables the scheduler) |
| `ALERT_WEBHOOK_URL` | empty | POST alerts to Slack/Discord/ntfy/your endpoint while the dashboard is closed |
| `DATABASE_PATH` | `backend/tradescope.db` | SQLite file: alerts, signals, watchlist, AI cache, backtests, trade log |
| `CORS_ORIGINS` | `http://localhost:3000` | allowed browser origins |

Strategy, planning and options thresholds are in JSON files. Unknown keys are rejected.

| File | Environment variable | Contents |
| --- | --- | --- |
| `backend/strategies.json` | `STRATEGY_CONFIG_PATH` | day-trading strategy thresholds |
| `backend/swing_strategies.json` | `SWING_STRATEGY_CONFIG_PATH` | swing strategy thresholds |
| `backend/planning.json` | `PLANNING_CONFIG_PATH` | trade-planning and risk rules |
| `backend/options.json` | `OPTIONS_CONFIG_PATH` | option-contract filters |

Copy the matching `*.example.json` file to start.

## Start commands

| What | Command |
| --- | --- |
| Development (both servers, hot reload) | `./scripts/dev.sh` |
| Backend only | `cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000` |
| Frontend only | `cd frontend && npm run dev` (proxies `/api` to `BACKEND_URL`, default `http://127.0.0.1:8000`) |
| Production backend | `cd backend && .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1` |
| Production frontend | `cd frontend && BACKEND_URL=http://127.0.0.1:8000 npm run build && npm start` |
| All tests, lint and build | `./scripts/test.sh` |
| Full-system integration check | `./scripts/integration.sh` (boots both servers on spare ports and smoke-tests every API area and every tab) |

Run the backend with **one worker**: the scheduler and alert engine run inside the process.
See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Features

Every feature is a tab or panel of the dashboard, backed by the API.

- **Market data** (`app/market`):
  - Alpaca bars, snapshots and calendar, with pagination, retries and a rate limiter;
  - one provider series per symbol and Alpaca timeframe, downloaded once at the deepest
    lookback the app uses, refreshed incrementally and fully every 6 hours. Alpaca pages
    intraday bars in small chunks, so per-request downloads would exhaust the free plan's
    200 requests/minute;
  - regular and extended sessions, NYSE holidays and early closes;
  - 1-minute to weekly candles, with only *completed* candles used for analysis;
  - freshness checks (stale, delayed, unavailable) and integrity checks (corrupt,
    duplicate or out-of-order candles).
- **Key levels** (`app/levels`):
  - previous day and week high/low, opening range, VWAP and EMAs;
  - swing points, support/resistance zones, liquidity pools and breakouts;
  - each level carries evidence, a 0–100 strength score and a validity flag, computed
    without look-ahead.
- **Day-trading strategies** (`app/strategies`): opening-range breakout, breakout and
  retest, liquidity-sweep reversal, trend pullback, and support/resistance rejection, on
  5- and 15-minute candles. Status is TRIGGERED, WATCHING or NO_TRADE; signals never
  repaint.
- **Swing strategies** (`app/swing`): daily/weekly breakout, consolidation breakout, trend
  continuation, support/resistance reversal, and trend reversal.
- **Trade planning** (`app/planning`):
  - entry trigger and zone, and a structural stop with a volatility buffer;
  - T1/T2 at the nearest confirmed levels;
  - a minimum 2:1 reward/risk (configurable; targets are never moved to meet it);
  - a transparent quality score, which is not a win probability.
- **Opportunity scanner** (`app/scanner`): the server scans the whole watchlist in both
  modes on every new eligible candle. It ranks setups, flags stale or missing data, and
  opens any row as a full analysis.
  - **Strategy evidence label** on every setup (`app/validation`): NEGATIVE HISTORICAL
    EVIDENCE, INCONCLUSIVE, UNVALIDATED, or VALIDATED POSITIVE only after a pre-registered
    confirmatory test (none so far). It comes from the frozen 50-stock validation, and
    alerts carry it too. The setup-quality score is a technical rules score, shown apart
    from any evidence. Setups are technical analysis, not recommendations.
  - **Filters:** mode, direction, strategy or family, status, entry state (entry
    conditions met now, or triggered earlier), evidence status, volume verification,
    key-level distance (ATR), minimum R:R, setup quality and liquidity (consolidated $ ADV).
  - **Saved presets:** stored server-side, overwritten by name.
- **Research dashboard** (Research tab, `GET /api/research/validation`): the frozen
  validation per mode, strategy and timeframe, with trades, win rate, net R and its 95% CI,
  profit factor, drawdown, cost sensitivity and the pre-registered endpoints. Read-only.
  The next-generation protocol is a design only: [docs/RESEARCH_PROTOCOL_V2.md](docs/RESEARCH_PROTOCOL_V2.md).
- **Alerts** (`app/alerts`):
  - approaching support/resistance, breakout and retest confirmations, triggers,
    invalidations, swing candidates, target/stop hits, your price levels, and data-stale
    warnings;
  - deduplicated per setup and candle, with configurable cooldowns and preferences;
  - never actionable from stale or incomplete data;
  - delivered by browser notification and an optional webhook.
- **Claude explanations** (`app/ai`):
  - Claude explains only the verified fact sheet; the engine's status and levels can't be
    overridden;
  - every number is checked against the facts, and guarantees and unverifiable sentences
    are removed;
  - explanations are cached;
  - on failure the panel says "AI explanation unavailable" and the analysis stays.
  - the fact sheet includes the strategy's historical-evidence label and the scanner's live
    entry state. Claude must restate the evidence, may not claim an edge, and the validator
    enforces the label and the status;
  - sections: summary, market structure, why the setup exists, key levels, bullish and
    bearish scenarios, what confirms it, what invalidates it, risk/reward, historical
    evidence, and risks;
  - cost controls: calls only on request (auto-explain is opt-in), a cache keyed by the facts,
    per-minute, per-day and daily-spend caps, and per-call token and cost logging
    (`GET /api/ai/usage`);
  - live check: `backend/.venv/bin/python scripts/ai_live_check.py`.
- **Options** (`app/options`):
  - Alpaca option chains and contracts: bid/ask, volume, open interest, IV and Greeks;
  - calls for long setups and puts for short setups;
  - liquidity, spread, staleness and Greek checks;
  - time-decay and short-dated risk;
  - stock-level and option-level risk shown separately;
  - a "candidate for review" only with real-time OPRA quotes while the market is open.
- **Backtesting** (`app/backtest`):
  - replays history through the **same** live engines, with only completed candles
    visible and no look-ahead;
  - models spread, slippage, commission, SEC/FINRA fees and short borrow;
  - stop-first when a stop and target share a candle;
  - position-sized portfolio;
  - in-sample, out-of-sample and walk-forward evaluation;
  - results by ticker, strategy, timeframe, direction, market regime and day/swing mode;
  - quality score vs outcome, and an evidence verdict that never claims profitability
    without sufficient evidence;
  - a trade inspector with charts.
- **Trade log** (`app/journal`):
  - your **actual** and **paper** trades in separate accounts, entered manually;
  - multiple fills per trade (scaling in, partial exits), long and short;
  - fees, average-cost P&L and R multiples;
  - duplicate fills and over-closing are rejected;
  - simulated backtest trades cannot enter it (API and database constraint);
  - CSV export.

## Tests and integration checks

```bash
./scripts/test.sh           # backend pytest, frontend vitest, typecheck, lint, production build
./scripts/integration.sh    # boots backend + production frontend, API + UI smoke tests
```

Latest results in the development environment:

| Check | Result |
| --- | --- |
| Backend tests (`pytest`) | 343 passed |
| Frontend unit tests (`vitest`) | 12 passed |
| TypeScript typecheck, ESLint | clean |
| Production build (`next build`) | succeeded |
| `scripts/integration.sh` | passed 3 times consecutively. API smoke 10/10: health, market data, analysis and levels, strategies and plans, scanner, alerts, Claude (unavailable path), options (unavailable path), trade log, backtest. UI smoke 5/5: every tab, recording a paper short with a partial exit, and opening a simulated trade in the backtest inspector chart. |

How the main areas are verified (see the test files in `backend/tests`):

| Area | Evidence |
| --- | --- |
| Market data validity | in-process fake of Alpaca's documented endpoints; sessions, holidays, DST, completion, stale feeds, corrupt candles, pagination, 401/403/429/5xx |
| Calculations and key levels | EMA, ATR, swings, zones and previous-day/week levels against independent reference implementations on real AAPL/GOOG history |
| Strategy detections | positive, negative and edge cases; no look-ahead; no repainting; real-history swing signals |
| Scan coverage | every watchlist ticker in both modes; parity with the engines; stale/missing tickers flagged |
| Risk metrics | long/short plans, invalid stops, zero risk, the 2:1 rule, target never moved, quality bounds |
| Claude explanations | schema, number checks, overrides and guarantee filtering against a scripted fake client; failures keep the analysis |
| Options data | parsing without filling gaps, spreads, liquidity, staleness, missing/inconsistent Greeks, OPRA 403 |
| Alerts | status changes, duplicates across restarts, cooldowns, stale-data gate, background scheduler |
| Trade-log P&L | longs, shorts, partial exits, scaling in, fees, R, over-close and duplicate rejection, account separation |
| Backtesting | execution assumptions, fees, sizing, data integrity, signal timing (fast replay = exhaustive replay; future data cannot change past signals) |
| Full system (`test_integration.py`) | real Alpaca code path with fakes, including an outage mid-session and recovery, a lagging feed, missing SIP/OPRA subscriptions, incomplete candles, duplicate signals |

## Known limitations

**Live services:**
- **Not exercised against live services in development.** Alpaca, Claude and webhook
  delivery are tested against faithful in-process fakes and mock transports, not live
  accounts; the development sandbox could not reach Alpaca.
- **Validate with your own keys before relying on it.** Run `./scripts/integration.sh`
  with real keys in `backend/.env`.

**Data:**
- **Free feeds.** The free IEX feed covers one exchange, so its volume is a small fraction
  of the consolidated volume. The app overlays consolidated (SIP) volume on candles older
  than 15 minutes, which the free plan allows. Relative volume on newer candles is labelled
  UNVERIFIED (default) or LIMITED and is never treated as full-market volume; see
  `partial_volume_policy` in [docs/ENGINES.md](docs/ENGINES.md). Free options data is *indicative*, not executable, so the
  app never recommends a contract from it.
- **Polling, not streaming.** Prices and scans update on a schedule: quotes every 10
  seconds while the market is open (60 s when closed), scans on candle closes. There is no websocket streaming.

**Backtests:**
- Fills are modelled from candles, with fixed spread/slippage assumptions.
- When a stop and target fall in the same candle, the stop is assumed first.
- Bundled real data is daily only: AAPL 2015–2017 and GOOG 2004–2013, both survivors.
  Intraday backtests on real data need Alpaca keys.
- Runs over the current watchlist are survivorship-biased and flagged; supply a
  point-in-time universe to reduce this.
- **Current evidence.** A frozen-protocol validation on 50 unseen, point-in-time selected
  S&P 500 stocks (`backend/research/validation/`) found:
  - day: 11,701 trades, −0.63 R per trade after modelled costs (95% CI −0.66 to −0.60);
    +0.04 R before costs, and still negative at measured quote costs;
  - swing: 227 trades, −0.22 R (95% CI −0.43 to −0.01); no swing strategy has a positive CI.

  No profitability is claimed. See [docs/BACKTESTING.md](docs/BACKTESTING.md).

**Alerts and access:**
- Browser notifications work only while a dashboard tab is open; use the webhook for
  closed-tab delivery. There is no Web Push.
- **Single user, no authentication.** Put the dashboard behind your own authentication
  (reverse proxy, VPN) before exposing it.
- **No order execution by design.** Paper trades are manual records, not automatic
  simulated orders.

## Hosting requirements

- **Server:**
  - one always-on host: 1 vCPU and 1 GB RAM suffice for 9–30 tickers;
  - Python 3.11+ and Node 20.9+;
  - outbound HTTPS to `data.alpaca.markets`, `paper-api.alpaca.markets` (or
    `api.alpaca.markets`) and `api.anthropic.com`.
- **Processes:**
  - backend as a supervised service (systemd or Docker) with `--workers 1`, auto-restart,
    and `backend/.env` at `chmod 600`;
  - frontend via `npm run build && npm start`, or any Node host, with `BACKEND_URL`
    pointing to the backend.
- **Storage:** a persistent disk for the SQLite file (`DATABASE_PATH`), backed up with
  `sqlite3 tradescope.db ".backup backup.db"`.
- **Clock:** NTP enabled; candle completion and alert timing depend on it.
- **Network and access:** HTTPS reverse proxy (browser notifications require a secure
  context) and authentication in front of the app. Do not expose port 8000.
- **Monitoring:** `GET /api/health` (scanner and alert heartbeat, feed and demo flag).

Full guide: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Final checklist

Status reflects what has actually been tested. "Fakes" means in-process simulations of the
provider's documented API.

### FULLY WORKING (tested, including through the UI where applicable)

- Demo mode end to end: charts, key levels, bias, day and swing setups, trade plans and
  the setup panels, all labelled DEMO DATA (API and UI smoke tests).
- Market-data processing: sessions, holidays, DST, completed-candle-only analysis,
  freshness, integrity checks, and MARKET DATA UNAVAILABLE on failure (pytest).
- Key-level engine against independent references on real AAPL/GOOG history.
- Day-trading and swing strategy engines: rules, statuses, no look-ahead, no repainting.
- Trade-planning and risk engine.
- Opportunity scanner: candle-aware scheduling, coverage, filters, stale flags, watchlist
  editing.
- Server-side alert engine: all alert types, dedupe, cooldowns, preferences, stale-data
  gate, price levels, alerts panel.
- Claude **failure** handling: the analysis stays and "AI explanation unavailable" is
  shown, verified with no key and with scripted failures.
- Options **unavailable** path in demo mode and on 403 from Alpaca.
- Backtesting:
  - swing backtests on the bundled real daily history, and day/swing/both modes on demo
    data;
  - fees, sizing, IS/OOS, walk-forward, regimes, quality analysis and verdict;
  - trade inspector chart, CSV export, filters.
- Trade log (actual and paper): fills, partial exits, shorts, fees, R, separation from
  backtests (API, database constraint, UI).
- `scripts/test.sh` and `scripts/integration.sh`.

### PARTIALLY WORKING (implemented and tested against fakes or mocks only, or with a stated gap)

- **Live Alpaca market data and option chains.** Every code path is tested end to end
  against fakes of the documented API (`test_integration.py`), but not against a live
  Alpaca account.
- **Claude explanations with a real key.** The request, schema and validation are tested
  with a scripted fake client; no real Claude call was made in development.
- **Webhook alert delivery.** Tested with a mock HTTP transport, not a real
  Slack/Discord/ntfy endpoint.
- **Browser notifications.** The code is in place, but the permission prompt and display
  are not covered by automated tests. They work only while a tab is open.
- **Options recommendations.** The "candidate for review" logic is tested with fake OPRA
  data. With the free indicative feed no contract is ever recommended, by design.
- **Day-trading backtests on real intraday history.** The engine is tested on demo and fake
  data; real runs need Alpaca keys.
- **Unrealized P&L in the trade log.** Shown only with fresh real quotes for watchlist
  tickers. It is deliberately hidden in demo mode.
- **Production hosting.** Documented (systemd, reverse proxy, backups, monitoring) but not
  deployed or exercised on a real server.

### NOT IMPLEMENTED

- Order execution of any kind, automatic paper trading, and brokerage position sync. These
  are intentionally excluded.
- Authentication and multi-user accounts.
- Web Push (notifications while no tab is open; use the webhook instead).
- Real-time websocket streaming (the app polls).
- Option strategies beyond single long calls/puts (spreads, writing options) and options
  backtesting.
- A bundled point-in-time index-constituent dataset; you must supply your own universe to
  remove survivorship bias.
- Docker images and CI configuration.
