# Pre-launch audit, Phase 1: code, architecture and stability

Date: 2026-10-15 (US market open; see "Market-data condition"). Branch
`claude/exciting-gauss-kwqte7`. Audited at `6410f4f`; fixes in `3747d77`. Nothing was
deployed. No order execution was enabled, no keys were printed, and no strategy rules
changed (`research/freeze.py check`: rules and rule surface match the frozen protocol).

Status meanings:
- **PASS:** a test was actually run in this audit and passed.
- **FAIL:** a test was run and failed.
- **FIXED:** a problem was found, fixed, and the fix was tested.
- **OPEN:** a known problem that is not fixed.
- **NOT TESTED:** no test was run in this audit.

## Market-data condition during the audit

From **12:56 ET** onwards Alpaca returned no new trades or bars on any feed (IEX and
SIP) for any stock checked: SPY, QQQ, NVDA, AAPL, TSLA, MSFT, F. This was checked
directly against Alpaca's API until 14:02 ET, while Alpaca's own clock reported the
market open. The cause (a market halt or an Alpaca outage) is unknown.

Because of this, everything that needs fresh intraday candles answered with its designed
"stale data / market data unavailable" response. Those features are marked NOT TESTED
below, not PASS.

## 1. Architecture map

| Layer | Contents |
| --- | --- |
| Frontend (Next.js 16, one page `/`) | **Tabs:** Chart & analysis, Opportunity scanner, Market scanner, Options, Backtesting, Trade log, Performance, Research. **Panels:** watchlist, chart, trade plan, market bias, key levels, setup analysis (Claude or rules-based), day and swing setups, recent setups, alerts. |
| Frontend → backend | The browser calls `/api/*` on the dashboard, which proxies to FastAPI on 127.0.0.1 (`next.config.ts`). |
| Backend (FastAPI) | 64 `/api` routes in 22 groups: ai(3), alerts(9), analysis, backtests(7), bars, explain, health, history, journal(8), levels, market, options(3), performance, planning, plans, research, scan, scanner(5), setups(4), strategies(3), swing(2), universe(5), watchlist(3). |
| Services | Market data (Alpaca read-only client, rate limiter, calendar, raw-bar cache), analysis and levels, day strategies, swing strategies, planning, opportunity scanner, universe scanner, alert engine, options analysis, backtester, trade journal and performance, rules explanations, Claude analyst with cost budget, validation registry. |
| Database (SQLite, `backend/tradescope.db`) | 18 tables: `watchlist`, `setups`, `strategy_signals`, `last_status`, `alert_events`, `alert_levels`, `alert_state`, `scanner_presets`, `saved_setups`, `journal_trades`, `journal_fills`, `backtest_runs`, `ai_cache`, `ai_usage`, `universe_builds`, `universe_runs`, `universe_results`, `universe_setup_seen`. |
| Background jobs | Watchlist scanner loop (every `SCAN_INTERVAL_SECONDS`; it rescans on each new candle and runs the alert engine). Universe scanner loop (every 15 min, with a DB lease). Manual universe runs (thread). Backtest runs (background, one at a time). Raw-cache save every 15 min and on shutdown. |

## 2. Results

### Builds, test suites and environment

