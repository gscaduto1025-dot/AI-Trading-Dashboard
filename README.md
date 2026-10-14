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
| `WATCHLIST` | Initial watchlist (first start only; afterwards it is edited in the app and saved in SQLite) |
| `SCAN_INTERVAL_SECONDS` | How often the server checks for newly closed candles (default 30; a scan runs only when there are new candles; `0` disables the scheduler) |

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
- **Trade-planning engine** (`tests/test_planning.py`): long/short plans, invalid stops,
  zero risk, the 2:1 rule without moving targets, no-trade conditions, quality score, config
  and the API.
- **Swing engine** (`tests/test_swing.py`): positives pinned to real AAPL/GOOG daily and
  weekly events, negatives and edges on mutated real candles and hand-built paths,
  walk-forward no-repaint, future-candle and determinism checks, sorting, separate config
  and the API (see the swing section below).
- **Key-level engine** (`tests/test_key_levels.py`): real AAPL/GOOG fixtures against
  independent reference implementations, no-look-ahead checks, splits, and hand-computed
  intraday fixtures (see the key-level section below).
- **Calendar and validation**: NYSE 2025/2026 holidays and early closes vs the published
  schedules, observance rules, session phases, DST, OHLCV and timestamp-order checks.
- **Claude explanations** (`tests/test_ai.py`, scripted fake client, no network): number
  verification, schema validation, engine authority over status and levels, guarantee
  filtering, every API failure mode, caching, setup selection and the endpoints.
- **Opportunity scanner** (`tests/test_scanner.py`): rows equal the engine output, filters,
  ranking, stale-data flags, candle-aware scheduling, no Claude calls, watchlist management.
- **Engine, demo data, API and alerts.**

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
  levels/                  key-level detection engine
  strategies/              day-trading strategy engine (5m/15m)
  swing/                   swing-trading strategy engine (daily/weekly, 4H/1H support)
  planning/                trade-planning and risk engine (shared)
  scanner/                 multi-stock opportunity scanner (candle-aware schedule)
  ai/facts.py              verified fact sheet Claude explains (all allowed numbers)
  ai/validate.py           output schema + validation against the fact sheet
  ai/claude.py             Claude request (structured JSON output, fallbacks)
  db.py                    SQLite: watchlist, detected setups, signals, alerts, AI cache
  main.py                  FastAPI routes + background scanner
frontend/src/components/
  Dashboard.tsx            layout, data flow, auto-refresh
  MarketStatusBar.tsx      session status, feed, latency, latest candle, volume
  PriceChart.tsx           TradingView Lightweight Charts
  ScannerView.tsx          opportunity scanner: filters, bullish/bearish tables, watchlist
  ...                      watchlist, trade plan, bias, levels, AI, setups, alerts panels
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

| Mode | Timeframes | Indicators | Min reward/risk (trade plans) |
| --- | --- | --- | --- |
| Day Trading | suggested 1m, 5m, 15m, 30m, 1H | EMA 9/21; on 1H or shorter, session VWAP, prior-day and opening-range levels | 2.0 : 1 (configurable) |
| Swing Trading | suggested 1H, 4H, 1D, 1W | EMA 20/50, SMA 200, 20-bar and 52-week high/low | 2.0 : 1 (configurable) |

Any timeframe can be charted in either mode. The mode only changes the analysis parameters.

### Day-trading strategy engine (`backend/app/strategies`)

The engine evaluates every watchlist ticker on **5-minute and
15-minute** candles, using **1-hour and daily** candles for higher-timeframe context. Each
of the five strategies runs independently for each symbol × timeframe × direction and
returns a status:

| Status | Meaning |
| --- | --- |
| **TRIGGERED** | Every strategy-specific condition was met on a *completed* candle, the candle was inside the entry window and not abnormal, and the trade plan passes every planning rule (see the trade-planning section). A trigger whose stop or T2 has since been reached is no longer an open plan and becomes NO_TRADE. |
| **WATCHING** | The pattern is developing while the session is open, with an objective trigger level and invalidation level already known (for example a breakout waiting for its retest), and the conditional plan passes the planning rules. |
| **NO_TRADE** | Requirements not met, with the reasons listed. A developing pattern without an objective trigger yet (an opening range still forming, price merely approaching a level) is NO_TRADE: not enough information for a plan. |

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

Stops below are the structural invalidation levels; the planner adds the volatility buffer
(0.1 × ATR for day trading) and picks the targets.

