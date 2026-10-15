# Research protocol v2 (design only)

**Status: design. Nothing in this document is implemented, run or tuned.** It describes how
a next-generation strategy family would be developed and tested. The current strategies
(baseline `rejected-v1`) and their frozen validation stay exactly as they are; v2 would be
added beside them, never by editing them.

## 1. Why a new family, and what it must fix

The frozen 50-stock validation (`backend/research/validation/`) found:

| Finding | Number | Consequence for v2 |
| --- | --- | --- |
| Day trades lose after costs | −0.63 R per trade, 95% CI −0.66 to −0.60 (11,701 trades) | The current day rules are rejected. |
| Small gross edge before costs | +0.04 R (CI +0.02 to +0.07) | Signals carry a little information, too little to trade. |
| Stops are very tight | Median stop 17 bps from entry | 1–2 bps of cost per side costs 0.1–0.2 R per trade. |
| Measured entry cost | Half-spread median 1.17 bps; entry cost mean 1.88 bps (286 NBBO samples) | Break-even for v1 day trades is about 0.3 bps per side: impossible. |
| Swing loses before costs | −0.16 R gross, −0.22 R net (227 trades) | Swing needs better signals, not cheaper execution. |

The design goal: **trades whose risk unit (1 R) is large relative to realistic execution
costs**, so that a modest gross edge can survive costs.

## 2. Candidate hypotheses (fixed before any data is examined)

Each candidate is a separate strategy with its own id (`v2_*`). The parameter values are
fixed here, before any data is examined. They are not chosen from results.

| Id | Idea | Fixed rules to specify before data | Max configurations |
| --- | --- | --- | --- |
| `v2_wide_stop_intraday` | Keep v1 entry signals on 15-minute and 1-hour candles, but place the stop at structure no closer than 0.5 daily ATR; target at the next key level at least 1.5 R away | Stop floor ∈ {0.5, 0.75} daily ATR | 2 |
| `v2_multi_day_hold` | Enter on intraday signals aligned with the daily trend, hold up to 5 sessions, exit at T1, stop or time stop | Time stop ∈ {3, 5} sessions; stop ≥ 1 daily ATR | 2 |
| `v2_limit_entry` | Same signals as above, but entry by a limit order at the trigger level (no market order); a fill counts only if price trades through the limit by one tick | Limit valid for {1, 3} candles | 2 |
| `v2_swing_trend_weekly` | The only positive point estimates in v1 (weekly swing trend continuation, +1.04 R on 9 trades; swing trend continuation +0.37 R on 34) restated as a pre-registered rule on weekly candles | One configuration | 1 |

At most **7 configurations** in total. No configuration may be added after development
data has been examined. If an idea is dropped, the reason is recorded, and its slot is not
reused.

## 3. Execution and cost model v2 (decided before testing)

- **Entry costs:** the measured NBBO distribution, by ticker and time of day. Each simulated
  market order pays the half-spread quoted 5 seconds after the decision. Each limit order
  pays nothing extra, but fills only when price trades through its limit by one tick.
- **Stop exits:** slippage measured from NBBO quotes at the moment the stop trades, at
  least 1 tick. A gap through the stop fills at the open, as in v1.
- **Fees:** Alpaca commission $0, plus SEC fee, FINRA TAF and short borrow as in v1.
- **Sensitivity:** every result is reported at ×0.5, ×1, ×1.5 and ×2 the measured cost. A
  candidate passes only if its point estimate is still positive at ×1.5.
- **Measurement first:** before any v2 backtest, measure exit slippage on a random 300-trade
  sample of v1 stop exits, so the cost model is not fitted to v2 outcomes.

## 4. Data, splits and leakage rules

| Role | Universe | Period | Rule |
| --- | --- | --- | --- |
| Development | S&P 500 liquidity ranks 51–100 on 2020-12-31 (point-in-time, same selection script), excluding all tickers used so far | 2021-01-04 to 2023-06-30 | All rule-writing and the choice among the ≤ 7 configurations happen here. |
| Validation | A new 50-ticker sample: ranks 101–150 on 2023-06-30 | 2023-07-01 to 2024-10-31 | One run per surviving configuration; no changes afterwards. |
| Confirmation | The sealed period (from 2026-05-01) on a fresh 50-ticker sample, plus the 9-stock forward ledger | Sealed: **only after explicit owner approval**; forward: from v2's freeze date | One-shot. |

- The v1 validation sample (50 tickers, 2023-11 to 2024-10 for day) has now been examined.
  It may not be used to choose or confirm v2.
- Swing confirmation needs more time than the sealed period offers (about 5.5 months). Swing
  candidates are therefore confirmed in forward testing only, with at least 100 trades per
  strategy.
- Each candidate is frozen with `research/freeze.py` before its validation run: its own rule
  surface (new files only), configuration and cost hash. The v1 rule surface must stay
  unchanged (enforced by `tests/test_validation_status.py`).

## 5. Statistics and decision rules

- **Primary endpoint:** net R per trade at measured costs (×1), with a seeded bootstrap
  95% CI (5,000 resamples).
- **Pass (validation):** CI lower bound > 0, point estimate > 0 at ×1.5 costs, and at least
  300 trades (day) or 100 trades (swing). Holm correction across the configurations
  validated.
- **VALIDATED POSITIVE (app label):** pass in validation **and** in the one-shot
  confirmation. Only then does `research/build_validation_status.py` list the strategy as
  VALIDATED_POSITIVE, by adding it to `CONFIRMED`.
- **Power:** with a per-trade standard deviation of about 1.5 R, detecting +0.10 R at 80% power
  needs about 1,800 trades; +0.20 R needs about 450. The validation universe is sized to
  reach these counts. If it cannot, the result is reported as inconclusive, not stretched.
- **Reported always:** trades, win rate (Wilson CI), net and gross expectancy, profit factor,
  cumulative-R drawdown, cost sensitivity, results by ticker, sector and timeframe.
  Inconclusive and negative results are reported in full.

## 6. What stays fixed

- `rejected-v1` rules, configuration and costs (frozen hash `12a8e177…`, rule surface
  `680c82f6…`).
- The 50-ticker validation results in `backend/research/validation/results/`.
- The sealed period: untouched until the owner explicitly approves its single use.
- No orders of any kind: v2 research is offline simulation only.

## 7. Steps, each needing a go-ahead

1. Measure exit (stop) slippage on v1 trades (market-data reads only).
2. Write the v2 rules exactly as specified in §2–3; get the configurations reviewed and frozen.
3. Development runs on ranks 51–100, 2021-01 to 2023-06.
4. Freeze the surviving configurations, then run validation on ranks 101–150, 2023-07 to
   2024-10.
5. Present results. Only with explicit approval: confirmatory use of the sealed period.