| Component | Status | Evidence |
| --- | --- | --- |
| Backend test suite (pytest) | **PASS** | 430/430 before fixes; **437/437** after fixes (7 new regression tests), 6 min 53 s |
| Frontend type check (`tsc`) | **PASS** | 0 errors |
| Frontend lint (`eslint`) | **PASS** | 0 problems |
| Frontend unit tests (`vitest`) | **PASS** | 17/17 |
| Frontend production build | **PASS** | compiled with no errors or warnings |
| Full-system integration script, real Alpaca data | **FAIL** (cause: data) | API 12/13, UI 7/8. Both failures were HTTP 503 `STALE_DATA` ("latest 5-minute candle is 61 min old during an open session"). Rerun with live data. |
| Frontend ↔ backend contract | **PASS** | All 45 URL patterns the frontend calls match a backend route. 8 routes are API-only or download links: CSV exports, history, configs, signals, universe members. |
| Clean environment, fresh clone, `scripts/start.sh` | **PASS** | No `.venv`, `node_modules` or `.env`. Installs, builds and serves. Health OK on real data (keys from the environment). |
| Clean environment without API keys | **PASS** | Starts in labelled demo mode. Options report "OPTIONS DATA UNAVAILABLE". |
| Production dependencies: known vulnerabilities | **PASS** | `npm audit --omit=dev`: 0. `pip-audit` on `requirements.txt`: 0. |
| Development dependencies: known vulnerabilities | **FAIL / OPEN** | 7 (1 critical, 5 high, 1 moderate). See problem P3. |
| Missing dependencies | **FIXED** | `tzdata` was missing (needed by `zoneinfo` on Windows). Every third-party import in `backend/app` is in `requirements.txt`. |
| Start and stop scripts | **FIXED** | Stopping `start.sh` left the dashboard server running; `dev.sh` exposed the dev server to the LAN. Retested: both scripts stop both servers, and dev binds 127.0.0.1. |

### Features, tested through the real interface

Run by a headless browser against the production build, opened through the computer's
network address (the way a phone would open it): **12/12 steps passed**, after the fixes.

| Feature | Status | Evidence / note |
| --- | --- | --- |
| Dashboard opens on real data (no demo labels) | **PASS** | "ALPACA IEX" badge, real prices |
| SPY / QQQ / NVDA charts | **PASS** | candles rendered for each |
| Prices and timestamps | **PASS** | values and "last trade 12:56 PM ET · stale" shown correctly |
| Day-trading key levels, market bias, trade plans | **NOT TESTED** | held back with "MARKET DATA UNAVAILABLE" because of the stall (the designed behaviour was observed) |
| Swing key levels, market bias, conditional trade plan | **PASS** | NVDA: levels (e.g. 243.34 previous week high), bias, entry trigger 236.54, entry zone |
| Opportunity scanner (day and swing) | **PASS** | table rows, filters and evidence labels load |
| Market scanner page | **PASS** | page, progress and coverage note load |
| Market scanner results | **NOT TESTED** | every stock was DATA_UNAVAILABLE during the stall |
| Trade log (create, view, delete) | **PASS** | paper trade recorded and deleted in the UI; partial-exit P&L verified by the smoke test (net 13.00, 0.65R) |
| Performance dashboard | **PASS** | loads |
| Backtesting (run, results, trade inspector chart) | **PASS** | bundled 2005–2017 history; 15 trades, "INSUFFICIENT EVIDENCE" |
| Options tab and data status | **PASS** | indicative-feed label and limitations shown |
| Research tab | **PASS** | UI smoke test |
| Claude analysis, live | **NOT TESTED** | no `ANTHROPIC_API_KEY` (by your choice) |
| Claude panel fallback | **PASS** | "Claude AI is not activated … deterministic engines" |
| Alerts (API) | **PASS** | 30 alerts produced in the integration run |
| Alerts panel interactions (acknowledge, settings) | **NOT TESTED** | |
| Watchlist scheduler (auto rescan per candle) | **PASS** | separate live test earlier today: 5 consecutive scheduled scans on 5-minute closes |
| Persistence across restart (raw cache, scan results) | **NOT TESTED** | not retested in this audit |

### Security

