"""OHLCV integrity and timestamp-order validation.

Severity:
* error   -- data that must not be analysed (impossible OHLC, conflicting duplicates,
             out-of-order or misaligned timestamps, candles from the future).
             Offending bars are removed and analysis of the set is blocked.
* warning -- recoverable oddities (exact duplicate rows, which are dropped).
* info    -- expected gaps (e.g. minutes with no trades on a single-exchange feed).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..data.base import Bar

EPS = 1e-9


@dataclass
class QualityReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    dropped: int = 0
    checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {"ok": self.ok, "checked": self.checked, "dropped": self.dropped,
                "errors": self.errors[:20], "warnings": self.warnings[:20],
                "info": self.info[:20]}


def _ohlcv_problem(b: Bar) -> str | None:
    values = (b.open, b.high, b.low, b.close, b.volume)
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        return "non-numeric or non-finite value"
    if min(b.open, b.high, b.low, b.close) <= 0:
        return "non-positive price"
    if b.volume < 0:
        return "negative volume"
    if b.high + EPS < max(b.open, b.close, b.low):
        return "high below open/close/low"
    if b.low - EPS > min(b.open, b.close, b.high):
        return "low above open/close/high"
    return None


def validate_bars(bars: list[Bar], *, align_seconds: int | None, now: int,
                  report: QualityReport | None = None) -> tuple[list[Bar], QualityReport]:
    """Return (clean, ascending, de-duplicated bars, report).

    `align_seconds`: intraday bars must start on multiples of this (e.g. 300 for 5Min);
    None skips the check (daily/weekly bars).
    """
    rep = report or QualityReport()
    rep.checked += len(bars)
    clean: list[Bar] = []
    for i, b in enumerate(bars):
        if i and b.time < bars[i - 1].time:
            rep.errors.append(f"Out-of-order timestamp at index {i} ({b.time} after {bars[i - 1].time})")
        problem = _ohlcv_problem(b)
        if problem:
            rep.errors.append(f"Invalid OHLCV at {b.time}: {problem}")
            rep.dropped += 1
            continue
        if align_seconds and b.time % align_seconds:
            rep.errors.append(f"Misaligned candle start at {b.time} (expected multiple of {align_seconds}s)")
            rep.dropped += 1
            continue
        if b.time > now + 60:
            rep.errors.append(f"Candle at {b.time} is in the future")
            rep.dropped += 1
            continue
        clean.append(b)

    clean.sort(key=lambda b: b.time)
    deduped: list[Bar] = []
    for b in clean:
        if deduped and deduped[-1].time == b.time:
            if deduped[-1] == b:
                rep.warnings.append(f"Duplicate candle at {b.time} dropped")
            else:
                rep.errors.append(f"Conflicting duplicate candles at {b.time}")
            rep.dropped += 1
            continue
        deduped.append(b)
    return deduped, rep


def count_intraday_gaps(bars: list[Bar], step_seconds: int, session_bounds) -> int:
    """Missing `step` slots between consecutive bars inside the same session window.

    `session_bounds(ts)` returns (start, end) unix seconds of the session containing ts.
    """
    gaps = 0
    for prev, cur in zip(bars, bars[1:]):
        bounds = session_bounds(prev.time)
        if bounds and cur.time < bounds[1]:
            gaps += max(0, (cur.time - prev.time) // step_seconds - 1)
    return gaps
