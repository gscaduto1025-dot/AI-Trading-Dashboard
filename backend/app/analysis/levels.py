"""Support and resistance detection.

Two sources of levels:
* Swing pivots: local highs/lows confirmed by `window` bars on each side, clustered
  together when they sit within a fraction of ATR of each other. More touches and more
  recent touches make a stronger level.
* Reference levels: prior-day high/low/close, the session open and opening range for day
  trading; multi-week highs/lows for swing trading.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from ..data.base import Bar

NY = ZoneInfo("America/New_York")


@dataclass
class Level:
    price: float
    kind: str  # "support" | "resistance"
    source: str  # e.g. "Swing pivot", "Prior day high"
    touches: int = 1
    strength: float = 0.0  # 0..1
    last_touch: int | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["price"] = round(self.price, 2)
        d["strength"] = round(self.strength, 2)
        return d


def find_pivots(bars: list[Bar], window: int) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Return (pivot_highs, pivot_lows) as (index, price) pairs."""
    highs, lows = [], []
    for i in range(window, len(bars) - window):
        seg = bars[i - window:i + window + 1]
        if bars[i].high == max(b.high for b in seg):
            highs.append((i, bars[i].high))
        if bars[i].low == min(b.low for b in seg):
            lows.append((i, bars[i].low))
    return highs, lows


def cluster_pivots(bars: list[Bar], pivots: list[tuple[int, float]], tolerance: float
                   ) -> list[Level]:
    """Merge pivots within `tolerance` of each other into levels."""
    groups: list[list[tuple[int, float]]] = []
    for idx, price in sorted(pivots, key=lambda p: p[1]):
        # Compare against the group's mean so a staircase of pivots can't chain together.
        if groups and abs(price - sum(p for _, p in groups[-1]) / len(groups[-1])) <= tolerance:
            groups[-1].append((idx, price))
        else:
            groups.append([(idx, price)])
    n = len(bars)
    levels = []
    for g in groups:
        avg = sum(p for _, p in g) / len(g)
        latest = max(i for i, _ in g)
        recency = latest / max(n - 1, 1)
        strength = min(1.0, 0.25 * len(g) + 0.35 * recency)
        levels.append(Level(avg, "", "Swing pivot", len(g), strength, bars[latest].time))
    return levels


def _session_groups(bars: list[Bar]) -> list[list[Bar]]:
    sessions: list[list[Bar]] = []
    day = None
    for b in bars:
        d = datetime.fromtimestamp(b.time, NY).date()
        if d != day:
            sessions.append([])
            day = d
        sessions[-1].append(b)
    return sessions


def reference_levels(bars: list[Bar], mode: str, bar_minutes: int) -> list[Level]:
    refs: list[Level] = []
    if mode == "day" and bar_minutes < 390:
        sessions = _session_groups(bars)
        if len(sessions) >= 2:
            prev = sessions[-2]
            refs.append(Level(max(b.high for b in prev), "", "Prior day high", strength=0.8))
            refs.append(Level(min(b.low for b in prev), "", "Prior day low", strength=0.8))
            refs.append(Level(prev[-1].close, "", "Prior day close", strength=0.6))
        today = sessions[-1]
        refs.append(Level(today[0].open, "", "Session open", strength=0.5))
        or_bars = today[:max(1, 30 // bar_minutes)]
        if len(today) > len(or_bars):
            refs.append(Level(max(b.high for b in or_bars), "", "Opening range high", strength=0.7))
            refs.append(Level(min(b.low for b in or_bars), "", "Opening range low", strength=0.7))
    else:
        # Exclude the latest bar: it is the one being evaluated against these levels.
        window = bars[-21:-1]
        refs.append(Level(max(b.high for b in window), "", "20-bar high", strength=0.7))
        refs.append(Level(min(b.low for b in window), "", "20-bar low", strength=0.7))
        if len(bars) >= 120:
            long_window = bars[-251:-1]
            refs.append(Level(max(b.high for b in long_window), "", "52-week high"
                              if bar_minutes >= 390 else "Long-range high", strength=0.9))
            refs.append(Level(min(b.low for b in long_window), "", "52-week low"
                              if bar_minutes >= 390 else "Long-range low", strength=0.9))
    return refs


def compute_levels(bars: list[Bar], atr_value: float, mode: str, bar_minutes: int,
                   max_per_side: int | None = 4) -> list[Level]:
    """Key levels sorted by price (descending), labelled relative to the last close."""
    if not bars:
        return []
    price = bars[-1].close
    window = 3 if mode == "day" else 5
    lookback = bars[-200:]
    highs, lows = find_pivots(lookback, window)
    tolerance = max(atr_value * 0.4, price * 0.001)
    candidates = cluster_pivots(lookback, highs + lows, tolerance)
    candidates += reference_levels(bars, mode, bar_minutes)

    # Drop near-duplicates, keeping the stronger level.
    merged: list[Level] = []
    for lvl in sorted(candidates, key=lambda l: -l.strength):
        if all(abs(lvl.price - m.price) > tolerance * 0.75 for m in merged):
            merged.append(lvl)

    for lvl in merged:
        lvl.kind = "resistance" if lvl.price > price else "support"
    above = sorted((l for l in merged if l.kind == "resistance"), key=lambda l: l.price)
    below = sorted((l for l in merged if l.kind == "support"), key=lambda l: -l.price)
    if max_per_side is not None:
        above, below = above[:max_per_side], below[:max_per_side]
    return sorted(above + below, key=lambda l: -l.price)


def nearest(levels: list[Level], kind: str) -> Level | None:
    side = [l for l in levels if l.kind == kind]
    if not side:
        return None
    return min(side, key=lambda l: l.price) if kind == "resistance" else max(side, key=lambda l: l.price)
