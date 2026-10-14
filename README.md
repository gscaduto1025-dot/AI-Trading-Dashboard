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
- **Strategy engine** (`tests/test_strategies.py`): positive, negative and edge cases for each
  strategy, the gates, candle-by-candle no-repainting checks, future-candle checks,
  determinism, independence and the API.
- **Key-level engine** (`tests/test_key_levels.py`): real AAPL/GOOG fixtures against
  independent reference implementations, no-look-ahead checks, splits, and hand-computed
  intraday fixtures (see the key-level section below).
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

### Day-trading strategy engine (`backend/app/strategies`)

The engine evaluates SPY, QQQ, NVDA, TSLA, AAPL, AMD and MSFT on **5-minute and
15-minute** candles, using **1-hour and daily** candles for higher-timeframe context. Each
of the five strategies runs independently for each symbol × timeframe × direction and
returns a status:

| Status | Meaning |
| --- | --- |
| **TRIGGERED** | Every strategy-specific condition was met on a *completed* candle, the candle was inside the entry window and not abnormal, and reward/risk to T1 is at least `min_rr`. |
| **WATCHING** | The pattern is developing while the session is open (for example a breakout waiting for its retest). |
| **NO_TRADE** | Requirements not met, with the reasons listed. |

Each setup returns:
- the ticker, strategy, direction, timeframe and higher-timeframe bias;
- its status, entry, stop, T1 and T2, and reward/risk;
- the supporting key levels;
- every confirmation condition (pass, fail or n/a, with the candle it was checked on);
- the invalidation rules and the evidence events.

`GET /api/strategies/setups[?symbol=&as_of=]`, `GET /api/strategies/config` (all
thresholds plus each strategy's rule text), `GET /api/strategies/signals` (the immutable log
of every accepted trigger; each new one also raises an alert).

**Rules** (all thresholds in `app/strategies/config.py`; override them with
`backend/strategies.json`, see `strategies.example.json`). ATR is the strategy timeframe's
14-candle ATR at the candle *before* the one being judged.

