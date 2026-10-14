"""Deterministic synthetic market data for running the dashboard without an API key.

Everything produced here is DEMO DATA. Prices are a seeded random walk with trending and
choppy regimes so that the analysis engine has something realistic-looking to work on.
They do not reflect real market prices and do not change in real time: the same symbol and
calendar day always generate the same bars.

Consistency across timeframes: a daily open/close path is generated first, then each
session's one-minute path is a Brownian bridge from that day's open to its close. Intraday
timeframes aggregate the minute path, and daily highs/lows come from it, so the watchlist,
the daily chart and the intraday charts all agree with each other.
"""

from __future__ import annotations

import math
import random
import zlib
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from .base import TIMEFRAMES, Bar, Quote, validate_timeframe

NY = ZoneInfo("America/New_York")
SESSION_MINUTES = 390
DAILY_HISTORY = 420  # weekdays of history generated per symbol

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


def _last_weekday(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _weekdays_back(end: date, count: int) -> list[date]:
    days: list[date] = []
    d = _last_weekday(end)
    while len(days) < count:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


@lru_cache(maxsize=64)
def _daily_path(symbol: str, anchor: date) -> tuple[tuple[date, float, float, float], ...]:
    """(day, open, close, volume) for DAILY_HISTORY weekdays ending at `anchor`."""
    start_price, daily_vol, daily_volume = _PROFILES.get(symbol, _DEFAULT_PROFILE)
    # Seeded on the window start, so the series is fixed for a given calendar day.
    days = _weekdays_back(anchor, DAILY_HISTORY)
    rng = random.Random(_seed(symbol, "daily", days[0].isoformat()))
    price = start_price * (1 + rng.uniform(-0.1, 0.1))
    drift, regime_left = 0.0, 0
    out = []
    for d in days:
        if regime_left <= 0:
            trending = rng.random() < 0.6
            drift = rng.choice([-1, 1]) * daily_vol * rng.uniform(0.05, 0.18) if trending else 0.0
            regime_left = rng.randint(8, 30)
        regime_left -= 1
        open_ = price * (1 + rng.gauss(0, daily_vol * 0.3))
        # Gentle mean reversion keeps the illustrative price near its profile level.
        pull = -0.02 * math.log(open_ / start_price)
        ret = drift + pull + rng.gauss(0, daily_vol)
        close = open_ * (1 + ret)
        volume = daily_volume * (1 + abs(ret) / daily_vol * 0.4) * rng.uniform(0.7, 1.3)
        out.append((d, open_, close, volume))
        price = close
    return tuple(out)


@lru_cache(maxsize=4096)
def _session_minutes(symbol: str, day: date, open_: float, close: float,
                     volume: float) -> tuple[Bar, ...]:
    """One-minute bars for a session: a Brownian bridge from `open_` to `close`."""
    _, daily_vol, _ = _PROFILES.get(symbol, _DEFAULT_PROFILE)
    rng = random.Random(_seed(symbol, "minute", day.isoformat()))
    n = SESSION_MINUTES
    sigma = daily_vol / math.sqrt(n)
    walk = [0.0]
    drift, regime_left = 0.0, 0
    for _ in range(n):
        if regime_left <= 0:
            drift = rng.choice([-1, 0, 0, 1]) * sigma * rng.uniform(0.1, 0.5)
            regime_left = rng.randint(20, 90)
        regime_left -= 1
        walk.append(walk[-1] + drift + rng.gauss(0, sigma))
    log_move = math.log(close / open_)
    path = [
        open_ * math.exp(log_move * i / n + walk[i] - walk[n] * i / n) for i in range(n + 1)
    ]
    open_dt = datetime.combine(day, time(9, 30), NY)
    bars = []
    for i in range(n):
        o, c = path[i], path[i + 1]
        hi = max(o, c) * (1 + abs(rng.gauss(0, sigma * 0.35)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, sigma * 0.35)))
        pos = i / (n - 1)
        shape = 0.55 + 4.0 * (pos - 0.5) ** 2  # U-shaped intraday volume
        v = volume / n * shape * rng.uniform(0.5, 1.5) / 0.88
        ts = int((open_dt + timedelta(minutes=i)).timestamp())
        bars.append(Bar(ts, o, hi, lo, c, v))
    return tuple(bars)


def _aggregate(minutes: tuple[Bar, ...], size: int) -> list[Bar]:
    out = []
    for i in range(0, len(minutes), size):
        chunk = minutes[i:i + size]
        out.append(Bar(
            chunk[0].time,
            round(chunk[0].open, 2),
            round(max(b.high for b in chunk), 2),
            round(min(b.low for b in chunk), 2),
            round(chunk[-1].close, 2),
            float(int(sum(b.volume for b in chunk))),
        ))
    return out


class DemoDataProvider:
    name = "demo"
    is_demo = True

    def __init__(self, today: date | None = None) -> None:
        self._today = today

    def _anchor(self) -> date:
        return _last_weekday(self._today or datetime.now(NY).date())

    def get_bars(self, symbol: str, timeframe: str, limit: int = 300) -> list[Bar]:
        validate_timeframe(timeframe)
        symbol = symbol.upper()
        days = _daily_path(symbol, self._anchor())
        if timeframe == "1Day":
            bars = []
            for d, o, c, v in days[-limit:]:
                session = _aggregate(_session_minutes(symbol, d, o, c, v), SESSION_MINUTES)[0]
                bars.append(session)
            return bars

        size = TIMEFRAMES[timeframe]
        per_session = math.ceil(SESSION_MINUTES / size)
        needed = min(math.ceil(limit / per_session), len(days))
        bars = []
        for d, o, c, v in days[-needed:]:
            bars.extend(_aggregate(_session_minutes(symbol, d, o, c, v), size))
        return bars[-limit:]

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        quotes = []
        for sym in symbols:
            prev, last = self.get_bars(sym, "1Day", limit=2)
            change = last.close - prev.close
            quotes.append(Quote(sym.upper(), last.close, prev.close, round(change, 2),
                                round(change / prev.close * 100, 2), last.time))
        return quotes
