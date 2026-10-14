"""Deterministic simulated market data for running the dashboard without Alpaca keys.

Everything produced here is DEMO DATA: a seeded random walk with trending and choppy regimes.
It does not reflect real prices and never updates in real time. Data covers regular sessions
up to the last completed trading day, follows the NYSE calendar (holidays, early closes),
and is shaped like Alpaca responses so it flows through the same validation pipeline.

Consistency across timeframes: a daily open/close path is generated first, then each session's
one-minute path is a Brownian bridge from that day's open to its close. Other timeframes
aggregate the minute path, so the watchlist, daily and intraday charts agree.
"""

from __future__ import annotations

import math
import random
import zlib
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

from ..market.calendar import NY, MarketCalendar, SessionDay
from ..market.sources import DEMO_FEED
from .base import TIMEFRAMES, Bar

DAILY_HISTORY = 420  # trading days of history per symbol

# (illustrative starting price, daily volatility, typical daily volume) -- not real quotes.
_PROFILES: dict[str, tuple[float, float, float]] = {
    "SPY": (520.0, 0.010, 70e6),
    "QQQ": (440.0, 0.013, 45e6),
    "NVDA": (120.0, 0.032, 300e6),
    "TSLA": (240.0, 0.035, 95e6),
    "AAPL": (210.0, 0.015, 55e6),
    "AMD": (150.0, 0.030, 50e6),
    "MSFT": (420.0, 0.014, 22e6),
    "META": (560.0, 0.020, 15e6),
    "AMZN": (190.0, 0.020, 40e6),
}
_DEFAULT_PROFILE = (100.0, 0.02, 10e6)


def _seed(*parts: object) -> int:
    return zlib.crc32("|".join(str(p) for p in parts).encode())


def _midnight(d: date) -> int:
    return int(datetime.combine(d, datetime.min.time(), NY).timestamp())


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@lru_cache(maxsize=64)
def _daily_path(symbol: str, days: tuple[SessionDay, ...]
                ) -> tuple[tuple[SessionDay, float, float, float], ...]:
    """(session, open, close, volume) per trading day."""
    start_price, daily_vol, daily_volume = _PROFILES.get(symbol, _DEFAULT_PROFILE)
    rng = random.Random(_seed(symbol, "daily", days[0].day.isoformat()))
    price = start_price * (1 + rng.uniform(-0.1, 0.1))
    drift, regime_left = 0.0, 0
    out = []
    for sd in days:
        if regime_left <= 0:
            trending = rng.random() < 0.6
            drift = rng.choice([-1, 1]) * daily_vol * rng.uniform(0.05, 0.18) if trending else 0.0
            regime_left = rng.randint(8, 30)
        regime_left -= 1
        open_ = price * (1 + rng.gauss(0, daily_vol * 0.3))
        pull = -0.02 * math.log(open_ / start_price)  # keeps the walk near its profile level
        ret = drift + pull + rng.gauss(0, daily_vol)
        close = open_ * (1 + ret)
        volume = daily_volume * (1 + abs(ret) / daily_vol * 0.4) * rng.uniform(0.7, 1.3)
        out.append((sd, open_, close, volume))
        price = close
    return tuple(out)


