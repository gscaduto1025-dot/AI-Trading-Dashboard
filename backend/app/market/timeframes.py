"""Timeframe definitions, session filtering, aggregation and candle completion.

Alpaca serves 1/5/15/30-minute, daily and weekly bars natively; minute bars align to the
clock (09:30, 09:35, ...). Clock-aligned hourly bars would start at 09:00 and mix pre-market
into the first regular-session hour, so 1-hour and 4-hour candles are built here from
30-minute bars, anchored to each day's session start:

* regular session: 1H = 09:30, 10:30, ... 15:30 (30-minute final bar); 4H = 09:30, 13:30
* extended session: anchored at 04:00 (1H = 04:00, 05:00, ...; 4H = 04:00, 08:00, ...)

A candle's end is its start plus its length, cut at the session close (so the 4H bar from
13:30 ends at 16:00, or at 13:00 on an early-close day). Daily and weekly candles end at the
regular close of their (last) trading day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..data.base import TIMEFRAMES, Bar
from .calendar import NY, MarketCalendar, SessionDay


@dataclass(frozen=True)
class TimeframeSpec:
    key: str
    label: str
    minutes: int  # nominal length; daily=390, weekly=1950
    fetch: str  # Alpaca timeframe to request
    intraday: bool

    @property
    def aggregated(self) -> bool:
        return self.fetch != self.key

    @property
    def fetch_minutes(self) -> int:
        return TIMEFRAMES[self.fetch]


SPECS: dict[str, TimeframeSpec] = {
    "1Min": TimeframeSpec("1Min", "1 minute", 1, "1Min", True),
    "5Min": TimeframeSpec("5Min", "5 minutes", 5, "5Min", True),
    "15Min": TimeframeSpec("15Min", "15 minutes", 15, "15Min", True),
    "30Min": TimeframeSpec("30Min", "30 minutes", 30, "30Min", True),
    "1Hour": TimeframeSpec("1Hour", "1 hour", 60, "30Min", True),
    "4Hour": TimeframeSpec("4Hour", "4 hours", 240, "30Min", True),
    "1Day": TimeframeSpec("1Day", "Daily", 390, "1Day", False),
    "1Week": TimeframeSpec("1Week", "Weekly", 1950, "1Week", False),
}


def spec(timeframe: str) -> TimeframeSpec:
    try:
        return SPECS[timeframe]
    except KeyError:
        raise ValueError(f"Unsupported timeframe {timeframe!r}; use one of {list(SPECS)}") from None


def ny_date(ts: int) -> date:
    return datetime.fromtimestamp(ts, NY).date()


def filter_session(bars: list[Bar], cal: MarketCalendar, session: str) -> list[Bar]:
    """Keep intraday bars that start inside the trading day's `session` window."""
    out = []
    for b in bars:
        sd = cal.day(ny_date(b.time))
        if sd is None:
            continue
        start, end = sd.bounds(session)
        if start.timestamp() <= b.time < end.timestamp():
            out.append(b)
    return out


def aggregate(bars: list[Bar], tf: TimeframeSpec, cal: MarketCalendar, session: str) -> list[Bar]:
    """Combine session-filtered fetch-timeframe bars into session-anchored `tf` candles."""
    if not tf.aggregated:
        return bars
    size = tf.minutes * 60
    buckets: dict[int, list[Bar]] = {}
    order: list[int] = []
    for b in bars:
        sd = cal.day(ny_date(b.time))
        if sd is None:
            continue
        anchor = int(sd.bounds(session)[0].timestamp())
        key = anchor + (b.time - anchor) // size * size
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(b)
    return [
        Bar(key, chunk[0].open, max(x.high for x in chunk), min(x.low for x in chunk),
            chunk[-1].close, sum(x.volume for x in chunk))
        for key in order
        for chunk in [buckets[key]]
    ]


def _week_last_session(cal: MarketCalendar, d: date) -> SessionDay | None:
    """Last trading day of the week starting at `d` (Alpaca stamps weekly bars per week)."""
    days = cal.trading_days(d, d + timedelta(days=6))
    return days[-1] if days else None


def bar_end(bar: Bar, tf: TimeframeSpec, cal: MarketCalendar, session: str) -> int:
    """Unix time at which `bar` is final."""
    d = ny_date(bar.time)
    if tf.key == "1Day":
        sd = cal.day(d)
        return int(sd.close.timestamp()) if sd else bar.time + 86400
    if tf.key == "1Week":
        sd = _week_last_session(cal, d)
        return int(sd.close.timestamp()) if sd else bar.time + 7 * 86400
    nominal = bar.time + tf.minutes * 60
    sd = cal.day(d)
    if sd is None:
        return nominal
    return min(nominal, int(sd.bounds(session)[1].timestamp()))


def lookback_start(tf: TimeframeSpec, cal: MarketCalendar, end: datetime, bars_wanted: int,
                   session: str) -> datetime:
    """A start time far enough back to cover `bars_wanted` candles, plus slack for gaps."""
    end_day = end.astimezone(NY).date()
    if tf.key == "1Week":
        return datetime.combine(end_day - timedelta(weeks=bars_wanted + 2),
                                datetime.min.time(), NY)
    if tf.key == "1Day":
        days = cal.trading_days_back(end_day, bars_wanted + 3)
        return datetime.combine(days[0].day, datetime.min.time(), NY)
    session_minutes = (16 * 60 if session == "extended" else 390)
    per_day = max(1, session_minutes // tf.minutes)
    n_days = bars_wanted // per_day + 3
    days = cal.trading_days_back(end_day, n_days)
    return days[0].bounds(session)[0]
