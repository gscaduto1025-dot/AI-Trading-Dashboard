# Backtesting and strategy validation

The **Backtesting** tab replays history through the same strategy and planning engines that
generate live signals, simulates hypothetical trades, and reports what happened. It is a
research tool. It never places orders, and nothing in TradeScope trades real money
automatically.

## How signals are produced (no separate backtest rules)

`backend/app/backtest/replay.py` calls the live functions directly:
- day: `app.strategies.engine.evaluate_symbol`;
- swing: `app.swing.engine.evaluate_swing_symbol`.

Both use the live planning rules and the live lookback windows (`Service.STRATEGY_FRAMES` /
`SWING_FRAMES`, asserted equal in tests).

1. **History is loaded once** per symbol and timeframe, then validated:
   - sorted, with duplicates removed;
   - impossible candles (high < low, non-positive prices) dropped;
   - missing daily sessions counted against the NYSE calendar.

   Every issue is listed in the report.
2. **Only completed candles are visible.** At replay time `T` the engine receives the
   candles whose *end* (`bar_end`: the regular-session close for daily bars, the last
   session of the week for weekly bars) is ≤ `T`.
   - A historical daily candle for the current session contains prices from later that day.
     It is therefore never shown to an intraday evaluation.
   - The engines also discard incomplete candles themselves.
3. **Two-pass replay** (default):
   - **Discovery:** evaluate at every session close (day) or every 20 sessions (swing,
     whose live scan covers the last 60 candles) to list the triggers the engines accepted.
   - **Confirmation:** re-evaluate at each trigger's confirmation time (the trigger
     candle's close + the live 5-second completion grace).
   - A signal counts only if the engine reports that setup as **TRIGGERED at that moment**.
     The plan, stop, targets and quality score are the ones the dashboard would have shown.
   - Accepted triggers the live engine did *not* show as TRIGGERED are listed and excluded,
     because they never produced a live signal. Example: a re-trigger later in the session,
     after the first trigger of that setup had already resolved.
   - `exhaustive: true` evaluates the engine at every candle close instead (about 10× slower).
     Tests assert both modes produce identical signals for day and swing.
4. **All strategies always run.** Strategy, ticker and timeframe filters are applied to the
   results, never to the engine, because cross-strategy conflicts change live statuses.

## Hypothetical execution (`backend/app/backtest/execution.py`)

| Assumption | Default (day / swing) | Notes |
| --- | --- | --- |
| Entry | market order at the open of the first candle after the signal candle closes | `entry_delay_bars` adds whole candles of delay; the seconds between close and order are covered by slippage |
| No-chase rule | skip if that open is outside the plan's entry zone | the live plan would not be taken there either |
| Gap checks | skip if the open is already through the stop, or at/through T1 | reported as skipped signals |
| Half spread | 2 bps / 2 bps per market order | fixed assumption, not historical quotes |
| Slippage | 3 bps / 5 bps on market and stop orders | adverse |
| Commission | $0.005 per share per side | Alpaca charges $0; this is deliberately conservative |
| Regulatory fees | SEC fee 0.00278 % of the sale notional + FINRA TAF $0.000166/share sold | charged on the sale: a long's exit, a short's entry. Rates change; the TAF per-trade cap is ignored (conservative) |
| Short borrow | 1 %/year of the entry price, per calendar day held (minimum one day) | easy-to-borrow assumption; hard-to-borrow names cost far more |
| Stop | stop-market at the stop, or at the open if a candle gaps through it, minus spread and slippage | a stop is not a guaranteed price |
| Target | limit fill at the target only when price trades **through** it by one tick | a gap through the target fills at the target, not the better open |
| **Stop and target in one candle** | **stop assumed first** | intrabar order is unknown; applies to the entry candle too |
| Day trade exit | flat at the session's last candle close | no overnight holding; a signal on the last candle is not traded |
| Swing time exit | after 20 daily candles, at the close | a simulation assumption, not a strategy rule |
| Exit policy | all at T1 (`t1`); also `t2`, or `split` (half at T1, half at T2, both using the original stop) | |
| Overlap | one open position per ticker × strategy × timeframe × direction | different setups may overlap |
| Data end / delisting | exit at the last available close | open trades are never dropped |

R = net profit per share ÷ risk at the fill (|fill − stop|). Gross R uses the same
denominator, so gross − net = cost.

### Position sizing (portfolio simulation)

Trades are also replayed as one portfolio, in time order:
- **Size:** shares = floor(equity × 1 % ÷ risk per share).
  - Capped at 25 % of equity in notional per position.
  - Capped by buying power (1× equity by default: no margin).
  - At most 10 open positions.
- **Equity for sizing** is *realized* equity (closed trades only).
- **Skipped:** a signal that cannot get at least one share, or arrives with 10 positions
  open, is skipped and counted.
- **P&L** = shares × net per-share result (all costs included).
- **Reported:** total return ($ and %) and maximum drawdown ($ and %).
- **Configurable:** `starting_capital`, `risk_per_trade_pct`, `max_position_pct`,
  `max_leverage`, `max_open_positions`.

R totals and dollar results can disagree. Example: on the bundled real history, 14 trades
sum to +1.46 R but the sized portfolio lost 0.63 %, because the position cap shrank two of
the largest winners. Both views are shown.

### Market regime

Each trade is labelled from daily candles complete before its signal, using SPY when the
data source has it and otherwise the ticker itself. The run states which.
- **trend:** close above or below its 200-day average;
- **volatility:** 20-day return volatility above or below its median over the previous year;
- **unknown:** with fewer than 200 days of history.

### Day + swing in one run

`mode: "both"` replays the day and swing engines separately over the same period (limited
to the day-mode span). The results are combined and broken down by mode.

