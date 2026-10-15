# Pre-launch audit — Phase 2: market-data accuracy, scanning reliability, signal validation

**Date:** 2026-10-15.
**Branch:** `claude/exciting-gauss-kwqte7`.
**Code audited:** from `0d3f271` (Phase 1). **Fixes in this phase:** `5711d60`, `b35d21d`, `2526e31`, `f32ab29`, `fddd784`.

**Data:** Alpaca market data (IEX real-time feed; SIP consolidated history where the free plan allows it), read-only.

**Safety and scope:**
- No orders were placed, and no brokerage execution exists or was enabled.
- Nothing was deployed.
- No frozen strategy rule changed. `research/freeze.py check` still matches `rules_hash 12a8e177…` and `rule_surface 680c82f6…` after every fix.
- No data from the sealed period (on or after 2026-05-01) was used to evaluate strategies. Every replay date is earlier.
  - The live tests necessarily use today's data. That is normal scanner operation, not strategy evaluation.

**Status meanings:**
- **PASS:** a test was actually run and passed.
- **FAIL:** a test was run and failed.
- **FIXED:** a problem was found, fixed, and retested.
- **NOT TESTED:** no run in this audit.
- **Kind of evidence:** *Live* means real Alpaca data in real time. *Replay* means real historical Alpaca data replayed. *Simulated* means fake feeds or hand-built candles.

> Technically valid signals are not evidence of profit. The strategies' historical validation
> status is unchanged: all day strategies and `swing_breakout` have **negative** historical
> evidence after costs; three swing strategies are inconclusive; `opening_range_breakout` and
> `swing_trend_reversal` are unvalidated (see `app/validation/status.json`).

## 1. Market-data investigation (Phase 1 stall)

| Check | Status | Evidence |
| --- | --- | --- |
| Root cause identified | **PASS** | Alpaca's status page: "vendor-side network disruption affecting third-party data feeds (IEX and SIP)" on 2026-10-15; restored about 14:18 EDT. There was no US market-wide halt. The circuit-breaker halt that day was South Korea's Kospi. |
| Not caused by the application, the subscription or market conditions | **PASS** | Alpaca's own API had no SPY/NVDA/AAPL 1-minute bars from **12:56 to 14:11 ET (75 min)** on both IEX and SIP. Alpaca **crypto** had 72 bars and no gaps in the same window, so Alpaca was up and only its US-equities feed failed. SPY moved only 769.58 → 769.91 across the gap, and volume showed no reopening surge. The market was open throughout (Alpaca clock `is_open: true`). |
| Gap backfilled by Alpaca | **FAIL (provider)** | As of 15:52 ET the 75-minute hole was still present in both IEX and SIP history (`results/alpaca_outage_gap.txt`). Any backtest that includes 2026-10-15 inherits it. |
| Timestamps vs exchange hours (live, 9 symbols) | **PASS (live)** | 15:02 ET, every watchlist stock: 53 five-minute candles from 09:30 to 15:00, none outside 09:30–16:00, 0 misaligned, strictly increasing. The only gap is 12:55 → 14:10. The forming 15:00 candle is flagged `latest_bar_complete=false` (`results/live_timestamps.txt`). |
| Fresh data after recovery (live, 9 symbols) | **PASS (live)** | 14 scans from 14:56 to 16:00 ET: **9/9 stocks fresh in every scan**. The newest closed candle each time was the one that had just closed (e.g. the 15:00:04 scan used the 14:55 candle). |
| Stale data never labelled real-time | **FIXED** (`5711d60`) | During the stall the header showed a green **"Real-time"** badge next to "Stale", and explanations said "IEX (real-time)". Now the badge is replaced by **"Stale data"**, and the source reads "IEX (real-time feed; data STALE: <reason>)". Test: `test_stale_data_is_never_labelled_with_the_feeds_nominal_latency`. |
| Stale data never produces signals (live, during the outage) | **PASS (live)** | Phase 1 servers running during the stall: **0 actionable rows and 0 actionable alerts**. Analysis and key levels returned HTTP 503 `STALE_DATA`; the universe marked every stock DATA_UNAVAILABLE. |

