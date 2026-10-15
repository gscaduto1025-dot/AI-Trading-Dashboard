# Market scanner (liquid-stock universe)

The market scanner runs the **existing** day-trading and swing-trading engines over a
configurable universe of liquid US stocks, server-side, on a schedule. It is separate
from the watchlist scanner (Opportunity scanner tab), which keeps its own 9–30 tickers.

## What it scans

| Step | Source (Alpaca, read-only) | Rule |
| --- | --- | --- |
| Candidates | `/v2/assets` | Active, tradable US equities on NYSE, NASDAQ, NYSE Arca, Cboe BZX or NYSE American; plain tickers (`BRK.B` style allowed). ETFs/ETNs are excluded by a name heuristic (Alpaca has no ETF flag) unless `UNIVERSE_INCLUDE_ETFS=true`. |
| Pre-screen | `/v2/stocks/snapshots` (IEX), 200 per request | Previous session's close ≥ `UNIVERSE_MIN_PRICE`; the top `UNIVERSE_SIZE × UNIVERSE_PRESCREEN_FACTOR` by IEX dollar volume. IEX volume is only a fraction of the market, so it orders candidates but does not decide membership. |
| Ranking | `/v2/stocks/bars` 1Day, `feed=sip`, 100 symbols per request | 20-session average **consolidated** dollar volume over completed sessions; close ≥ min price and average ≥ `UNIVERSE_MIN_ADV_USD`; the top `UNIVERSE_SIZE`. |

`UNIVERSE_INCLUDE` always joins and `UNIVERSE_EXCLUDE` never does. The universe is stored
with every number used and rebuilt after `UNIVERSE_REBUILD_HOURS`, when the configuration
changes, or on request ("Rebuild universe"). Measured on 2026-10-15:

- 14,398 assets were filtered to 6,578 candidates (6,183 ETF-like names excluded);
- 400 were pre-screened, 399 passed the filters, and the top 100 were kept;
- it took 38 requests and 14.6 s.

The universe **is not the full US market**. The status endpoint and the page say which
universe was scanned and how many of its stocks were evaluated.

## Detection

No strategy is reimplemented. A second engine instance (`app.main.Service`) holds the
universe as its symbol list. It runs the frozen strategy, swing and planning code through
the same `strategy_setups()` / `swing_setups()` calls, and builds rows with the watchlist
scanner's `setup_row()` / `annotate_row()`. A universe row is therefore identical to a
watchlist row for the same setup; `tests/test_universe.py` checks this.

Setup types available are the existing engines' setups:

| Requested setup | Implementation |
| --- | --- |
| Bullish breakouts and bearish breakdowns | `swing_breakout` and `swing_consolidation_breakout` on daily/weekly candles, both directions; the opening-range breakout (`opening_range_breakout`, day). |
| Breakout and retest | `breakout_retest` (day). |
| Support/resistance reversals | `sr_rejection` (day) and `swing_sr_reversal` (swing). |
| Trend-continuation pullbacks | `trend_pullback` (day) and `swing_trend_continuation` (swing). |
| Consolidation breakouts | `swing_consolidation_breakout`. |
| Opening-range breakouts | `opening_range_breakout`. |
| Liquidity sweeps | `liquidity_sweep_reversal` (day). |
| Market-structure reversals | `swing_trend_reversal` (swing). |

No new strategy was added. Statuses are TRIGGERED, WATCHING and NO_TRADE, plus
**DATA_UNAVAILABLE** for a stock the engines could not evaluate. Such stocks are listed
with the reason and never silently dropped.

## Reliability

| Concern | How |
| --- | --- |
| Duplicate jobs | In-process lock, plus a database lease with a heartbeat: a second process cannot start a scan while one is alive. A run whose heartbeat stops for 3 minutes is marked `interrupted`. |
| Duplicate setups | Rows are keyed by the engines' deterministic setup id; one row per setup, with `first_seen` / `last_seen`. |
| Persistence | Progress is written after every batch of 8 stocks; the latest result per (stock, mode) is stored in SQLite and survives restarts. A restart loses only the batch in flight. |
| Transient errors | Stocks that hit a data error (an HTTP 429 burst, a network blip) are retried once after `UNIVERSE_RETRY_PAUSE_SECONDS`. |
| Rate limits | The universe engine shares the one Alpaca client and its 190-requests-per-minute limiter with the watchlist scanner. |
| Without the dashboard | The schedule runs in the backend process (`UNIVERSE_SCAN_INTERVAL_SECONDS`, default 900; 0 disables it). No browser is needed. |
| AI | The scanner never calls Claude, never creates alerts, never writes the signal log and never places orders. |

## Measured throughput (Alpaca free IEX feed, market hours, 2026-10-15)

| Run | Stocks | Failures | Time | Requests |
| --- | --- | --- | --- | --- |
| Cold (nothing cached) | 100 / 100 | 0 | 885 s | 2,583 |
| Warm rescan 15 minutes later | 100 / 100 | 0 | 301 s | 1,042 |

That scan found 1,087 setups: 6 TRIGGERED (all historically triggered, not fresh entries),
114 WATCHING and 967 NO_TRADE.

A warm scan costs about 10 requests per stock during the session, because every intraday
series is stale after a few minutes. At 190 requests a minute, 100 stocks take about 5
minutes; a 15-minute schedule leaves room for the watchlist scanner. `UNIVERSE_SIZE` above
about 250 cannot be refreshed within 15 minutes on the free plan (hard ceiling 500).

## Cold start (Part 5 investigation)

| Measurement (9 watchlist tickers, market hours) | Time | Requests |
| --- | --- | --- |
| Cold, sequential (baseline) | 89.5–108.8 s | 217–254 |
| Cold, parallel warm-up, 4 threads | 109 s | 280 |
| **Warm restart from the persisted cache** | **44.6 s** | **101** |
| Warm, in process | 1.4–2.4 s | 14 |

The cold start is bound by the request quota (about 200 requests per minute). The
30-minute series alone is 6,400 IEX bars served in pages of a few hundred. Parallel
threads cannot beat the quota, so the parallel warm-up is used only to keep the universe
scan at the rate limit (175 requests per minute measured, against 145 sequential).

**Implemented, outside the frozen code** (`app/rawcache.py`):

- **What it does:** the raw bars the market-data service downloaded are saved (gzip JSON) on
  shutdown, every 15 minutes, and after each universe scan. They are restored at startup
  with their ages.
- **Who decides refreshes:** the frozen service's own rules: an incremental refresh with
  overlap, and a full re-fetch once a series is 6 hours old. Series already 6 hours old
  are not restored.
- **Verified equivalent:** 15 of 15 restored-then-refreshed series were identical to a
  fresh download (completed candles).
- **Isolation:** the cache is refused for a different source, feed or adjustment, and
  never written in demo mode or tests.

**Proposal (needs approval: frozen market-data code)** for the remaining cold-start and
throughput cost:

1. **Multi-symbol bar requests.** `/v2/stocks/bars` accepts up to 100 symbols per request.
   Fetching all universe symbols per timeframe in one paginated request would cut cold
   requests by roughly 10–20x.
2. **Persistent store inside the market-data service.** Keep the raw store in SQLite with
   the same refresh rules, instead of restoring from outside.
3. **Narrower deep lookbacks.** The 4-hour/1-hour swing context drives the 6,400-bar
   30-minute series. Reducing it changes the swing engines' inputs, so it is a strategy
   change needing its own validation.

Each would change files under `backend/app/market` (and 3 under `backend/app/swing`),
which are part of the frozen rule surface. None is implemented.
