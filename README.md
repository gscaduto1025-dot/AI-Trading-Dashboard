# TradeScope AI

An AI-assisted stock trading **analysis** dashboard for day and swing traders. It scans a
watchlist (SPY, QQQ, NVDA, TSLA, AAPL, AMD, MSFT, META, AMZN), finds support and resistance,
reads market bias, detects breakout / breakdown, trend-pullback and reversal setups, computes
entry, stop, targets and reward/risk, and says plainly when there is **no trade**. Claude
explains each analysis in plain language.

> **Analysis only.** TradeScope AI never places trades and has no brokerage/trading
> integration. Nothing here is financial advice.

> **Demo data.** Without Alpaca API keys, the app runs on **simulated DEMO DATA**. That's
> labelled everywhere: a banner, the watchlist, the chart watermark, the header, setups and
> alerts. Demo prices are not real and do not change in real time.


## Quick start

Requirements: Python 3.11+, Node.js 20+.

```bash
./scripts/dev.sh
```

The first run creates `backend/.venv`, installs the Python and npm dependencies and copies
`backend/.env.example` to `backend/.env`. Then open:

- Dashboard: <http://localhost:3000>
- API docs (Swagger): <http://localhost:8000/docs>

### Manual start (two terminals)

```bash
# Terminal 1: backend
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env            # optional: add API keys
.venv/bin/uvicorn app.main:app --reload --port 8000

# Terminal 2: frontend
cd frontend
npm install
npm run dev                     # http://localhost:3000
```

The frontend proxies `/api/*` to the backend (see `frontend/next.config.ts`). To point at a
backend elsewhere, set `BACKEND_URL`, e.g. `BACKEND_URL=http://10.0.0.5:8000 npm run dev`.

### Connecting Alpaca market data (API keys)

Keys go in **`backend/.env`**. That file is listed in `.gitignore` and is never committed.
`./scripts/dev.sh` creates it from `backend/.env.example` on first run. Otherwise:

```bash
cp backend/.env.example backend/.env
chmod 600 backend/.env          # readable only by you
```

Then edit these two lines in `backend/.env` and restart the backend:

```dotenv
ALPACA_API_KEY_ID=your-key-id
ALPACA_API_SECRET_KEY=your-secret-key
```

Get keys at <https://app.alpaca.markets> under **API Keys** (a paper-trading account's keys
work for market data). Alpaca's own variable names `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`
also work, and real environment variables override the file (useful for deployment secrets
managers). The keys are only ever sent to Alpaca as request headers. They're never returned
by the API, logged, or put in error messages, and `Settings` excludes them from its `repr`.
The frontend never sees them.

Choose the feed that matches your Alpaca subscription. Coverage differs by plan:

| `ALPACA_FEED` | Coverage | Latency | Plan |
| --- | --- | --- | --- |
| `iex` (default) | Investors Exchange only; volume is IEX-only | Real-time | Free |
| `sip` | All US exchanges (consolidated) | Real-time | Algo Trader Plus |
| `delayed_sip` | All US exchanges (consolidated) | 15-min delayed | Free |

With `sip` on a plan that doesn't include it, Alpaca returns 403 and the dashboard shows
**MARKET DATA UNAVAILABLE** with Alpaca's reason. Switch to `iex` or `delayed_sip`.

Other settings in `backend/.env`:

