"""Market data types shared by every provider."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol

# Timeframes the dashboard exposes, with their length in minutes.
TIMEFRAMES: dict[str, int] = {
    "1Min": 1,
    "5Min": 5,
    "15Min": 15,
    "1Hour": 60,
    "1Day": 390,  # one regular session
}

# Which timeframes make sense for each trading style, and the default one.
MODE_TIMEFRAMES: dict[str, list[str]] = {
    "day": ["1Min", "5Min", "15Min"],
    "swing": ["1Hour", "1Day"],
}
DEFAULT_TIMEFRAME: dict[str, str] = {"day": "5Min", "swing": "1Day"}


class DataError(Exception):
    """Raised when market data cannot be retrieved."""


@dataclass(frozen=True)
class Bar:
    time: int  # unix seconds (bar open), UTC
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    prev_close: float
    change: float
    change_pct: float
    as_of: int  # unix seconds

    def to_dict(self) -> dict:
        return asdict(self)


class MarketDataProvider(Protocol):
    """Source of OHLCV bars and quotes. `is_demo` marks synthetic data."""

    name: str
    is_demo: bool

    def get_bars(self, symbol: str, timeframe: str, limit: int = 300) -> list[Bar]: ...

    def get_quotes(self, symbols: list[str]) -> list[Quote]: ...


def validate_timeframe(timeframe: str) -> str:
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe {timeframe!r}; use one of {list(TIMEFRAMES)}")
    return timeframe