| Strategy | Trigger (on a completed candle) | Stop / targets |
| --- | --- | --- |
| **Breakout & retest** | A close beyond a confirmed zone, or PDH/PDL/PWH/PWL ± 0.1 ATR, by 0.1 ATR (previous close not beyond) with relative volume ≥ 1.5 when volume exists. Within 12 candles, a wick back to within 0.15 ATR of the broken edge without closing back through. Within 3 candles, a close beyond the retest candle's high (long) or low (short). | Beyond the retest extreme and the zone, + 0.1 ATR. A close back through the zone invalidates. |
| **Opening range breakout** | A 15- or 30-minute range (every candle present, width 0.1–1.2 daily ATR). The first close beyond it by 0.05 ATR with relative volume ≥ 1.2 (vs the same time of day), before 11:30 ET. | Range midpoint (or the opposite side) − 0.1 ATR. Targets: key levels or 1×/2× range measured moves. A retest is reported for information. |
| **Liquidity sweep reversal** | A previously confirmed high or low is traded through by 0.05–1.0 ATR. Candidates: PDH/PDL, PWH/PWL, valid swings, liquidity areas, or intraday swings once confirmed and at least 6 candles old. Within 3 candles, a close back inside on a reversal candle (bearish, close in the lower half; mirrored for longs). | Sweep extreme + 0.1 ATR. |
| **Trend pullback** | The higher-timeframe bias matches the direction (daily and 1-hour close vs EMA20 vs EMA50; if they disagree it's neutral). EMA9 vs EMA20 and close vs VWAP are aligned. At least 2 consecutive lower highs into a touch of VWAP, EMA20 or a support zone (within 0.1 ATR, without closing more than 0.25 ATR through it). Then a bullish close above the prior candle's high. | Pullback low − 0.1 ATR. An indicator alone never triggers. |
| **S/R rejection** | Price enters a zone (strength ≥ 35, or PDH/PWH, PDL/PWL) from outside without closing through it, on a pin bar (wick ≥ 2× body and ≥ 50% of the range, close in the outer 40%) or an engulfing candle. Within 2 candles, a close beyond the rejection candle's low (short) or high (long). | Beyond the rejection extreme and the zone. A close through the zone invalidates. |

**Targets** (all strategies): T1 is the first key level (or strategy-specific candidate) at
least 1R away. T2 is the next level at least 0.5R beyond T1. When no level qualifies, the
measured fallbacks are 1.5R and 3R. Each target's basis is returned.

**Gates.** No strategy triggers when any of these apply:
- Data is stale, unavailable or fails validation.
- 20-day average dollar volume is below $20M, or more than 20% of the session's candle
  slots have no trades.
- The session opened more than 3 daily ATR away from the prior close.
- The trigger candle is wider than 4 ATR, or follows 3 or more empty slots (a possible
  halt).
- The trigger candle closes in the first 5 minutes, or within 30 minutes of the close
  (early-close days included).

**No look-ahead, no repainting.**
- Only candles closed by `as_of` exist in the context, and every series (ATR, EMA, VWAP,
  time-of-day relative volume, 1-hour bias) is causal.
- Key levels come from prior sessions as of 09:30. Intraday levels are derived as the scan
  moves forward.
- Strategies scan the session candle by candle, so a trigger is fixed once emitted. Later
  candles only add an informational `outcome` (open / target 1 / target 2 / stopped).
- Tests replay a session one candle at a time and assert triggers only accumulate, appear
  exactly when their confirmation candle closes, and never change. Tests also check that
  future candles change nothing and that results are identical across runs.
- Removing the completed-candle cutoff makes those tests fail.

On the dashboard, the **Day-Trading Setups** panel lists TRIGGERED and WATCHING setups
(tick "no-trade" to see the rest). Clicking one loads its symbol and timeframe, draws its
entry, stop, targets, supporting levels and evidence on the chart, and opens the **Setup
Detail** panel with every condition.

### Key-level engine (`backend/app/levels`)

`GET /api/levels/{symbol}?mode=day|swing&timeframe=5Min[&as_of=ISO]` returns every level with
its ticker, type, price or range (`low`/`high`), timeframe(s), `calculated_at`, `formed_at`
(when it first became knowable), evidence (a summary, the rule's inputs, and the candles that
support it), a strength score with its components, `valid`, and a status.

**Inputs and point-in-time rule.** Each mode uses these timeframes:

| Mode | Timeframes used |
| --- | --- |
| Day | 5m, 15m, 1H, daily, weekly |
| Swing | 1H, 4H, daily, weekly |

All candles are regular-session and split-adjusted (`ALPACA_ADJUSTMENT=split` by default).
Before any calculation, every candle that had not closed by `as_of` is dropped. Anything
that needs later candles (swings, breakouts) records when it was confirmed and is never
reported before then. Passing `as_of` replays any past moment for backtests. Tests check
that adding future candles changes nothing in the output.

**Common quantities.**
- **ATR**: Wilder's average true range over 14 candles of the level's timeframe.
- **EMA(p)**: α = 2/(p+1), seeded with the SMA of the first p closes. It's flagged
  unreliable with fewer than 3p candles.
- **Current session**: today while today's 04:00–20:00 ET extended session is still running,
  otherwise the next trading day. The **previous session** is the trading day before it.

| # | Level | Rule |
| --- | --- | --- |
| 1-2 | **PDH / PDL** | High/low of the previous session's regular hours (09:30–close) from the finest intraday candles that cover the whole session. Falls back to the daily candle. |
| 3-4 | **PWH / PWL** | High/low of the daily candles in the last complete ISO week before the current session's week (holiday-shortened weeks included). |
| 5 | **Session open** | Open of the current session's 09:30 candle, once that candle has closed (day mode). |
| 6-7 | **ORH / ORL** | High/low of the 09:30–10:00 candles. Reported only after 10:00 and only if every candle is present. |
| 8-11 | **Support / resistance zones** | See the zone algorithm below. |
| 12-13 | **Swing high / low** | Fractal over k candles each side (k = 3, or 2 for 4H and weekly): `high[i] > max(high[i-k..i-1])` and `high[i] >= max(high[i+1..i+k])` (mirror for lows). Confirmed when candle i+k closes. The two most recent per timeframe are reported, and become invalid once a later candle trades beyond them. |
| 14 | **Session VWAP** | Σ(typical price × volume) / Σ volume over the current session's candles, where typical price = (H+L+C)/3, anchored 09:30. Valid while the session is open. |
| 15-18 | **EMA 9/20/50/200** | On 5m and daily (day mode) or daily (swing mode). The chart draws them on whatever timeframe is shown. |
| 19 | **Liquidity areas** | At least 2 confirmed swing highs within 0.1 ATR ("equal highs") that no later candle has exceeded give a *potential* buy-side area of [top, top + 0.25 ATR]; equal lows mirror this. A sweep within the last 10 candles is reported as swept, or "swept and rejected" if that candle closed back inside. This is not observed order flow. |
| 20 | **Breakouts / breakdowns** | On the trigger timeframe (5m day, daily swing), candle i closes beyond the level by more than 0.1 × ATR (taken at candle i−1) after closing on the near side, and candle i+1 also closes beyond it. Confirmation time is candle i+1's close. A zone's bounds come only from pivots confirmed before candle i. A later close back through the far side marks it failed. |

**Zone algorithm.** Zones are computed on 1H/daily/weekly (day mode) or 4H/daily/weekly
(swing mode), over the last 500 / 500 / 260 candles (4H: 400).
1. Find the confirmed swing highs and lows.
2. Sort them by price and cluster greedily: a pivot joins a cluster if it's within
   0.5 ATR of the cluster's top and the cluster stays no wider than 0.75 ATR.
3. A cluster with at least 2 reactions becomes a zone, spanning its lowest to highest pivot
   and widened symmetrically to at least 0.25 ATR. Zones are ranges, not single prices.
4. The zone is **support** if it's below the last price, **resistance** if above, or
   **contested** if price is inside it. If it last reacted in the other role, it's "flipped".
5. If closes have crossed through it 3 or more times since its last reaction, it's
   **invalidated** (no longer respected).
6. Combining timeframes: working from weekly down, a lower-timeframe zone whose midpoint
   lies inside a higher-timeframe zone is attached to it as extra evidence and doesn't
   change its bounds. Otherwise it stands alone.

**Strength score (0–100).** A transparent heuristic, **not** a probability of reversal:

```
score = R + C + T + F
R  reactions   = 7 × min(reactions, 5)                                   (0–35)
C  recency     = 25 × 0.5^(days since last reaction / 30)                (0–25)
T  timeframe   = 5m 4 · 15m 6 · 30m 8 · 1H 10 · 4H 13 · daily 16 · weekly 20
F  confluence  = 5 × min(# other level types overlapping within 0.15 daily ATR, 4)   (0–20)
```

For zones, `reactions` is the number of pivots in the zone. For other levels, it's the number
of confirmed pivots (any timeframe in the plan) within the level ± 0.15 daily ATR. Labels:
<35 weak, 35–59 moderate, ≥60 strong. Every level returns its components and inputs.

**Splits.** History is requested split-adjusted. As a safety net, an overnight gap within
5% of a common split ratio (2:1 … 50:1, or a reverse split) with at least a 45% move and
share volume moving in the same proportion is treated as an unadjusted split. Only candles
from that date on are then used, and a warning is returned. `ALPACA_ADJUSTMENT=raw` also
returns a warning.

**Chart styles.**

| Level family | Drawn as |
| --- | --- |
| Support / resistance zones | Green / red shaded bands; daily and weekly zones are more opaque. Shows the nearest 3 per side with a strength of at least 35. |
| Liquidity | Purple dashed band |
| PDH / PDL | Cyan dotted line |
| PWH / PWL | Orange dashed line |
| Session open, ORH / ORL | Grey lines |
| VWAP | Amber dashed line |
| EMA 9 / 20 / 50 / 200 | Blue / purple / teal / white lines |
| Swing points | Grey dots |
| Breakouts / breakdowns | Green / red arrows plus a solid line (✕ when failed) |

The **Key Technical Levels** panel lists everything, with filters, evidence and the score
breakdown. The setup engine also uses valid key levels for its breakout, pullback and target
logic.

**Tests** (`tests/test_key_levels.py`) run on real AAPL (2015–17) and GOOG (2004–13) daily
candles, comparing PDH/PDL/PWH/PWL, EMA, ATR, swing points and zones with separately written
reference implementations. They also check that adding future candles changes nothing, that
swings and breakouts aren't confirmed early, split detection, and hand-computed intraday
fixtures for the opening range, VWAP and breakout timing.

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
| GET | `/api/strategies/setups[?symbol=SPY&as_of=ISO]` | day-trading strategy setups (TRIGGERED / WATCHING / NO_TRADE) |
| GET | `/api/strategies/config` · `/api/strategies/signals` | strategy thresholds and rules · immutable trigger log |
| GET | `/api/levels/{symbol}?mode=day&timeframe=5Min[&as_of=2026-03-05T12:00]` | automatic key levels with evidence, strength, validity (point-in-time replay with `as_of`) |
| GET | `/api/history/{symbol}?timeframe=1Day&start=2024-01-01&end=2024-12-31[&format=csv]` | full paginated history for backtesting (naive times = New York) |
| GET | `/api/analysis/{symbol}?mode=day&timeframe=5Min` | bias, levels, setup, checklist, overlays |
| POST | `/api/ai/{symbol}?mode=day&timeframe=5Min[&refresh=true]` | Claude explanation |
| POST | `/api/scan` | scan the watchlist now |
| GET | `/api/setups/recent` | recently detected setups |
| GET | `/api/alerts` / POST `/api/alerts/{id}/ack` | alerts |
