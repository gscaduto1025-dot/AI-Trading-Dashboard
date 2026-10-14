"""Technical indicators over plain float lists.

Each function returns a list aligned with its input; positions without enough history
hold None.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from ..data.base import Bar

NY = ZoneInfo("America/New_York")

Series = list[float | None]


def sma(values: list[float], period: int) -> Series:
    out: Series = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def ema(values: list[float], period: int) -> Series:
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: list[float], period: int = 14) -> Series:
    """Wilder's RSI."""
    out: Series = [None] * len(values)
    if len(values) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        gains += max(delta, 0)
        losses += max(-delta, 0)
    avg_gain, avg_loss = gains / period, losses / period

    def _value(g: float, l: float) -> float:
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100 - 100 / (1 + g / l)

    out[period] = _value(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        delta = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0)) / period
        out[i] = _value(avg_gain, avg_loss)
    return out


def atr(bars: list[Bar], period: int = 14) -> Series:
    """Wilder's Average True Range."""
    out: Series = [None] * len(bars)
    if len(bars) <= period:
        return out
    trs = [bars[0].high - bars[0].low]
    for prev, cur in zip(bars, bars[1:]):
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    value = sum(trs[1:period + 1]) / period
    out[period] = value
    for i in range(period + 1, len(bars)):
        value = (value * (period - 1) + trs[i]) / period
        out[i] = value
    return out


def session_vwap(bars: list[Bar]) -> Series:
    """VWAP anchored to each New York trading day (resets at every new session)."""
    out: Series = []
    day = None
    pv = vol = 0.0
    for b in bars:
        d = datetime.fromtimestamp(b.time, NY).date()
        if d != day:
            day, pv, vol = d, 0.0, 0.0
        typical = (b.high + b.low + b.close) / 3
        pv += typical * b.volume
        vol += b.volume
        out.append(pv / vol if vol else typical)
    return out


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9
         ) -> tuple[Series, Series, Series]:
    fast_ema, slow_ema = ema(values, fast), ema(values, slow)
    line: Series = [f - s if f is not None and s is not None else None
                    for f, s in zip(fast_ema, slow_ema)]
    start = next((i for i, v in enumerate(line) if v is not None), len(line))
    sig_tail = ema([v for v in line[start:] if v is not None], signal)
    sig: Series = [None] * start + sig_tail
    hist: Series = [l - s if l is not None and s is not None else None for l, s in zip(line, sig)]
    return line, sig, hist


def last(series: Series) -> float | None:
    for v in reversed(series):
        if v is not None:
            return v
    return None