## 2. Live scanner verification (2026-10-15, 14:52–16:44 ET)

**Setup:** backend on real data, `SCAN_INTERVAL_SECONDS=30`, a 20-stock universe scan every 5 minutes, scratch database.

**Logging:** 185 polls of `/api/scanner`, `/api/alerts` and `/api/universe/status`. One poll failed: the first, made before the server was up.

| Check | Status | Evidence |
| --- | --- | --- |
| Rescans on each 5-minute candle close | **PASS (live)** | 13 scheduled scans, 15:00:04 → 16:00:03, one per close. Candle count in the signature 66 → 77, then `closed`. Start delay after the close: min 2 s, median 17 s, max 29 s. Duration: min 11.7 s, median 19.2 s, max 30.7 s. 0 scan errors. |
| No rescan without a new candle | **PASS (live)** | Polled every 30 s; scans ran only when the signature changed, with exactly one scan per candle. After 16:00:03, `due=false` until the end of logging. |
| Market close handling | **FIXED** (`2526e31`) | The 16:00:03 scan ran inside the 5-second candle-completion grace. Its newest day candle was 15:50, the signature was already `closed`, and no later scan ran, so **the 15:55 candle was never scanned** and stop/target outcomes in it would wait for the next morning. The signature now counts a candle only once it is final. Test `test_the_sessions_last_candle_is_scanned_after_the_close` fails on the old code and passes now. |
| After-hours handling (universe scanner) | **FIXED** (`f32ab29`) | The "one complete scan after the close" rule only applied in the `closed` phase. In `after-hours` the 20-stock scan kept running every ~5.5 minutes: **runs 17–21 (16:22 → 16:43)** were redundant, and runs would have continued until 20:00. Test `test_after_close_rule_covers_after_hours_and_pre_market` fails on the old code and passes now. |
| Market open handling | **NOT TESTED (live)** | The server started at 14:52. Covered by the scheduling test (`test_rescans_only_when_new_eligible_candles_exist`) and the 09:10 replay and scanner run in §3. |
| No duplicate signals | **PASS (live)** | Database after the run: `setups` 41 rows and 0 duplicate (symbol, mode, timeframe, bar_time, type, direction). `strategy_signals` 9 rows, 0 duplicates. `alert_events` 55 rows, 0 duplicate dedupe keys, 0 alerts repeating (type, setup, candle). 14 scan snapshots with 0 duplicate setup ids (`results/live_duplicates.txt`). |
| No duplicate actionable alerts or webhook (Discord) posts | **PASS (simulated)** / **NOT TESTED (live)** | No webhook URL was configured in the live run (0 deliveries). Simulated: one actionable signal across 20 rescans, an engine restart, a second engine on the same database (another process) and lost engine state gave **1 stored alert and 1 webhook POST** (`test_repeated_scans_restarts_and_parallel_engines_send_one_webhook`). The webhook fires only after a successful UNIQUE insert. |
| Discord message size | **FIXED** (`b35d21d`) | Webhook text was unbounded; Discord rejects `content` over 2000 characters. It is now truncated to 2000 (test). Never observed live. |
| Actionable alerts equal currently-actionable rows | **PASS (live)** | 0 = 0 in every scan. 55 alerts: approaching support 21, approaching resistance 16, setup invalidated 12, swing candidate 5, target reached 1. All non-actionable. |
| No actionable signal from missing, partial or stale candles (live) | **PASS (live)** | The 75-minute hole made 14 of 65 five-minute slots empty (21.5% > the 20% gate), so **all 180 day setups were NO_TRADE** with reason "5Min: 14 of 65 candle slots this session have no trades (thin liquidity or data gaps)". When the gate lifted at 15:20 (14/70), the two day triggers shown were from **before** the outage (AAPL 12:50, NVDA 12:35, `liquidity_sweep_reversal`). Both were HISTORICALLY_TRIGGERED: outside the 09:35–15:30 entry window, 37–40 candles old, price outside the entry zone, live reward/risk 0.43 and 0.40 below 2:1. Total actionable across all scans: **0**. |
| NO_TRADE reasons, genuine vs data-related (live, 15:18 ET) | **PASS (live)** | **Day:** 180/180 NO_TRADE, all **data-related** (the gap gate), 36 per strategy. **Swing:** every NO_TRADE **genuine**. `swing_breakout` 33: poor reward/risk to the nearest confirmed zone. `swing_consolidation_breakout` 33: no qualifying range, or a break on relative volume below 1.3. `swing_sr_reversal` 35: no accepted reversal trigger. `swing_trend_continuation` 35: no established trend or no trigger. `swing_trend_reversal` 36: one break of structure, no confirmed higher low or lower high. 6 WATCHING, 2 TRIGGERED (`results/live_no_trade_reasons_1518ET.json`). |
| Live trigger arithmetic (live) | **PASS (live)** | SPY weekly LONG (`swing_sr_reversal`, `swing_trend_continuation`): (830.62 − 778.51) / (778.51 − 754.47) = **2.17** ✓. From the live price 772.37: 3.25 ✓. Not actionable because price had fallen back below the entry zone. AAPL day SHORT: (344.08 − 341.73) / (344.649 − 344.08) = **4.13** ✓. NVDA day LONG: (218.04 − 216.60) / (216.60 − 215.906) = **2.07** ✓. |
| Live output equals replay output on identical data | **PASS (live)** | At 15:09 ET, `/api/strategies/setups` and `/api/swing/setups` were compared with `?as_of=<same evaluated_at>` on the same server, every field compared: **day 180/180 identical, swing 180/180 identical**, same ids (`results/live_vs_replay.json`). |
| A failed API request does not crash the scanner | **PASS (simulated)** + **PASS (real API)** | Simulated: HTTP 503 on every request (with retries), then a stalled feed. No exception, scan count advanced, 0 actionable rows and alerts. The background loop survived two injected crashes (`tests/test_scanner_resilience.py`). Real API: real Alpaca quota exhaustion ("request quota exhausted") while three processes shared one account. A scanner run at a historical clock time (§3) returned 0 rows and 0 actionable for the starved moment, with no exception. |
| Recovery when data returns | **PASS (simulated)** | After the simulated stall the next scheduled scan was fresh again with no restart, and rows were back to at least the pre-stall count. Live: the outage ended at 14:11 ET, before this run started, so recovery inside one live process was not observed. |
| Cache persistence on shutdown | **FIXED** (`fddd784`) | Shutdown log: `Could not save the raw bar cache: [Errno 2] … watchlist.json.tmp`. The universe loop and the shutdown handler saved the same file at once with a shared temp name, so one rename failed and the two writers could interleave. Each file now has its own lock and a unique temp name. Test `test_concurrent_cache_saves_never_fail_or_corrupt` (8 threads) fails on the old code and passes now. |

