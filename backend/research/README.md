# Research scripts

Offline research tools for the strategy audit. They change no strategy rule and place no
orders: they run the production backtest unchanged on real Alpaca market data and analyse
its trades. Nothing here is imported by the app or its tests.

| Script | What it does |
| --- | --- |
| `collect_trades.py` | Runs `run_backtest` for fixed segments (development, validation, bundled) and saves every trade and untraded signal, plus the plan the live engine reported at entry. Refuses the sealed test period (from 2026-05-01) unless `--unseal` is given. |
| `analyze.py` | Breakdowns by strategy, ticker, timeframe, direction, regime, time of day; R, MFE, MAE and holding-period distributions; loser anatomy; cost sensitivity. Wilson intervals for win rates, seeded bootstrap intervals for expectancy. |
| `verify_execution.py` | Re-simulates sampled trades with an independent implementation of the exit rules, and resolves same-candle stop/target cases with finer candles. |
| `universe.py` | Point-in-time 50-ticker selection: S&P 500 members on T0, SIP dollar-volume rank, sector cap. |
| `freeze.py` | Hashes the strategy/planning/volume settings, cost assumptions and app source; `check` refuses a run if they changed. |
| `validate_run.py` | Frozen-protocol 50-ticker validation, one symbol per resumable step. |
| `validation_report.py` | Validation report by mode, strategy, timeframe, ticker, sector, direction: CIs, PF, drawdown, cost sensitivity, coverage, sealed-period audit. |
| `forward_test.py` | Forward paper test of the 9-stock watchlist from 2026-10-16: append-only ledger, re-checks earlier trades. No orders. |
| `measure_costs.py` | Reads historical consolidated quotes (NBBO) just after sampled day entries: half-spread and cost versus the candle open. |

Typical run (from `backend/`, Alpaca keys set):

```bash
.venv/bin/python research/collect_trades.py /tmp/day-dev.json day-dev
.venv/bin/python research/analyze.py /tmp/day-dev.json > /tmp/analysis.json
.venv/bin/python research/verify_execution.py /tmp/day-dev.json > /tmp/verify.json
.venv/bin/python research/measure_costs.py /tmp/day-dev.json > /tmp/costs.json
```

Segments: `day-dev` 2024-11-01 – 2025-10-31, `day-val` 2025-11-01 – 2026-04-30, `swing-dev`
2021-07-01 – 2024-06-30, `swing-val` 2024-07-01 – 2026-04-30, `bundled` (AAPL/GOOG daily
2005–2017). The final test period 2026-05-01 – 2026-10-09 is sealed: evaluate pre-registered
hypotheses on it once, not during development.

## 50-ticker validation (frozen protocol)

`validation/protocol.json` (committed before any run) fixes the rules hash, universes, periods
and endpoints. Results: `validation/results/` (`day.jsonl.gz`, `swing.jsonl.gz`, `report.json`,
`costs-day-nbbo.json`). Rebuild the report:

```bash
.venv/bin/python research/validation_report.py research/validation/results/day.jsonl.gz \
    research/validation/results/swing.jsonl.gz > report.json
```