| Variable | Purpose |
| --- | --- |
| `ALPACA_ADJUSTMENT` | `split` (default), `raw`, `dividend`, `all` |
| `ALPACA_TRADING_URL` | Used only for the market calendar. Paper by default; set `https://api.alpaca.markets` for live-account keys. |
| `ALPACA_RATE_LIMIT_PER_MIN`, `ALPACA_MAX_RETRIES`, `ALPACA_TIMEOUT_SECONDS` | Client-side rate limit (default 190/min, under the free plan's 200), retries, timeout |
| `ANTHROPIC_API_KEY` | Enables the **Claude AI Analysis** panel |
| `CLAUDE_MODEL`, `CLAUDE_EFFORT` | Default `claude-opus-5-5`, `medium` |
| `WATCHLIST` | Comma-separated symbols the API will serve |
| `SCAN_INTERVAL_SECONDS` | Background scanner interval (default 300, `0` disables it) |

If Alpaca is configured but fails, the app shows **MARKET DATA UNAVAILABLE**. It never falls
back to demo data and never shows an old price as current.

## Tests and builds

```bash
./scripts/test.sh
```

This runs the backend pytest suite, then the frontend typecheck, ESLint and the production
`next build`. The backend suite covers:

- **Market data service** (`tests/test_market_data.py`): runs against an in-process fake of
  Alpaca's endpoints (`tests/fake_alpaca.py`, mirroring the documented response shapes).
  Covers auth headers, pagination, feeds, 429/5xx retries with `Retry-After`, no retries on
  401/403, the rate limiter, the regular vs extended session, 1H/4H anchoring, early closes,
  holidays, stale-feed detection, delayed-SIP cutoffs, corrupt candles, caching, pre-market
  quote math, calendar fallback, and the API's unavailable/history/CSV responses.
- **Calendar and validation**: NYSE 2025/2026 holidays and early closes vs the published
  schedules, observance rules, session phases, DST, OHLCV and timestamp-order checks.
- **Engine, demo data, API, alerts and Claude** (with a stubbed client).

## How it works

```
backend/app/
  config.py                env/.env settings (secrets hidden from repr)
  market/calendar.py       NYSE calendar: holidays, early closes, pre/after-hours, session status
  market/timeframes.py     8 timeframes, session filter, session-anchored 1H/4H, candle completion
  market/validation.py     OHLCV integrity + timestamp order/alignment checks
  market/alpaca_client.py  Alpaca REST: auth headers, token-bucket rate limit, retries, error types
  market/sources.py        AlpacaSource (bars, snapshots, calendar) and feed definitions
  market/service.py        MarketDataService: fetch windows, validation, completion,
                           freshness/latency labels, caching, quotes, history
  data/demo.py             deterministic simulated data (DEMO DATA), same pipeline
  analysis/                indicators, levels, setup engine
  ai/claude.py             Claude explanation (structured JSON output)
  db.py                    SQLite: detected setups, alerts, AI response cache
  main.py                  FastAPI routes + background scanner
frontend/src/components/
  Dashboard.tsx            layout, data flow, auto-refresh
  MarketStatusBar.tsx      session status, feed, latency, latest candle, volume
  PriceChart.tsx           TradingView Lightweight Charts
  ...                      watchlist, setup, bias, levels, AI, setups, alerts panels
```

### Market data service

- **Endpoints**: `GET /v2/stocks/bars` (paginated via `next_page_token`, `sort=asc`, up to
  10,000 per page) and `GET /v2/stocks/snapshots` on `data.alpaca.markets`, plus
  `GET /v2/calendar` on the Trading API. GET requests only; no order or account endpoints.
- **Timeframes**: 1m, 5m, 15m, 30m and 1D/1W come from Alpaca directly. 1H and 4H are built
  from 30-minute bars and anchored to the session open (09:30, 10:30, … / 09:30, 13:30),
  because clock-aligned hourly bars would mix pre-market into the first hour.
- **Sessions**: the chart shows regular hours (RTH, 09:30–16:00) or extended hours (ETH,
  04:00–20:00). Analysis always uses RTH candles. Early-close days end at 13:00 (after-hours
  17:00). Holidays come from Alpaca's calendar, with built-in NYSE rules as a fallback (Good
  Friday, Juneteenth, observance rules, special closures).
- **Timezones**: all session logic runs in `America/New_York`, so it's DST-safe. The API
  returns UTC epoch seconds plus ISO times with the NY offset.
- **Completed candles**: a candle is final once its end (cut at the session close) has
  passed the feed's data cutoff, plus a 5-second grace period. The chart draws the forming
  candle translucent. **Analysis, setups and alerts use completed candles only**, so every
  trigger is confirmed on a candle close.
- **Freshness**: each response carries `status` with feed, coverage, latency
  (`real-time`/`delayed`/`historical`/`demo`) and freshness (`fresh`/`stale`/
  `unavailable`). Data is stale when, during an open session, the latest candle lags by more
  than about 3 candles (minimum 5 minutes; 30 minutes in the thinner extended hours), or when
  a closed market's data doesn't reach the last session. Stale, invalid, insufficient or
  unavailable data **blocks analysis**: the API returns 503 with a code (`STALE_DATA`,
  `DATA_INTEGRITY`, `INSUFFICIENT_DATA`, `MARKET_DATA_UNAVAILABLE`, ...).