## 3. Twelve-date historical replay (real Alpaca data, all before 2026-05-01)

`docs/audit/phase2/tools/replay_audit.py` runs the **live code path**: `Service.strategy_setups(as_of=T)` and `swing_setups(as_of=T)` for the 9 watchlist stocks, 2 modes, every strategy, both directions and both timeframes. That is 180 day and 180 swing setups per date.

**Look-ahead test.** Each date is evaluated twice from one shared download:
- once with the real candles;
- once with **every candle that is not final at T** (including candles still forming at T) replaced by absurd values: high ×3.5, low ×0.2, close ×0.3, volume ×50.

The outputs must be byte-identical. Candle end times come from the exchange calendar: early closes, holidays, and weekly candles ending on the week's last session.

| Date (ET) | Condition | Day T/W/N | Swing T/W/N | Future candles poisoned | Identical | Plan errors |
| --- | --- | --- | --- | --- | --- | --- |
| 2025-03-12 10:47 | normal, morning | 2/7/171 | 0/5/175 | 90 | ✅ | 0 |
| 2025-06-18 13:22 | normal, FOMC day | 1/1/178 | 0/5/175 | 90 | ✅ | 0 |
| 2025-09-23 15:05 | normal, late session | 0/3/177 | 1/4/175 | 90 | ✅ | 0 |
| 2025-04-07 10:15 | **high volatility** (tariff sell-off) | 0/3/177 | 0/0/180 | 90 | ✅ | 0 |
| 2025-04-09 14:00 | **high volatility** (+9.5% rally) | 0/5/175 | 0/0/180 | 90 | ✅ | 0 |
| 2024-08-05 11:30 | **high volatility** (VIX spike) | 1/4/175 | 0/10/170 | 90 | ✅ | 0 |
| 2024-11-29 12:15 | **low volume**, 13:00 early close | 1/3/176 | 1/6/173 | 90 | ✅ | 0 |
| 2024-12-24 11:40 | **low volume**, Christmas Eve | 1/2/177 | 0/11/169 | 90 | ✅ | 0 |
| 2025-01-10 09:10 | pre-market (day after a market closure) | 4/0/176 | 0/6/174 | 46 | ✅ | 0 |
| 2025-07-03 16:30 | after a 13:00 early close | 2/0/178 | 0/5/175 | 0 | ✅ | 0 |
| 2025-12-26 13:05 | **low volume**, after Christmas | 3/1/176 | 0/3/177 | 90 | ✅ | 0 |
| 2026-02-17 11:00 | normal, recent | 0/1/179 | 0/2/178 | 90 | ✅ | 0 |

