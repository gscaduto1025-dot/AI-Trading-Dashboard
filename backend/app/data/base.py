"""Market data types shared by every provider."""

from __future__ import annotations

from dataclasses import asdict, dataclass

# Timeframes the dashboard exposes, with their nominal length in minutes. A trading day is
# counted as one regular session (390 minutes) so the engine can tell intraday from daily.
TIMEFRAMES: dict[str, int] = {
    "1Min": 1,
    "5Min": 5,
    "15Min": 15,
    "30Min": 30,
    "1Hour": 60,
    "4Hour": 240,
    "1Day": 390,
    "1Week": 5 * 390,
}
INTRADAY_TIMEFRAMES = ("1Min", "5Min", "15Min", "30Min", "1Hour", "4Hour")

# Which timeframes each trading style suggests, and the default one. Every timeframe can be
# selected in either mode; the mode only changes the analysis parameters.
MODE_TIMEFRAMES: dict[str, list[str]] = {
    "day": ["1Min", "5Min", "15Min", "30Min", "1Hour"],
    "swing": ["1Hour", "4Hour", "1Day", "1Week"],
}
DEFAULT_TIMEFRAME: dict[str, str] = {"day": "5Min", "swing": "1Day"}

SESSIONS = ("regular", "extended")


# ------------------------------------------------------------------------------ errors

class DataError(Exception):
    """Market data could not be retrieved. `code` is a stable identifier for the UI."""

    code = "MARKET_DATA_UNAVAILABLE"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class AuthError(DataError):
    code = "AUTH_FAILED"


class SubscriptionError(DataError):
    """The account's market data subscription does not cover the request (HTTP 403)."""

    code = "SUBSCRIPTION_NOT_PERMITTED"


class RateLimitError(DataError):
    code = "RATE_LIMITED"


class ProviderUnavailable(DataError):
    code = "PROVIDER_UNAVAILABLE"


class InvalidRequest(DataError):
    code = "INVALID_REQUEST"


# ------------------------------------------------------------------------------- types

@dataclass(frozen=True)
class Bar:
    time: int  # unix seconds of the bar's start, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_dict(self) -> dict:
        return asdict(self)


def validate_timeframe(timeframe: str) -> str:
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe {timeframe!r}; use one of {list(TIMEFRAMES)}")
    return timeframe
