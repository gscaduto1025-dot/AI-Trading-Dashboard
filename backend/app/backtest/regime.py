"""Market-regime label at the moment of a signal, from daily candles COMPLETE before it.

trend:      close above / below its 200-day simple moving average
volatility: 20-day standard deviation of daily returns above / below its median over the
            previous 252 sessions
The basis is SPY when the data source has it (the broad market), otherwise the traded
ticker itself; the run reports which.
"""

from __future__ import annotations

import math
import statistics

from .data import Series


def _std(xs: list[float]) -> float:
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def regime_at(daily: Series, ts: int) -> str:
    bars = daily.completed(ts)
    if len(bars) < 200:
        return "unknown (under 200 days of history)"
    closes = [b.close for b in bars[-273:]]
    trend = "uptrend" if closes[-1] > sum(closes[-200:]) / 200 else "downtrend"
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    if len(rets) < 40:
        return trend
    vols = [_std(rets[i - 20:i]) for i in range(20, len(rets) + 1)]
    vol = "high volatility" if vols[-1] > statistics.median(vols[-252:]) else "low volatility"
    return f"{trend}, {vol}"