**Overall replay results:**
- **Look-ahead:** **12/12 dates identical. 946 not-yet-final candles poisoned in total, with 0 output differences.**
- **Plan arithmetic:** **0 errors in 4,320 setups.** Each setup was checked for:
  - stop side, target sides, and target 2 at or beyond target 1;
  - reward/risk recomputed for target 1 and target 2;
  - risk and reward per share;
  - worst-fill reward/risk no higher than the planned reward/risk;
  - summary fields equal to the plan's numbers;
  - quality score between 0 and 100;
  - every TRIGGERED setup at or above its plan's `min_rr` (2.0);
  - every NO_TRADE carrying a reason.
- **Candle timestamps:** **1,097,923 Alpaca candles in 1,296 series. 0 out of order, 0 duplicates, 0 off the 1–60-minute grid.**

**Harness error, found and corrected (not an application bug).** The first replay run used a fixed 16:00 close and a Friday 16:00 weekly end. On 2025-07-03 it therefore poisoned the week of 30 June, which was already complete: that week's last session ended at 13:00 on 07-03, and 07-04 was a holiday. It reported a false difference. A rerun with calendar-aware end times showed 0 candles ending after T and identical output, and the full 12-date run above uses the corrected harness. The intermediate run that shared the quota with two other processes starved (most dates got 0 setups) and was discarded.

**Per-strategy outcomes across the 12 dates.** Each strategy has 432 evaluations (9 stocks × 2 timeframes × 2 directions × 12 dates).

| Strategy | TRIGGERED | WATCHING | NO_TRADE |
| --- | --- | --- | --- |
| day `liquidity_sweep_reversal` | 14 | 17 | 401 |
| day `trend_pullback` | 1 | 3 | 428 |
| day `breakout_retest` | 0 | 10 | 422 |
| day `opening_range_breakout` | 0 | 0 | 432 |
| day `sr_rejection` | 0 | 0 | 432 |
| swing `swing_breakout` | 1 | 41 | 390 |
| swing `swing_trend_continuation` | 1 | 0 | 431 |
| swing `swing_consolidation_breakout` | 0 | 14 | 418 |
| swing `swing_sr_reversal` | 0 | 2 | 430 |
| swing `swing_trend_reversal` | 0 | 0 | 432 |

**Pre-market triggers are not actionable.** On 2025-01-10 at 09:10, the day before had been a market closure (Carter day of mourning). The live scanner path with its clock set to that moment showed the previous session's triggers as **HISTORICALLY_TRIGGERED**: SPY LONG and NVDA SHORT `liquidity_sweep_reversal`. They failed the entry-window, trigger-age and entry-zone checks. Actionable rows 0, actionable alerts 0. Two of the four replay triggers could not be checked: the Alpaca quota was exhausted by the concurrent replay. The same run for 2025-03-12 got no data for the same reason and is **NOT TESTED**.

