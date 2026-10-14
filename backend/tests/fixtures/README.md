# Historical candle fixtures

Real daily OHLCV data used to test the key-level engine against independently computed
reference values. These files are test inputs only; the app never displays them.

| File | Symbol | Range | Source | Adjustment |
| --- | --- | --- | --- | --- |
| `aapl_daily_2015_2017.csv` | AAPL | 2015-02-17 → 2017-02-16 (506 sessions) | plotly/datasets `finance-charts-apple.csv` (Yahoo Finance export) | OHLC as traded (no split in range; the 7:1 split was 2014-06-09 and the 4:1 split 2020-08-31). `adj_close` is Yahoo's dividend-adjusted close. |
| `goog_daily_2004_2013.csv` | GOOG | 2004-08-19 → 2013-03-01 (2,148 sessions) | `backtesting` 0.6.6 package, `backtesting/test/GOOG.csv` | As traded, before the 2014 Class C split. |

Spot checks against the public record:
- AAPL's highest 2015 close was $133.00 on 2015-02-23 and its 2015 intraday high was $134.54
  on 2015-04-28.
- GOOG's IPO-day candle on 2004-08-19: high $104.06, low $95.96.

Intraday rules (opening range, session VWAP, PDH/PDL from regular-hours candles, intraday
breakouts) are tested with small hand-built fixtures defined in the test files. Their
expected values are worked out by hand in comments there. They are not real market data.