| Strategy | Trigger (on a completed candle) | Stop / targets |
| --- | --- | --- |
| **Breakout & retest** | A close beyond a confirmed zone, or PDH/PDL/PWH/PWL ± 0.1 ATR, by 0.1 ATR (previous close not beyond) with relative volume ≥ 1.5 when volume exists. Within 12 candles, a wick back to within 0.15 ATR of the broken edge without closing back through. Within 3 candles, a close beyond the retest candle's high (long) or low (short). | Beyond the retest extreme and the zone, + 0.1 ATR. A close back through the zone invalidates. |
| **Opening range breakout** | A 15- or 30-minute range (every candle present, width 0.1–1.2 daily ATR). The first close beyond it by 0.05 ATR with relative volume ≥ 1.2 (vs the same time of day), before 11:30 ET. | Range midpoint (or the opposite side) − 0.1 ATR. Targets: key levels or 1×/2× range measured moves. A retest is reported for information. |
| **Liquidity sweep reversal** | A previously confirmed high or low is traded through by 0.05–1.0 ATR. Candidates: PDH/PDL, PWH/PWL, valid swings, liquidity areas, or intraday swings once confirmed and at least 6 candles old. Within 3 candles, a close back inside on a reversal candle (bearish, close in the lower half; mirrored for longs). | Sweep extreme + 0.1 ATR. |
| **Trend pullback** | The higher-timeframe bias matches the direction (daily and 1-hour close vs EMA20 vs EMA50; if they disagree it's neutral). EMA9 vs EMA20 and close vs VWAP are aligned. At least 2 consecutive lower highs into a touch of VWAP, EMA20 or a support zone (within 0.1 ATR, without closing more than 0.25 ATR through it). Then a bullish close above the prior candle's high. | Pullback low − 0.1 ATR. An indicator alone never triggers. |
| **S/R rejection** | Price enters a zone (strength ≥ 35, or PDH/PWH, PDL/PWL) from outside without closing through it, on a pin bar (wick ≥ 2× body and ≥ 50% of the range, close in the outer 40%) or an engulfing candle. Within 2 candles, a close beyond the rejection candle's low (short) or high (long). | Beyond the rejection extreme and the zone. A close through the zone invalidates. |

**Targets** come from the trade-planning engine: T1 is the nearest confirmed key level (or a
strategy measured move) ahead of the entry, never a multiple of risk. See below.

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

### Swing-trading strategy engine (`backend/app/swing`)

A separate engine with its own configuration (`app/swing/config.py`, overridable with
`backend/swing_strategies.json` or `SWING_STRATEGY_CONFIG_PATH`; see
`swing_strategies.example.json`). The day-trading scanner and its config are unchanged.

- **Universe**: the full watchlist.
- **Primary timeframes**: daily and weekly candles. Each strategy runs on both, for each
  direction.
- **Supporting timeframes**: 4-hour and 1-hour trend (close vs EMA20 vs EMA50), shown as
  optional "supporting" conditions. They never trigger anything on their own.
- **Higher-timeframe context**: for daily candles, the weekly structure plus the weekly close
  vs EMA20/50. For weekly candles, the weekly close vs a rising or falling EMA50.

"ATR" below is the 14-candle ATR of the strategy's own timeframe at the candle before the
one being judged. Counts like "10/6" mean 10 daily candles or 6 weekly candles.

| Strategy | Trigger (on a completed daily/weekly close) | Stop · invalidation · targets |
| --- | --- | --- |
| **Daily breakout** | A support/resistance zone that existed before the candle: at least 2 confirmed swing reactions, the first one at least 10/6 candles old. The previous close was inside or below it; this candle closes beyond its far edge by 0.1 ATR. Relative volume ≥ 1.4 (vs 20 candles) when volume exists. The higher-timeframe structure must not oppose. | Stop: the zone's opposite edge ± 0.25 ATR. Invalidation: a close back through the zone. Targets: daily/weekly zones and the 1-year extreme. |
| **Trend continuation** | Established trend: EMA20 > EMA50 > EMA200 with the close beyond EMA200, plus confirmed higher highs AND higher lows (mirrored for shorts). Needs 220 daily candles; on weekly candles with fewer than 200 weeks, falls back to EMA20 vs EMA50. A pullback within 5/3 candles: the low comes within 0.3 ATR of EMA20, EMA50 or a support zone, retraces at least 1 ATR from the prior 20-candle extreme, and doesn't close more than 0.5 ATR through the level. Then a bullish candle closes above the prior candle's high. | Stop: pullback extreme ± 0.25 ATR. Invalidation: a close beyond the pullback extreme. The prior swing extreme is a target candidate. |
| **Consolidation breakout** | The longest range of 10–40 / 6–20 candles ending at the prior candle, with height ≤ 3.5 ATR and at least 2 touches of each edge (within 0.3 ATR). A close beyond an edge by 0.1 ATR, from inside the range, with relative volume ≥ 1.3. | Stop: beyond the breakout candle's opposite extreme, never farther than the range midpoint (configurable: midpoint or opposite edge) ± 0.25 ATR. Invalidation: a close back through the midpoint. Targets include 1× and 2× range-height measured moves. With a midpoint stop and a 1× measured move, R:R is structurally below 2:1. |
| **S/R reversal** | A daily or weekly zone known at the time. Price approaches from outside and trades into the zone without closing more than 0.5 ATR through it, on an objective reversal candle: hammer / shooting star (wick ≥ 1.5× body and ≥ 45% of the range, close in the outer 40%) or an engulfing candle. Then, within 2/1 candles, a close beyond the reversal candle's high (long) or low (short). | Stop: the rejection wick ± 0.25 ATR. Invalidation: a close beyond that wick. |
| **Trend reversal** | A sequence, never one candle: (1) the confirmed structure was a downtrend (lower highs AND lower lows), (2) a close above the last lower high (break of structure, within 80/40 candles), (3) a confirmed higher low above the final lower low, not undercut since, (4) a close above the post-break high (higher high), and (5) the close is above EMA20 with EMA20 higher than 5 candles earlier. EMA20 > EMA50 is a supporting condition. Mirrored for tops. | Stop: higher low ± 0.25 ATR. Invalidation: a close beyond the higher low. |

**Strategy entry rules.** The trigger candle range must be ≤ 4 ATR, and the close must be
≤ 1.5 ATR past the entry trigger (no chasing). Everything else (stop placement, stop width,
targets, reward/risk, contradictions) is decided by the trade-planning engine.

**Target candidates** are support/resistance zones (daily, plus weekly for daily setups)
and the 1-year extreme, all known at the time, plus strategy-specific candidates such as
measured moves. Minor single swing points are not used as swing targets.

**Status.**
- **TRIGGERED**: the latest accepted trigger is at most 3 daily (or 1 weekly) candles old,
  has not been invalidated, and has not hit its stop or T2.
- **WATCHING**: a developing pattern with a conditional plan. Examples: approaching an
  established zone, inside a qualifying range, a reversal candle awaiting confirmation, or a
  break of structure plus higher low awaiting the higher high. A developing plan that the
  planner would reject (poor R:R, stop too wide, contradictions) stays NO_TRADE, with the
  reason and the plan shown for reference.
- **NO_TRADE**: none of the above, or a gate applies: data unavailable/stale/invalid, fewer
  than 60 daily candles, or 20-day average dollar volume < $20M.

**Each swing setup returns**:
- ticker, direction, strategy, market structure (swing structure, EMA20/50/200 stack,
  higher-timeframe and 4H/1H context);
- relevant daily and weekly key levels (the nearest three on each side, from the key-level
  engine);
- entry trigger, potential entry zone (trigger to trigger + 0.5 ATR), stop, T1, T2,
  reward/risk and the basis for each target;
- confirmation conditions (each pass/fail/n-a with detail) and invalidation conditions;
- status, evidence and recent triggers (accepted or rejected, with the reason);
- distance from the price to the entry trigger (% and ATR; negative means already beyond);
- the complete trade plan and its **setup quality** score (see the trade-planning section).

**No look-ahead, no repainting.**
- Only candles closed by `as_of` exist; a daily candle closes at the session close (13:00 on
  early-close days), and a weekly candle closes at its last session's close.
- ATR/EMA/relative volume at candle *i* use candles 0..*i*.
- Swing points count only once confirmed (k candles later) before candle *i*.
- Zones "as of candle *i*" are rebuilt from candles before *i* with the key-level engine's
  own rules. A past trigger is therefore judged with exactly the levels that existed then.
- Tests walk forward through real AAPL history and assert triggers never change, check that
  future candles (even absurd ones) change nothing, and check that results are
  deterministic. Removing the completed-candle cutoff or the swing-confirmation delay makes
  those tests fail.

**Tests** (`tests/test_swing.py`, 28 tests). Triggered positives are pinned to real events
and re-checked from the raw CSV rows:

| Strategy | Real event (TRIGGERED under the default 2:1 plan) |
| --- | --- |
| Daily breakout | GOOG, 2008-01-31 (T1 is a confirmed daily zone) |
| Weekly breakout | GOOG, week of 2008-04-14 (T1 is the 1-year high) |
| S/R reversal | GOOG, 2006-03-20, including a check that it was only pending on the reversal candle's own close |
| Trend continuation | AAPL downtrend, 2016-07-13 |
| Trend reversal | GOOG, 2005-08-10, plus a hand-built downtrend → break → higher low → higher high path |
| Consolidation breakout | a hand-built range and breakout (measured-move T1, breakout-candle stop) |

Two famous moves are detected as patterns but correctly planned as NO_TRADE, because a
confirmed zone sits just ahead of the entry:
- AAPL's post-earnings breakout on 2017-02-01: resistance about 0.8 above the entry, R:R ≈ 0.2.
- AAPL's Brexit-day range breakdown on 2016-06-24: support just below.

Negative and edge cases:
- a forming candle;
- a breakout with ordinary volume;
- chasing and R:R rejections;
- trigger expiry;
- "one break of structure is not a reversal";
- a missing moving-average shift;
- the gates;
- sorting and configuration separation.

**Behaviour on real history** (`cd backend && .venv/bin/python -m scripts.swing_backtest`).
Every accepted trigger in the AAPL 2015–17 and GOOG 2004–13 fixtures, followed forward:

```
strategy                     tf     signals accepted T1 first stopped  open  avg R
Consolidation breakout       1Day        48        2        2       0     0   4.05
Consolidation breakout       1Week       13        0        0       0     0   0.00
Daily breakout               1Day       111        8        2       6     0   0.12
Daily breakout               1Week       12        2        1       1     0   0.75
Support/resistance reversal  1Day       124        3        1       2     0   0.23
Support/resistance reversal  1Week       14        2        1       1     0   0.86
Trend continuation           1Day       116        3        0       3     0  -1.00
Trend continuation           1Week       27        1        1       0     0   2.05
Trend reversal               1Day        24        1        0       1     0  -1.00
Trend reversal               1Week        2        0        0       0     0   0.00
```

- These are two stocks and tiny samples (24 accepted triggers in total). They ignore
  costs, slippage and gaps, and they are **not evidence of an edge**.
- With T1 at the nearest confirmed level (never moved), the 2:1 minimum rejects most raw
  pattern signals: a confirmed zone usually sits close to the breakout or pullback.

**Dashboard.** Switching to **Swing Trading** changes:
- the chart's default timeframe (1D);
- the key levels (swing plan: 1H/4H/D/W);
- the bias/setup analysis;
- the AI explanation, which now also receives that mode's active strategy setups;
- the scanner panel: the **Swing Trading Scanner** replaces the day-trading setups.

The scanner sorts by setup quality, ticker, strategy, direction, risk/reward or distance
from the entry trigger. Clicking a row loads the symbol on its daily or weekly chart and
draws the following:
- the entry zone band, trigger, stop, T1 and T2;
- the setup levels and evidence markers.

It also opens the setup's **Trade Plan** (with a **Setup details** tab). Switching back to Day Trading restores the
day-trading panel and clears the selection.

API: `GET /api/swing/setups[?symbol=&as_of=&sort=quality|ticker|strategy|direction|risk_reward|distance]`
and `GET /api/swing/config` (thresholds plus each strategy's rule text). Fresh accepted swing
triggers are written to the same immutable signal log (`/api/strategies/signals`) and raise
`swing` alerts.

### Trade-planning and risk engine (`backend/app/planning`)

Every day-trading and swing-trading setup goes through one shared, rules-based planner.
Strategies only report objective facts: the trigger level a completed candle must close
beyond, the structural level that invalidates the idea, the supporting levels and the
confirmation conditions. The planner turns those into a complete conditional plan, or
explains why there is none. It never invents an entry, never moves a target to satisfy
the reward/risk threshold, and never returns TRIGGERED without a confirmed trigger.

**Plan fields:**

| Group | Fields |
| --- | --- |
| Identity | ticker, direction, strategy, status |
| Entry | entry trigger, entry price or entry zone |
| Exits | stop-loss, target 1, target 2 |
| Risk | risk per share, reward per share (T1 and T2), reward/risk (T1, T2 and at the worst fill in the entry zone) |
| Conditions | confirmation requirements, invalidation conditions |
| Audit | every planning check (pass / fail / pending), the calculations as formulas, the quality score with its components |

Every price carries a `basis` sentence that explains it.

**Entries** are objective conditions, never arbitrary prices:
- Examples: a close beyond the retest candle (breakout & retest), the first close beyond the opening range, a reversal candle closing back above a swept level, a bullish close above the prior candle's high after a pullback, a close beyond a rejection candle, a close beyond the post-break high (structure shift).
- TRIGGERED: the entry is the confirmation candle's close.
- WATCHING: the plan is priced at the trigger level and is conditional on the close.
- Entry zone: from the trigger to 0.25 × ATR (day) or 0.5 × ATR (swing) beyond it, or to the confirmation close if that is farther. Fills beyond it count as chasing.

**Stops** are the strategy's invalidation level plus a volatility buffer (0.1 × ATR day,
0.25 × ATR swing):
- below the retest low / zone (breakout & retest);
- the opening-range midpoint or opposite side (ORB);
- beyond the sweep extreme (sweep);
- the pullback low (trend pullback, trend continuation);
- beyond the rejection wick (rejections and reversals);
- beyond the broken zone (swing breakout);
- beyond the breakout candle, capped at the range midpoint (consolidation);
- the higher low / lower high (trend reversal).

**Targets** are the nearest confirmed levels ahead of the entry. Candidates:
- day trading: zones, prior day/week high/low, swing levels and liquidity areas from the key-level engine;
- swing trading: daily/weekly zones and the 1-year extreme;
- strategy measured moves (opening-range or consolidation height).

Selection:
- T1 is the nearest candidate at least 0.05 ATR beyond the entry. A level right in front of the entry is an obstacle, so it caps the trade instead of being skipped.
- T2 is the next candidate at least 0.25 ATR beyond T1.
- Only when no candidate lies ahead (price beyond every known level) is an ATR projection used: entry ± 2 ATR (day) or 3 ATR (swing), then T1 ± 1 / 1.5 ATR for T2. The basis says so.
- Targets are chosen without looking at the stop, so they can never be tuned to reach a ratio.

**Reward/risk**: risk = |entry − stop|, reward = |target − entry|, R:R = reward ÷ risk.
The default minimum is 2:1 to T1, configurable globally and per mode. A plan below it is
NO_TRADE with the numbers shown.

**NO_TRADE conditions** (each reported as a failed check, with an explanation):
- stale, unavailable or invalid data, or failed gates (liquidity, data gaps, abnormal opening gap);
- insufficient candle history (no ATR);
- a quoted bid/ask spread wider than 0.25% (day) / 0.75% (swing) when a quote is available. Bars-only feeds report the spread as unknown rather than assuming it is fine.
- no valid technical setup / no objective trigger, or no structural invalidation level;
- invalid stop placement (the invalidation level is not on the loss side of the entry);
- zero risk (stop equals entry) or negligible risk (< 0.05 ATR), so R:R is never divided by zero;
- a stop wider than 3 ATR;
- reward/risk below the minimum;
- contradictory signals: 2 or more conflicts. Conflicts for trend-following strategies:
  - the higher-timeframe trend opposes the trade (daily, 1-hour, weekly or 4-hour, by mode);
  - an opposite-direction trigger on the same ticker and timeframe (all strategies).

  Reversal strategies trade against the prior trend by design, so an opposing trend is
  scored lower but not counted as a conflict.
- a strategy entry rule rejected the trigger candle (entry window, abnormal candle, chasing);
- a required confirmation condition failed;
- a day trigger that already reached its stop or T2, or a swing trigger that is stale, invalidated or resolved.

**Setup quality score** (0–100). This is a transparent ranking of supporting evidence. It is
**not a predicted win probability**.

| Component | Weight | Full points when |
| --- | --- | --- |
| Higher-timeframe alignment | 15 | aligned (neutral = half, opposed = 0) |
| Strength of supporting levels | 15 | key-level strength 100/100 (swing zones: 5+ reactions); unrated = half |
| Price-action confirmation | 20 | every required confirmation condition passed |
| Relative volume | 10 | ≥ 1.5× average (0.5× = 0; unavailable = half) |
| Trend structure | 15 | swing structure / EMA alignment agrees with the trade (mixed = half) |
| Available risk/reward | 25 | R:R to T1 ≥ 2 × the minimum (linear below) |
| Conflicting evidence | −10 each | (penalty) |

**Configuration**: `app/planning/config.py`, overridden by `backend/planning.json` or
`PLANNING_CONFIG_PATH` (see `planning.example.json`). Unknown keys are rejected and the
weights must sum to 100.

**API**:
- `GET /api/plans/{symbol}?mode=day|swing[&as_of=]`: every plan for a symbol, active plans first.
- `GET /api/planning/config`: rules and weights.
- Each setup in `/api/strategies/setups` and `/api/swing/setups` also carries its `plan`. The setup's status and numbers are exactly the plan's.
- The Claude explanation receives the selected plan, and its final status is always the plan's status (see the AI section).

**Dashboard**: the right sidebar is now the **Trade Plan** panel. It shows the following:
- every price with its basis;
- the per-share risk and reward, and the reward/risk ratios;
- the formulas;
- every planning check;
- the confirmation and invalidation conditions;
- the quality breakdown.

With no setup selected, it lists the current symbol's plans (best first). Clicking a setup
in either scanner opens its plan, with a **Setup details** tab for the strategy evidence. The
chart draws the plan's trigger, entry zone, entry, stop and targets.

**Tests** (`tests/test_planning.py`, 26 tests) cover the following:
- long and short plans checked by hand;
- WATCHING plans;
- invalid stop placement for both directions;
- zero and negligible risk;
- stops that are too wide;
- the "target not moved" rule (a nearer level caps the trade even when a farther one would give 4:1);
- configurable and per-mode minimums, plus the exact-threshold case;
- ATR projections only when nothing lies ahead;
- stale data and missing history;
- no setup;
- contradictions and spreads;
- strategy rejections and missing confirmation;
- quality weights and bounds;
- config validation;
- that both engines publish exactly their plan's numbers;
- the API.

Deliberately reintroducing target skipping (ignoring levels within 1 ATR) makes the
"target not moved" test fail.

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

### Market bias and legacy setup scan (`backend/app/analysis`)

- **Market bias** scores up to six factors (price vs slow EMA, fast vs slow EMA, slow EMA
  slope, RSI, price vs VWAP or SMA200, swing structure), each with an ATR dead-band so flat
  markets read as neutral. It feeds the Market Bias panel and Claude's fact sheet.
- The original single-pattern detector (ACTIONABLE / FORMING / NO TRADE) still powers the
  watchlist scan history ("Recently detected setups") and its alerts. It no longer drives
  the dashboard's trade plans, the chart's plan lines or the AI analysis; the
  trade-planning engine does.

### AI analysis with Claude (`backend/app/ai`)

Claude explains verified analysis; it never produces prices, levels, setups or statuses.

**Fact sheet** (`facts.py`). For the selected setup (or the symbol's best plan in the current
mode), the backend assembles a deterministic sheet from the market-data service and the
analysis, key-level and planning engines:
- market structure and bias factors;
- the higher-timeframe trend;
- moving-average values and alignment;
- the VWAP relationship (day trading);
- relative volume and feed coverage;
- the ten nearest key levels, with evidence and distance;
- the nearest support and resistance;
- the full trade plan: trigger, entry, zone, stop, targets, risk/reward, confirmation and
  invalidation conditions, conflicting signals and failed checks;
- the data timestamp and data source (DEMO DATA is flagged).

**Request** (`claude.py`):
- One call to `claude-opus-5-5` (configurable with `CLAUDE_MODEL` / `CLAUDE_EFFORT`).
- Structured outputs (`output_config.format` with a strict JSON schema).
- Server-side refusal fallbacks (`fallbacks: "default"`).
- A system prompt that requires every number to be copied from the sheet and forbids
  forecasts and guarantees.

**Output schema**: `symbol`, `trading_mode`, `market_bias`, `setup_name`, `setup_status`,
`technical_summary`, `key_level_explanations[{level, explanation}]`, `setup_explanation`,
`bullish_scenario`, `bearish_scenario`, `entry_conditions[]`, `invalidation_conditions[]`,
`risk_explanation`, `data_timestamp`.

**Validation** (`validate.py`), applied before anything is shown or cached:
1. Schema: types, enums, required and unexpected fields. A violation rejects the response.
2. Engine authority: `setup_status`, `market_bias`, `symbol`, `trading_mode`, `setup_name`
   and `data_timestamp` are replaced by the engine values. Disagreements are listed as
   corrections, so Claude can never override a status or a level.
3. Numbers: every number in Claude's text must match a number in the fact sheet at the
   precision Claude wrote it (prices, zones, percentages, `x:1` ratios, relative volume).
   Indicator periods, small counts, dates and times are exempt. A sentence with an
   unverified number is removed and reported.
4. Claims: sentences promising outcomes ("guaranteed", "risk-free", "can't lose", "will
   definitely", "100%", ...) are removed.
5. If nothing verifiable remains in the technical summary, setup explanation or risk
   analysis, the whole response is rejected.

**Failure handling**: API errors (auth, rate limit, 5xx, network), refusals, truncated
output, invalid JSON and failed validation all return `available: false` with "AI
explanation unavailable: <reason>". The deterministic fact sheet, Trade Plan panel and all
calculated analysis stay on screen. Failures are not cached.

**Cost control**:
- Explanations are cached in SQLite under a hash of the fact sheet, model, effort and prompt
  version. The same candles and plan never cost a second call; a new candle or plan change
  produces a new sheet.
- `GET /api/ai/{symbol}` reads only the cache.
- `POST /api/ai/{symbol}` generates (`refresh=true` forces a new call).
- Concurrent requests for the same sheet share one call.

**AI Analysis panel**: it follows the selected ticker and setup (or the symbol's best plan).
On every change it first checks the cache. When **auto** is on, it generates after a
0.9-second pause, so flicking through setups doesn't spend API calls. Turn **auto** off to
generate only on demand.

It shows:
- the engine status, setup, bias, last price and data timestamp;
- Claude's sections;
- a validation footer: numbers verified, statements removed, fields reset to engine values,
  and the model and time.

Without `ANTHROPIC_API_KEY`, the panel shows the calculated analysis and the unavailable
message.

### Opportunity scanner (`backend/app/scanner`)

The **Opportunity scanner** view (header toggle) scans every watchlist ticker for day- and
swing-trading setups. Initial watchlist: SPY, QQQ, NVDA, TSLA, AAPL, AMD, MSFT, META, AMZN.
Add or remove tickers in the scanner view (`POST /api/watchlist`, `DELETE /api/watchlist/{symbol}`):
- tickers are validated against the feed; with Alpaca, a symbol it has no data for is rejected;
- at most 30 tickers, so a full scan stays inside the rate limit;
- the list is saved in SQLite and survives restarts.

**Where the results come from.** Every row is a setup produced by the day-trading or
swing-trading strategy engine and planned by the trade-planning engine. The scanner copies
the following and recalculates nothing:
- status, entry trigger, stop, targets and R:R;
- the quality score;
- the confirmation conditions and failed checks.

Tests check every row against the engine output, field by field. Rows include:
- **triggered** and **watching** setups;
- setups that **fail requirements**: a pattern was detected, with an objective trigger, but
  the plan is NO_TRADE. The failed checks are shown.

Each row is tagged with its family: breakout, retest, pullback or reversal. Rows are ranked
by status, then quality score, then R:R. Bullish and bearish setups appear in separate
tables.

**Per-ticker summary:**
- price and change;
- day and swing market bias;
- the nearest important support and resistance, from zones and reference/swing levels of
  the key-level engine, with distance in % and ATR, and "near" within 0.5 ATR or inside the
  zone;
- counts by status;
- data freshness and the last completed candle for each mode.

**Stale or missing data.** A ticker whose candles are stale, invalid or unavailable is
flagged in red with the reason. The engines' data gates make its setups NO_TRADE, and the
scanner marks them not actionable ("Data stale: ..."). The other tickers are unaffected.

**Filters** (UI and `GET /api/scanner`):
- `mode=day|swing`;
- `side=bullish|bearish`;
- `status=TRIGGERED,WATCHING,NO_TRADE`;
- `min_rr`;
- `min_quality`;
- `strategy`: a family such as `breakout`, or a strategy id;
- `include_no_trade`.

**Automatic updates, server-side.**
- **Schedule:** a background task checks every `SCAN_INTERVAL_SECONDS` (default 30)
  whether a new eligible candle can exist: the watchlist, the number of completed 5-minute
  candles in the current session, and the last completed daily session.
- **Rescans:** a scan runs only when that changes, on startup, after a watchlist edit, or
  on **Scan now** (`POST /api/scanner/run`). No browser tab is needed.
- **No redundant work:** candles are cached per timeframe, strategy results per candle, and
  the market-data client's token bucket paces every request under the feed's rate limit.
- **No Claude calls:** the scanner never calls Claude; AI explanations are generated only in
  the AI panel and are cached.

Each snapshot records the following, and the view polls the snapshot every 15 s:
- scan id, trigger, start and finish times, duration;
- the feed;
- per-row last-updated time, data freshness and last candle.

**Click-through**: clicking a row opens that ticker in the chosen mode and timeframe on the
**Chart & analysis** view, with the setup selected. That view shows:
- the chart with the plan drawn;
- the Trade Plan and Setup details;
- key levels and bias;
- the AI analysis of that setup.

Clicking a ticker card opens its chart.

**Tests** (`tests/test_scanner.py`):
- every row equals the engine's setup and plan; reporting a different stop makes this test fail;
- every detected setup is listed once and ranked;
- requirement failures and actionability are set correctly;
- ticker summaries, and every filter;
- stale and unavailable tickers are flagged and never actionable;
- candle-aware scheduling: no rescan inside a forming 5-minute candle, rescans on a new
  candle, the close or a watchlist change;
- no Claude calls;
- watchlist add/remove/validation/persistence, and the API.

### Alerts

The scheduled scanner (and **Scan now**) records alerts:
- every new accepted strategy trigger (day and swing) goes into the immutable signal log;
- the default-timeframe analysis feeds "Recently detected setups".

Re-scans of unchanged data don't duplicate alerts. Turn on **Notify** in the Alerts panel
to get browser notifications for actionable setups.

## API

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/health` | data mode (`demo`/`alpaca`), feed, AI status, timeframes |
| GET | `/api/market/status` | session phase, holiday/early close, next open/close, calendar source |
| GET | `/api/watchlist` | quotes: price, change, volume, last-trade time, latency |
| GET | `/api/bars/{symbol}?timeframe=5Min&session=regular\|extended&limit=300` | candles + completion + data status + quality report |
| GET | `/api/strategies/setups[?symbol=SPY&as_of=ISO]` | day-trading strategy setups (TRIGGERED / WATCHING / NO_TRADE) |
| GET | `/api/strategies/config` · `/api/strategies/signals` | strategy thresholds and rules · immutable trigger log |
| GET | `/api/swing/setups[?symbol=&as_of=&sort=quality]` | swing-trading scanner (daily/weekly; sortable) |
| GET | `/api/swing/config` | swing strategy thresholds and rules |
| GET | `/api/plans/{symbol}?mode=day\|swing[&as_of=]` | complete trade plans for a symbol (active first) |
| GET | `/api/planning/config` | trade-planning rules, thresholds and quality weights |
| GET | `/api/levels/{symbol}?mode=day&timeframe=5Min[&as_of=2026-03-05T12:00]` | automatic key levels with evidence, strength, validity (point-in-time replay with `as_of`) |
| GET | `/api/history/{symbol}?timeframe=1Day&start=2024-01-01&end=2024-12-31[&format=csv]` | full paginated history for backtesting (naive times = New York) |
| GET | `/api/analysis/{symbol}?mode=day&timeframe=5Min` | bias, levels, setup, checklist, overlays |
| GET | `/api/ai/{symbol}?mode=day\|swing[&setup_id=]` | cached Claude explanation + verified fact sheet (never calls Claude) |
| POST | `/api/ai/{symbol}?mode=day\|swing[&setup_id=&refresh=true]` | generate (or return cached) validated Claude explanation |
| GET | `/api/scanner[?mode=&side=&status=&min_rr=&min_quality=&strategy=&include_no_trade=]` | latest opportunity-scanner snapshot (server-side, filtered) |
| POST | `/api/scanner/run` · `/api/scan` | scan the watchlist now |
| POST / DELETE | `/api/watchlist` (`{"symbol": "IWM"}`) · `/api/watchlist/{symbol}` | add / remove a ticker (validated, persisted) |
| GET | `/api/setups/recent` | recently detected setups |
| GET | `/api/alerts` / POST `/api/alerts/{id}/ack` | alerts |