## 4. Strategy test coverage (all ten strategies)

| Strategy | TRIGGERED case | WATCHING case | NO_TRADE / blocked cases | Status |
| --- | --- | --- | --- | --- |
| `opening_range_breakout` | short with measured targets | while the range forms | into PDH, opposite direction, after the cutoff, abnormally wide range, weak volume | **PASS** (11 tests; never triggered in replay) |
| `liquidity_sweep_reversal` | PDH sweep short, next-candle confirmation | — | holding breakout, too-deep penetration | **PASS** (7 tests; 14 replay triggers) |
| `trend_pullback` | with HTF alignment | — | poor reward/risk below the session high, bearish HTF, indicators alone | **PASS** (7 tests) |
| `sr_rejection` | short at resistance | waits for confirmation | no objective candle, invalidated by close | **PASS** (6 tests; never triggered in replay) |
| `breakout_retest` | confirmation close | without retest, then expiry | failed breakout, weak volume | **PASS** (10 tests) |
| `swing_breakout` | GOOG Jan-2008 daily, Apr-2008 weekly | — | AAPL Feb-2017 overhead resistance, no volume, chasing, reward/risk, stale trigger | **PASS** (10+ tests) |
| `swing_trend_reversal` | GOOG Aug-2005, hand-built sequence | — | one break of structure, no MA shift | **PASS** (6 tests) |
| `swing_consolidation_breakout` | hand-built LONG and SHORT (**new**) | **new**: range formed, close exactly at the range high, forming candle | **new**: average-volume breakout, no range, reward/risk 50, chasing, stale, missing | **PASS** (5 new tests) |
| `swing_sr_reversal` | GOOG 2006-03-20 with independent maths (**new**) | **new**: on the reversal candle, confirming close below or at the trigger, forming | **new**: reward/risk 50, chasing, stale, missing | **PASS** (4 new tests) |
| `swing_trend_continuation` | AAPL 2016-07-13 SHORT, hand-built LONG and SHORT (**new**) | no WATCHING state by design | **new**: close at or above the prior extreme, wrong-colour candle, flat market, 219 vs 220 candles, reward/risk 1.76 vs 2.02, reward/risk 50, chasing, stale, missing, forming | **PASS** (8 new tests) |

All seven gate tests (stale, invalid, liquidity, abnormal candle or gap, near the close, forming candle) apply to every strategy.

**Fault injection** (`docs/audit/phase2/tools/mutate.py`). Each fault was planted in a throwaway copy of the backend; production code was never modified (`git status app/` stayed clean). The new focused tests detected **12 of 13** faults:

| Planted fault | New tests failing (of 17) | Existing swing tests failing (of 28) |
| --- | --- | --- |
| stop buffer on the wrong side | 10 | 2 |
| reward/risk miscalculated by 10% | 10 | 1 |
| 2:1 minimum reward/risk disabled | 5 | 4 |
| reward/risk boundary loosened to 1.7 | 2 | 1 |
| look-ahead: forming and future candles kept | 9 | 15 |
| stale/missing-data problems ignored | 3 | 1 |
| S/R reversal accepts a close AT the trigger | 1 | **0** |
| trend continuation accepts a close AT the prior low | 1 | **0** |
| trend continuation LONG accepts a close AT the prior high | 1 | **0** |
| trend continuation LONG ignores candle colour | 1 | **0** |
| trend continuation SHORT ignores candle colour | 1 | **0** |
| consolidation volume threshold lowered 1.3 → 0.9 | 1 | **0** |
| one of the two consolidation volume checks disabled | 0 | 0 |

The undetected fault is **equivalent**: relative volume is checked twice, when the breakout is detected and again as a required confirmation. Disabling one check cannot change any outcome. This is defence in depth, not a test gap.

## 5. Signal reliability