- **Validation**: rejects non-finite or non-positive prices, negative volume,
  high < max(open, close, low), low > min(...), misaligned or future timestamps, out-of-order
  rows and conflicting duplicates. Exact duplicates are dropped with a warning. Minutes with no
  trades on single-venue feeds are reported as information, not errors.
- **Reliability**: a token-bucket limiter (190/min) runs before every call. Retries use
  exponential backoff with jitter on timeouts, connection errors, 429 and 5xx, honouring
  `Retry-After` / `X-RateLimit-Reset`. 401/403/4xx are not retried. Responses are cached with
  timeframe-aware TTLs (5–30 s while a session is live, 5–15 min when closed), and failures
  are cached for 10 s to protect the quota.
- **Quotes**: last trade, change vs the correct reference close, and session volume. Before
  the feed has a daily bar for today, change is measured from the latest daily close and
  volume reads 0. IEX volume is labelled "IEX only".
- **Auto-refresh**: the dashboard polls every 10–20 s while any session is live and every
  2 minutes when closed. The backend cache keeps this well inside rate limits.

### Modes and timeframes

| Mode | Timeframes | Indicators | Min reward/risk |
| --- | --- | --- | --- |
| Day Trading | suggested 1m, 5m, 15m, 30m, 1H | EMA 9/21; on 1H or shorter, session VWAP, prior-day and opening-range levels | 1.5 : 1 |
| Swing Trading | suggested 1H, 4H, 1D, 1W | EMA 20/50, SMA 200, 20-bar and 52-week high/low | 2.0 : 1 |

Any timeframe can be charted in either mode. The mode only changes the analysis parameters.

### Setup engine

- **Market bias** scores up to six factors (price vs slow EMA, fast vs slow EMA, slow EMA
  slope, RSI, price vs VWAP or SMA200, swing structure), each with an ATR dead-band so flat
  markets read as neutral.
- **Detectors**: breakout/breakdown through a key level (with the bias), trend pullback into
  an EMA or level, and reversal at a level with stretched RSI plus a hammer or engulfing candle.
- **Trade plan**: the entry is the trigger price, the stop sits beyond the level or pullback
  extreme, and the targets are the next levels in the trade direction, or 2R/3R when no level
  exists.
- **Status**:
  - **ACTIONABLE**: the trigger fired on a closed bar and every required check passes.
  - **FORMING**: the pattern is present but it's waiting on the trigger or on one required
    condition.
  - **NO TRADE**: no pattern, reward/risk too low, the move is already extended past the
    entry ("don't chase"), or too many conditions are missing. The panel explains why and
    which levels to watch.
- **Claude** gets the engine's numbers and explains them. It may not invent prices. The
  backend also enforces that its verdict is never more aggressive than the engine's status.
  Responses are cached in SQLite per bar so repeat views don't cost API calls. The request
  uses server-side refusal fallbacks (`fallbacks: "default"`).

### Alerts

The scanner runs on startup and every `SCAN_INTERVAL_SECONDS`, or when you press **Scan
watchlist**. It covers each symbol in both modes on the default timeframe (5m / 1D). An
alert fires when a symbol's setup starts forming or becomes actionable. Re-scans of unchanged
data don't duplicate alerts. Turn on **Notify** in the Alerts panel to get browser
notifications for actionable setups.

## API

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/health` | data mode (`demo`/`alpaca`), feed, AI status, timeframes |
| GET | `/api/market/status` | session phase, holiday/early close, next open/close, calendar source |
| GET | `/api/watchlist` | quotes: price, change, volume, last-trade time, latency |
| GET | `/api/bars/{symbol}?timeframe=5Min&session=regular\|extended&limit=300` | candles + completion + data status + quality report |
| GET | `/api/history/{symbol}?timeframe=1Day&start=2024-01-01&end=2024-12-31[&format=csv]` | full paginated history for backtesting (naive times = New York) |
| GET | `/api/analysis/{symbol}?mode=day&timeframe=5Min` | bias, levels, setup, checklist, overlays |
| POST | `/api/ai/{symbol}?mode=day&timeframe=5Min[&refresh=true]` | Claude explanation |
| POST | `/api/scan` | scan the watchlist now |
| GET | `/api/setups/recent` | recently detected setups |
| GET | `/api/alerts` / POST `/api/alerts/{id}/ack` | alerts |