| Check | Status | Evidence |
| --- | --- | --- |
| No brokerage order execution | **PASS** | Code review: the Alpaca client only issues GET requests, to market data plus calendar, assets and option contracts. No order or position routes. |
| Secrets not exposed | **PASS** | Not returned by any route, not logged, not stored. `.gitignore` covers `.env`, databases and cache. No key-shaped strings in 31 commits of git history. |
| SQL injection | **PASS** | All values are bound; dynamic column names come from fixed allowlists |
| Path traversal, SSRF, XSS, open redirects | **PASS** | No user-built file paths. Webhook URL is env-only. No raw HTML rendering. |
| Cross-site requests (CSRF) | **FIXED** | Before: a cross-site `POST /api/ai/SPY` returned 200. After: 403 through the proxy and directly against the backend (live test). |
| DNS rebinding (foreign Host header) | **FIXED** | Live: 403 through the proxy and direct; LAN-IP and localhost access still 200 |
| One bad timestamp breaks the trade log | **FIXED** | Before: a millisecond fill time caused 500 on create (and was still saved), then 500 on journal, CSV and performance. After: 400 with an explanation; listing unaffected. |
| Non-finite numbers in the journal | **FIXED** | `quantity: "inf"` was accepted; now 400 |
| CSV formula injection (trade-log export) | **FIXED** | `=HYPERLINK(…)` is exported as `'=HYPERLINK(…)` |
| Malformed backtest requests return 500 | **NOT REPRODUCED** | 3 malformed bodies returned 400 |

## 3. Remaining problems (not fixed)

| # | Severity | Problem | Recommendation |
| --- | --- | --- | --- |
| P1 | High before any non-home use | **No login.** On your Wi-Fi (`--lan`) anyone can read and edit the trade log, spend Claude budget (once a key is set) and trigger scans. The new guard blocks other websites, not people on the network. | Add authentication before any LAN-sharing outside a trusted home network, VPN access or hosting (Phase 2/3). |
| P2 | Medium | Intraday features could not be verified today because of the Alpaca feed stall. | Rerun `scripts/integration.sh` and the UI test during a normal market session. |
| P3 | Medium (dev machine only) | 7 vulnerable development packages. **Vitest 4.0.18** (critical: arbitrary file read only while the Vitest UI server runs, which this project does not use) needs 4.1.12, but npm 10 fails with an internal resolver error (`edgesOut`) when upgrading. The **`eslint-config-next` → `braces`** chain is a ReDoS in lint tooling; npm's only offered fix is a downgrade to Next 14's config. **`eslint@9.39.5`** is deprecated. None ship in the app. | Upgrade Vitest with a newer npm, or regenerate the lockfile; track `eslint-config-next` updates. |
| P4 | Low (inactive) | Claude cost caps can be exceeded: concurrent requests for different symbols all pass the budget check before any usage is recorded. Failed but billed calls (max tokens, refusals) and unknown model names are not costed. | Fix before activating Claude: reserve budget atomically, cost failed calls, and price unknown models at the top rate. |
| P5 | Low | `/api/history` caches every distinct date range (up to 512 entries × 50k bars). Browsers can no longer reach it cross-site; programs on your own computer still can. | Cap ranges per timeframe. This needs a change in the frozen market-data code, so it requires your approval. |
| P6 | Low | The database and candle cache use default file permissions (readable by other accounts on the same computer). | Restrict permissions on shared computers. |
| P7 | Low | A per-request lock dictionary in the Claude path grows without limit (slow memory growth once Claude is active). | Evict entries after use. |
| P8 | Info | Windows instructions (and `tzdata` on Windows) are untested; there is no Windows machine here. | Test on Windows if you use it. |
| P9 | Info | API docs at `/docs` list every route; this only matters if the API is ever exposed. | Disable in any hosted deployment. |

## 4. Fixes made in this phase (`3747d77`)

All fixes are covered by `backend/tests/test_audit_fixes.py` or by the live checks above.

1. `backend/app/guard.py`: refuses cross-site browser requests and foreign Host headers (proxied or direct). `ALLOWED_HOSTS` adds names such as a VPN hostname.
2. Journal: fill times must be between 1990 and now, in seconds. Numbers must be finite. At most 500 fills per request.
3. Journal CSV export defuses formula-like text.
4. `scripts/dev.sh` binds to 127.0.0.1 and makes `.env` private. Both scripts stop their own servers on exit. `start.sh` uses `npm ci`, and its `--lan` warning is accurate.
5. `tzdata` added to `backend/requirements.txt`.