| Check | Status | Evidence |
| --- | --- | --- |
| No future data or look-ahead (real data) | **PASS (replay)** | §3: 12/12 dates identical with 946 poisoned not-final candles |
| No look-ahead (fixtures) | **PASS (simulated)** | Day: `test_future_candles_do_not_change_results`, `test_no_repainting_triggers_only_accumulate`. Swing: 4 real-history cases plus walk-forward no-repaint. New: future-candle invariance for 3 strategies. Mutation: keeping future candles fails 9 new and 15 existing tests. |
| Partial or forming candles never trigger | **PASS** | Replay: forming candles poisoned, outputs identical. Tests: `test_forming_daily_candle_never_triggers` and new forming-candle cases for 3 strategies. Live: the forming 15:00 candle was flagged incomplete. |
| Consistent candle timestamps | **PASS (live + replay)** | 0 timestamp problems in 1,097,923 replay candles; live check in §1 |
| Normal, volatile and low-volume sessions | **PASS (replay)** | §3: 3 normal, 3 high-volatility and 4 low-volume or early-close dates. Every date gave identical output and 0 plan errors. On the two April 2025 volatility days the day strategies produced only WATCHING and NO_TRADE. |
| Missing data | **PASS (live + simulated)** | Live gap gate (§2); `tests/test_scanner_resilience.py`; new missing-data cases for 3 swing strategies; 219 vs 220 daily-candle boundary |
| No signals from stale data | **PASS (live + simulated)** | §1 outage behaviour; the stale-gate test for every strategy; resilience tests |
| Backtest uses the live rules | **PASS** | `tests/test_backtest_parity.py` (9 tests): the same rule functions, point-in-time volume and liquidity equal to the live service, replay decisions equal to the live engine at each moment, every backtest trade currently actionable in the live scanner. Frozen hash unchanged. Live vs replay identical, 360/360 setups (§2). |

## 6. Backtest execution assumptions

**Modelled** (from `/api/backtests/config`; `order_execution: false`):
- **Entry:** next candle open for day trades (`entry_delay_bars` 0, configurable). The entry zone is respected.
- **Costs:**
  - half-spread: day 2 bps, swing 2 bps;
  - slippage: day 3 bps, swing 5 bps;
  - commission $0.005 per share;
  - SEC fee 2.78e-5 of notional;
  - FINRA TAF $0.000166 per share;
  - short borrow 100 bps a year.
- **Fills:** the stop is assumed first when stop and target fall in the same candle. Gaps through the stop fill at the worse open (tests).
- **Holding:** swing trades hold at most 20 candles.
- **Sizing:** 1% of realized equity at risk. Capped at 25% of equity per position, 1× buying power and 10 open positions, in the dollar portfolio simulation.
- **Evaluation:** out-of-sample 30% and 4 walk-forward folds. Evidence needs at least 100 trades and at least 30 out-of-sample trades.
- **Feed delay:** the consolidated-volume embargo is applied: IEX signals with unverified volume are detected but not traded.

