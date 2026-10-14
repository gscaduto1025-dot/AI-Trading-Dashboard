"""Historical data for backtests: loaded once per symbol and timeframe, validated, and
sliced so that the engines only ever see candles that were COMPLETE at the replay time.

Lookahead guard: a candle is visible at `as_of` only if its end time (the live
`bar_end`, including the regular-session close for daily bars and the last session of the
week for weekly bars) is <= as_of. A historical daily bar for the current session would
contain prices from later in the day, so it is never passed to an intraday evaluation.
"""

from __future__ import annotations

import bisect
import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from ..data.base import Bar, DataError
from ..market.calendar import NY, MarketCalendar
from ..market.timeframes import bar_end, spec

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"

# Bundled REAL daily candles (provenance: tests/fixtures/README.md). Daily only, so they
# support swing backtests; weekly candles are aggregated from them like Alpaca's.
BUNDLED = {
    "AAPL": ("aapl_daily_2015_2017.csv",
             "AAPL daily 2015-02-17 to 2017-02-16 (real; as traded, no split in range)"),
    "GOOG": ("goog_daily_2004_2013.csv",
             "GOOG daily 2004-08-19 to 2013-03-01 (real; as traded, before the 2014 split)"),
}


def _midnight(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=NY).timestamp())


def load_bundled(symbol: str) -> list[Bar]:
    name = BUNDLED[symbol][0]
    with open(FIXTURE_DIR / name) as f:
        return [Bar(_midnight(date.fromisoformat(r["date"])), float(r["open"]),
                    float(r["high"]), float(r["low"]), float(r["close"]), float(r["volume"]))
                for r in csv.DictReader(f)]


def weekly_from_daily(bars: list[Bar]) -> list[Bar]:
    """Monday-stamped weekly candles from daily candles (Alpaca's weekly convention)."""
    weeks: dict = {}
    for b in bars:
        d = datetime.fromtimestamp(b.time, NY).date()
        weeks.setdefault(d.isocalendar()[:2], []).append(b)
    out = []
    for g in weeks.values():
        d0 = datetime.fromtimestamp(g[0].time, NY).date()
        monday = date.fromordinal(d0.toordinal() - d0.weekday())
        out.append(Bar(_midnight(monday), g[0].open, max(b.high for b in g),
                       min(b.low for b in g), g[-1].close, sum(b.volume for b in g)))
    return out


@dataclass
class Integrity:
    """What was found (and dropped) while validating one series."""
    symbol: str
    timeframe: str
    bars: int = 0
    first: str | None = None
    last: str | None = None
    dropped_invalid: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    missing_sessions: int = 0
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def validate_series(symbol: str, tf: str, bars: list[Bar], cal: MarketCalendar) -> tuple[list[Bar], Integrity]:
    """Sort, de-duplicate and drop impossible candles; count missing daily sessions."""
    rep = Integrity(symbol, tf)
    out: list[Bar] = []
    seen: set[int] = set()
    prev = None
    for b in bars:
        if prev is not None and b.time < prev:
            rep.out_of_order += 1
        prev = b.time
    for b in sorted(bars, key=lambda x: x.time):
        if b.time in seen:
            rep.duplicates += 1
            continue
        ok = (min(b.open, b.high, b.low, b.close) > 0 and b.volume >= 0
              and b.high >= max(b.open, b.close) and b.low <= min(b.open, b.close))
        if not ok:
            rep.dropped_invalid += 1
            continue
        seen.add(b.time)
        out.append(b)
    if rep.duplicates:
        rep.issues.append(f"{rep.duplicates} duplicate candle(s) removed")
    if rep.out_of_order:
        rep.issues.append(f"{rep.out_of_order} candle(s) arrived out of order (sorted)")
    if rep.dropped_invalid:
        rep.issues.append(f"{rep.dropped_invalid} impossible candle(s) dropped (OHLC/volume)")
    if out and tf == "1Day":
        have = {datetime.fromtimestamp(b.time, NY).date() for b in out}
        first = datetime.fromtimestamp(out[0].time, NY).date()
        last = datetime.fromtimestamp(out[-1].time, NY).date()
        missing = [s.day for s in cal.trading_days(first, last) if s.day not in have]
        rep.missing_sessions = len(missing)
        if missing:
            rep.issues.append(f"{len(missing)} trading session(s) missing, e.g. "
                              f"{', '.join(d.isoformat() for d in missing[:3])}")
    rep.bars = len(out)
    if out:
        rep.first = datetime.fromtimestamp(out[0].time, NY).isoformat()
        rep.last = datetime.fromtimestamp(out[-1].time, NY).isoformat()
    return out, rep


class Series:
    """One validated candle series with precomputed completion times for fast slicing."""

    def __init__(self, tf: str, bars: list[Bar], cal: MarketCalendar) -> None:
        self.tf = tf
        self.bars = bars
        s = spec(tf)
        self.ends = [bar_end(b, s, cal, "regular") for b in bars]
        self.starts = [b.time for b in bars]
        # bar_end is monotonic for sorted bars, so bisect finds the visible prefix.

    def completed(self, as_of_ts: int, limit: int | None = None) -> list[Bar]:
        """Candles complete at `as_of_ts` (end <= as_of), the last `limit` of them."""
        k = bisect.bisect_right(self.ends, as_of_ts)
        return self.bars[max(0, k - limit):k] if limit else self.bars[:k]

    def after(self, ts: int) -> list[Bar]:
        """Candles that START at or after `ts` (used for execution, never for signals)."""
        k = bisect.bisect_left(self.starts, ts)
        return self.bars[k:]


class HistoryProvider:
    """Interface: bars(symbol, timeframe, start, end) -> list[Bar]."""

    name = "base"
    is_demo = False
    description = ""

    def bars(self, symbol: str, tf: str, start: datetime, end: datetime) -> list[Bar]:
        raise NotImplementedError


class MarketHistory(HistoryProvider):
    """Alpaca (or demo) history through the shared MarketDataService (validated,
    session-filtered, aggregated exactly like live candles)."""

    def __init__(self, market) -> None:
        self.market = market
        self.name = market.source.name
        self.is_demo = market.source.is_demo
        self.description = ("DEMO DATA - simulated prices, not market history" if self.is_demo
                            else f"Alpaca {market.feed.label} historical candles")

    def bars(self, symbol, tf, start, end):
        return self.market.get_history(symbol, tf, start, end, any_symbol=True).bars


class BundledHistory(HistoryProvider):
    """The real daily fixtures shipped with the repository (swing backtests only)."""

    name = "bundled"
    description = "Bundled real daily candles: " + "; ".join(v[1] for v in BUNDLED.values())

    def bars(self, symbol, tf, start, end):
        if symbol not in BUNDLED:
            raise DataError(f"No bundled history for {symbol}; bundled: {', '.join(BUNDLED)}")
        daily = load_bundled(symbol)
        if tf == "1Day":
            out = daily
        elif tf == "1Week":
            out = weekly_from_daily(daily)
        else:
            return []  # intraday is not bundled
        lo, hi = start.timestamp(), end.timestamp()
        return [b for b in out if lo <= b.time <= hi]
