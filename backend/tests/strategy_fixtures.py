"""Hand-built market fixtures for strategy tests.

Layout (all New York time):
* Daily: 80 sessions ending Mon 2026-03-09. Trend "up": closes rise 0.5/day to 99.0 on
  Fri 03-06 (close > EMA20 > EMA50 => daily bullish); "down" mirrors from above. Every
  daily high/low is monotonic, so the daily chart has no swing points or zones that could
  interfere. The 03-09 daily candle is O 99.8 H 101 L 99 C 100.
* Mon 03-09, 5-minute: 78 flat candles O 100 H 100.2 L 99.8 C 100 V 1000, except the
  09:30+20x5 = 11:10 candle (high 101 => PDH) and the 13:40 candle (low 99 => PDL).
  5-minute ATR is therefore ~0.4-0.45.
* Tue 03-10 (the session under test): rows supplied by each test, starting 09:30.
15-minute and 1-hour candles are aggregated from the 5-minute ones (session-anchored).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app.data.base import Bar
from app.market.calendar import NY, MarketCalendar
from app.planning.config import PlanningConfig
from app.strategies.config import StrategyConfig
from app.strategies.engine import evaluate_symbol

CAL = MarketCalendar()
PREV, DAY = date(2026, 3, 9), date(2026, 3, 10)
PDH, PDL = 101.0, 99.0
# The hand-built sessions have few levels above/below, so most targets are ATR projections.
# Pattern-detection tests therefore plan with min_rr 1.0; the 2:1 default and every other
# planning rule are tested in test_planning.py.
PATTERN_PLANNING = PlanningConfig(min_rr=1.0)


def t(day: date, hh: int, mm: int) -> int:
    return int(datetime(day.year, day.month, day.day, hh, mm, tzinfo=NY).timestamp())


def five_min(day: date, rows) -> list[Bar]:
    t0 = t(day, 9, 30)
    return [Bar(t0 + i * 300, *r) for i, r in enumerate(rows)]


def prev_session() -> list[Bar]:
    rows = []
    for i in range(78):
        o, h, l, c = 100.0, 100.2, 99.8, 100.0
        if i == 20:
            h = PDH
        if i == 50:
            l = PDL
        rows.append((o, h, l, c, 1000.0))
    return five_min(PREV, rows)


def daily(trend: str = "up", volume: float = 1e6) -> list[Bar]:
    days = CAL.trading_days_back(date(2026, 3, 6), 79)
    out = []
    for k, sd in enumerate(days):
        if trend == "up":
            c = 60 + 0.5 * k
            o, h, l = c - 0.25, c + 0.25, c - 0.5
        elif trend == "down":
            c = 140 - 0.5 * k
            o, h, l = c + 0.25, c + 0.5, c - 0.25
        else:
            c = 100 + (0.1 if k % 2 else -0.1)
            o, h, l = c, c + 0.3, c - 0.3
        out.append(Bar(t(sd.day, 0, 0), o, h, l, c, volume))
    out.append(Bar(t(PREV, 0, 0), 99.8, PDH, PDL, 100.0, volume))
    return out


def aggregate(bars: list[Bar], minutes: int) -> list[Bar]:
    """Session-anchored aggregation (09:30 + n x minutes)."""
    groups: dict[int, list[Bar]] = {}
    for b in bars:
        d = datetime.fromtimestamp(b.time, NY).date()
        anchor = t(d, 9, 30)
        key = anchor + (b.time - anchor) // (minutes * 60) * minutes * 60
        groups.setdefault(key, []).append(b)
    return [Bar(k, g[0].open, max(x.high for x in g), min(x.low for x in g), g[-1].close,
                sum(x.volume for x in g)) for k, g in sorted(groups.items())]


def frames(today_rows, trend="up", daily_volume=1e6) -> dict[str, list[Bar]]:
    m5 = prev_session() + five_min(DAY, today_rows)
    d = daily(trend, daily_volume)
    return {"5Min": m5, "15Min": aggregate(m5, 15), "1Hour": aggregate(m5, 60), "1Day": d,
            "1Week": []}


def after_bars(n: int) -> datetime:
    """The moment the n-th candle of the test session (5-minute) has closed."""
    return datetime.fromtimestamp(t(DAY, 9, 30) + n * 300, NY)


def run(today_rows, as_of=None, trend="up", cfg: StrategyConfig | None = None,
        problems=None, daily_volume=1e6, planning: PlanningConfig | None = None):
    as_of = as_of or after_bars(len(today_rows))
    ctx, setups = evaluate_symbol("TEST", frames(today_rows, trend, daily_volume), as_of, CAL,
                                  cfg or StrategyConfig(), problems,
                                  planning=planning or PATTERN_PLANNING)
    return ctx, setups


def pick(setups, strategy: str, direction: str, tf: str = "5Min"):
    return next(s for s in setups if s.strategy == strategy and s.direction == direction
                and s.timeframe == tf)


def bar_time(n: int) -> int:
    return t(DAY, 9, 30) + n * 300


__all__ = ["run", "pick", "PATTERN_PLANNING", "after_bars", "bar_time", "frames", "PDH", "PDL", "DAY", "CAL",
           "timedelta"]