**Not modelled** (stated in the app's limitations list or found here):
- queue position, partial fills and latency;
- spreads measured from historical quotes; costs are fixed assumptions;
- intrabar order below 5 minutes;
- trading halts and limit-up/limit-down bands;
- market impact for large size;
- hard-to-borrow availability;
- regime change;
- survivorship bias, unless a point-in-time universe is supplied;
- provider data errors, such as today's 75-minute unfilled IEX/SIP hole.

**Documentation note (not fixed; frozen code):** limitation 5 says the equity curve runs "without margin, buying-power or portfolio-level limits". That is true of the R-multiple curve only. The separate dollar portfolio simulation does enforce the position, buying-power and open-position limits. The text lives in frozen `app/backtest`, so changing it needs approval.

## 7. Test runs (final, after all fixes)

| Suite | Result |
| --- | --- |
| Backend (pytest), full | **BACKEND_RESULT** |
| Frozen-rule check | **PASS** (`rules_hash 12a8e177…`, `rule_surface 680c82f6…`) |
| Frontend type check / lint / unit tests | **FRONTEND_RESULT** |
| Frontend production build | **BUILD_RESULT** |
| Full-system integration (`scripts/integration.sh`, real Alpaca data, after the close) | **PASS**. API 13/13 (universe 12/12, 0 failed, 136 setups) and UI 8/8. The two failures in Phase 1 were the stale-data 503s during the outage. |
| Focused swing tests (`tests/test_swing_focus.py`) | **PASS** (17/17) |

## 8. Bugs found in Phase 2

| # | Bug | Found by | Status |
| --- | --- | --- | --- |
| B1 | Stale data labelled "Real-time" (header badge, explanation source) | Live outage | **FIXED** `5711d60` |
| B2 | The session's last 5-minute candle was never scanned (scan at 16:00:03 inside the completion grace, signature already `closed`) | Live close | **FIXED** `2526e31` |
| B3 | Universe scanner rescanned every interval during after-hours (5 redundant runs 16:22–16:43, wasting quota) | Live after-hours | **FIXED** `f32ab29` |
| B4 | Concurrent cache saves shared a temp file (ENOENT at shutdown; possible interleaved writes) | Live shutdown | **FIXED** `fddd784` |
| B5 | Webhook (Discord) text unbounded; Discord rejects over 2000 characters | Review | **FIXED** `b35d21d` |

Each fix has a test that fails on the old code and passes on the new.

## 9. Remaining limitations, untested conditions and launch blockers

**Launch blockers:**
1. **No login (from Phase 1).** Anyone who can reach the dashboard can read and edit the trade log and trigger scans. This blocks any use beyond a single trusted computer or home network.
2. **No proven edge.** Historical validation is negative after costs for every day strategy and for `swing_breakout`, inconclusive for three swing strategies, and absent for two. The scanner's signals are technically valid, not profitable. The app must keep presenting them as analysis, as it does now.

**Needs your decision (frozen rules; not changed):**

3. **The gap gate is a session ratio.** After the 75-minute outage, day strategies resumed at 15:20, once 14 empty slots were 20% of 70 (the rule blocks only *above* 20%), while still calculating across the hole. A stricter rule needs your approval, for example blocking for N candles after any gap of K or more consecutive empty candles.
4. **Backtest limitation text 5 is incomplete** (§6).

**Not tested or limited:**

5. Live Discord or webhook delivery: no URL was configured. Only simulated tests cover single delivery.
6. A live CURRENTLY_ACTIONABLE day signal was never observed. The gap gate blocked today's session, and the actionable path is covered only by simulated tests and parity tests.
7. Market open behaviour was not observed live (§2).
8. Live recovery inside one running process was not observed: the outage ended before the server started. Covered by simulation.
9. `opening_range_breakout`, `sr_rejection` and `swing_trend_reversal` never triggered in 432 replay evaluations each. `swing_sr_reversal` and `swing_consolidation_breakout` never triggered in the replay either. Their trigger paths are covered by fixture tests only. `opening_range_breakout` is always rejected by the 2:1 planning minimum (known; UNVALIDATED).
10. **Quota sharing.** All processes using one Alpaca account share 200 requests a minute. With three processes running, the quota was exhausted and a scan returned 0 rows (no crash, no signals). Run a single backend per account.
11. Alpaca has not backfilled the 2026-10-15 12:56–14:11 ET hole. This date is inside the sealed period, which must not be used before your approval.
12. The stock-level `gate_reasons` field in `/api/strategies/setups` is empty while a setup-level data gate applies (cosmetic; the setups carry the reason).
13. Phase 1 open items still apply: Claude cost-cap race (inactive), development-tool vulnerabilities, `/api/history` cache size (frozen code), file permissions, Windows untested.

## 10. Reproducing

- `docs/audit/phase2/tools/replay_audit.py <out.json>`: 12-date replay; needs Alpaca keys, about 20 minutes, read-only.
- `docs/audit/phase2/tools/mutate.py [out.json]`: fault injection on temporary copies.
- `live_vs_replay.py` and `no_trade_reasons.py`: run against a backend on port 8810.
- The results used in this report are in `docs/audit/phase2/results/`.