## No parameter fitting on test data

The backtester never tunes strategy parameters. The engines run with the configuration in
effect. The only data-driven choice is the walk-forward minimum-quality filter, and it is
picked from earlier trades only (tested).

## Metrics and validation (`backend/app/backtest/metrics.py`)

**Totals:**
- total trades, win rate, average winner and loser (R and %);
- average planned R:R and the realized win/loss ratio;
- profit factor;
- expectancy per trade with a 95% confidence interval;
- maximum drawdown in R (additive) and in % (fixed-fractional equity, 1% risk per trade
  applied in exit order);
- exit-reason breakdown and average cost per trade.

**Breakdowns:** by ticker, strategy, timeframe, direction, **mode (day/swing)** and
**market regime**, each with the same metrics and a sample-size note (n < 30: not
statistically meaningful; n < 100: indicative only).

**In-sample / out-of-sample:** the first 70% of the period by signal time is in-sample,
the rest out-of-sample. Filters never move the split date.

**Walk-forward (expanding window, 4 folds):**
- the period is cut into 5 equal segments;
- for each fold, the minimum-quality filter (0 / 50 / 60 / 70) with the best expectancy on
  *earlier* trades is chosen, then applied unchanged to the next segment;
- only those out-of-sample segments are aggregated;
- the strategy rules themselves are never re-tuned, and test outcomes cannot influence the
  filter chosen (tested).

**Quality score vs outcome:** Spearman rank correlation between the quality score and
realized R, its t-statistic, and expectancy per score bucket. Fewer than 30 trades is
reported as "too few to judge".

**Evidence verdict.** It never says "profitable" without evidence. **POSITIVE EVIDENCE
(hypothetical)** requires all of:
- at least 100 trades and at least 30 out-of-sample trades;
- a 95% confidence interval of expectancy that lies entirely above zero;
- positive out-of-sample *and* walk-forward expectancy;
- an out-of-sample profit factor above 1;
- no survivorship-biased universe;
- real (non-demo) data.

Otherwise the verdict is one of:
- **INSUFFICIENT EVIDENCE** (too few trades);
- **NO EDGE FOUND** (expectancy ≤ 0);
- **NOT DEMONSTRATED** (positive but failing a check);
- **NOT EVIDENCE** (demo data).

## Inspecting simulated trades

Click any row of the trade-level report to open the inspector. It shows:
- the candles around the trade, re-fetched from the run's data source;
- the fill, stop, target and exit price lines, with entry and exit markers;
- open versus fill, costs and fees, R (gross and net), MAE/MFE, shares and dollar P&L, the
  market regime, and the quality score.

Backtest trades are **simulated** and live only inside backtest runs. They never appear in
the Trade log, which holds your actual and paper trades, and cannot be added to it: the API
and a database constraint both refuse.

## Data sources

- **Market data:** Alpaca (or the demo source in demo mode, where every result is
  labelled DEMO and the verdict is NOT EVIDENCE). Day and swing modes are supported.
  Intraday history needs Alpaca keys; the IEX feed covers one exchange only.
- **Bundled real daily history** (swing only), from `backend/tests/fixtures`:
  - AAPL 2015-02-17 → 2017-02-16;
  - GOOG 2004-08-19 → 2013-03-01.

## Survivorship bias

- A universe made of today's watchlist, or of famous survivors such as AAPL and GOOG,
  excludes companies that were delisted or fell out of favour, which inflates results. Such
  runs are flagged, and the verdict cannot be positive.
- To reduce it, supply a **point-in-time universe**: one `SYMBOL,from,to` per line,
  including delisted names (Alpaca has history for many of them). Signals outside a
  symbol's membership window are skipped and counted.
- Symbols without data are reported as data errors, not silently dropped.

## Limitations

These are also shown with every result:
1. Results are hypothetical. Real fills depend on queue position, liquidity, latency and
   partial fills.
2. Candles hide intrabar order. The stop-first rule is conservative but can understate
   winners. Day trades are resolved on 5-minute candles.
3. Spread, slippage and commission are fixed assumptions. Fast or illiquid markets cost
   more.
4. Trades are simulated independently: no margin, buying-power or portfolio limits. The
   equity curve applies fixed-fractional risk in exit order.
5. The rules are today's rules, so in-sample results are optimistic. Judge the
   out-of-sample and walk-forward sections.
6. Survivorship bias, as above.
7. Data errors, gaps and corporate-action adjustments are only as good as the provider.
   Integrity issues found are listed per series.
8. Regimes change. Few trades, or results concentrated in one ticker or period, are weak
   evidence.
9. Position sizing uses realized equity and ignores intraday margin rules, pattern-day-
   trader limits and partial fills on large orders.
10. The engines' strategy and planning configuration in effect when you run the backtest
   is used. Changing `strategies.json` / `planning.json` changes the results.

## API

| Method | Path | |
| --- | --- | --- |
| GET | `/api/backtests/config` | default assumptions, sources, limitations |
| POST | `/api/backtests` | start a run: `{"mode":"day"\|"swing"\|"both","data_source":"market"\|"bundled","symbols","start","end","settings"?,"universe"?}` (one run at a time; 409 while busy) |
| GET | `/api/backtests` | runs with summaries and live progress |
| GET | `/api/backtests/{id}?strategy=&ticker=&timeframe=&direction=&mode=&regime=&start=&end=` | run metadata + metrics and portfolio recomputed for the filters + trade list |
| GET | `/api/backtests/{id}/trades/{trade_id}/chart` | candles around one simulated trade |
| GET | `/api/backtests/{id}/trades.csv` | trade-level report |
| DELETE | `/api/backtests/{id}` | delete a stored run |