@lru_cache(maxsize=4096)
def _session_minutes(symbol: str, sd: SessionDay, open_: float, close: float,
                     volume: float) -> tuple[Bar, ...]:
    """One-minute bars for a session: a Brownian bridge from `open_` to `close`."""
    _, daily_vol, _ = _PROFILES.get(symbol, _DEFAULT_PROFILE)
    rng = random.Random(_seed(symbol, "minute", sd.day.isoformat()))
    n = int((sd.close - sd.open).total_seconds() // 60)
    sigma = daily_vol / math.sqrt(390)
    walk = [0.0]
    drift, regime_left = 0.0, 0
    for _ in range(n):
        if regime_left <= 0:
            drift = rng.choice([-1, 0, 0, 1]) * sigma * rng.uniform(0.1, 0.5)
            regime_left = rng.randint(20, 90)
        regime_left -= 1
        walk.append(walk[-1] + drift + rng.gauss(0, sigma))
    log_move = math.log(close / open_)
    path = [open_ * math.exp(log_move * i / n + walk[i] - walk[n] * i / n) for i in range(n + 1)]
    start = int(sd.open.timestamp())
    bars = []
    for i in range(n):
        o, c = path[i], path[i + 1]
        hi = max(o, c) * (1 + abs(rng.gauss(0, sigma * 0.35)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, sigma * 0.35)))
        pos = i / max(n - 1, 1)
        shape = 0.55 + 4.0 * (pos - 0.5) ** 2  # U-shaped intraday volume
        v = volume / n * shape * rng.uniform(0.5, 1.5) / 0.88
        bars.append(Bar(start + i * 60, o, hi, lo, c, v))
    return tuple(bars)


def _combine(chunk, time: int) -> Bar:
    return Bar(time, round(chunk[0].open, 2), round(max(b.high for b in chunk), 2),
               round(min(b.low for b in chunk), 2), round(chunk[-1].close, 2),
               float(int(sum(b.volume for b in chunk))))


class DemoSource:
    name = "demo"
    is_demo = True
    feed = DEMO_FEED
    adjustment = "n/a"

    def __init__(self, calendar: MarketCalendar | None = None,
                 now: datetime | None = None) -> None:
        self.calendar = calendar or MarketCalendar()
        self._now = now

    def _days(self) -> tuple[SessionDay, ...]:
        now = self._now or datetime.now(timezone.utc)
        last = self.calendar.last_completed_session(now)
        return tuple(self.calendar.trading_days_back(last.day, DAILY_HISTORY))

    def _daily_bars(self, symbol: str, path) -> list[Bar]:
        return [_combine(_session_minutes(symbol, sd, o, c, v), _midnight(sd.day))
                for sd, o, c, v in path]

    def fetch_bars(self, symbol: str, timeframe: str, start: datetime, end: datetime,
                   max_items: int = 50_000) -> list[Bar]:
        symbol = symbol.upper()
        lo, hi = start.timestamp(), end.timestamp()
        path = [p for p in _daily_path(symbol, self._days())
                if p[0].close.timestamp() > lo and _midnight(p[0].day) < hi]
        if timeframe == "1Day":
            return self._daily_bars(symbol, path)[:max_items]
        if timeframe == "1Week":
            weeks: dict[date, list[Bar]] = {}
            for b in self._daily_bars(symbol, path):
                d = datetime.fromtimestamp(b.time, NY).date()
                weeks.setdefault(d - timedelta(days=d.weekday()), []).append(b)
            return [_combine(chunk, _midnight(monday)) for monday, chunk in weeks.items()][:max_items]

        size = TIMEFRAMES[timeframe]
        out: list[Bar] = []
        for sd, o, c, v in path:
            minutes = _session_minutes(symbol, sd, o, c, v)
            for i in range(0, len(minutes), size):
                bar = _combine(minutes[i:i + size], minutes[i].time)
                if lo <= bar.time < hi:
                    out.append(bar)
        return out[:max_items]

    def fetch_snapshots(self, symbols: list[str]) -> dict[str, dict]:
        """Alpaca-shaped snapshots built from the last two simulated sessions."""
        out = {}
        days = self._days()
        for sym in symbols:
            sym = sym.upper()
            path = _daily_path(sym, days)[-2:]
            prev, last = self._daily_bars(sym, path)
            bar = lambda b: {"t": _iso(b.time), "o": b.open, "h": b.high, "l": b.low,  # noqa: E731
                             "c": b.close, "v": b.volume}
            out[sym] = {
                "latestTrade": {"p": last.close, "t": _iso(int(path[-1][0].close.timestamp()) - 1)},
                "dailyBar": bar(last),
                "prevDailyBar": bar(prev),
            }
        return out
