"""Relative-volume evidence and how far it can be trusted.

Alpaca's IEX feed reports only IEX's own trades (typically 1-6% of consolidated volume), so
relative volume computed from it is a proxy, not a measure of market participation.
Consolidated (SIP) bars are available on the free plan once they are more than 15 minutes
old, so the market-data service overlays SIP volume on every candle old enough
(`VolumeOverlay`); only the most recent candles must fall back to the IEX proxy.

Every volume reading carries a verification level:
  FULL         relative volume from consolidated volume (SIP feeds, the SIP overlay, or
               the demo source's simulated volume);
  LIMITED      IEX-only proxy, accepted as confirmation (partial_volume_policy="limited");
  UNVERIFIED   IEX-only proxy that may reject but cannot confirm (default policy);
  UNAVAILABLE  the feed has no volume for this candle.
A volume-dependent signal is fully confirmed only when every required volume condition is
FULL (`volume_confirmation`).
"""

from __future__ import annotations

from dataclasses import dataclass

FULL, LIMITED, UNVERIFIED, UNAVAILABLE = "FULL", "LIMITED", "UNVERIFIED", "UNAVAILABLE"
NOT_REQUIRED = "NOT_REQUIRED"
_RANK = {FULL: 0, LIMITED: 1, UNVERIFIED: 2, UNAVAILABLE: 3}

PARTIAL, CONSOLIDATED, DEMO = "partial", "consolidated", "demo"


@dataclass(frozen=True)
class VolumeOverlay:
    """Consolidated volume for one symbol and timeframe, from the market-data service.

    `volumes` maps a candle's start time to its SIP volume; `covered_until` is the newest
    candle end the SIP data may cover (the plan's 15-minute embargo). Candles ending later
    have no consolidated volume yet."""

    volumes: dict[int, float]
    covered_until: int
    feed: str = "sip"
    label: str = "consolidated (SIP)"


@dataclass(frozen=True)
class VolumeSource:
    """What the feed's volume represents for a context: `scope` is partial (IEX),
    consolidated (SIP) or demo; `overlays` holds SIP volume per timeframe."""

    scope: str = CONSOLIDATED
    feed_label: str = "feed"
    policy: str = "unverified"
    overlays: dict | None = None

    def overlay(self, tf: str) -> VolumeOverlay | None:
        return (self.overlays or {}).get(tf)


@dataclass(frozen=True)
class VolumeReading:
    rv: float | None  # relative volume used for the decision (None: nothing to judge)
    level: str  # FULL | LIMITED | UNVERIFIED | UNAVAILABLE
    source: str  # where rv came from

    @property
    def proxy(self) -> bool:
        return self.level in (LIMITED, UNVERIFIED)

    def rejects(self, threshold: float) -> bool:
        """A measured relative volume below the threshold rejects the signal; a proxy may
        reject (the conservative direction) but never confirms under 'unverified'."""
        return self.rv is not None and self.rv < threshold

    def passed(self, threshold: float) -> bool | None:
        """Condition result: True/False when measured, None when it can't be confirmed."""
        if self.rv is None or self.level == UNAVAILABLE:
            return None
        if self.rv < threshold:
            return False
        return None if self.level == UNVERIFIED else True

    def detail(self, unit: str) -> str:
        if self.level == UNAVAILABLE or self.rv is None:
            return f"volume unavailable: {self.source} (UNAVAILABLE)"
        base = f"{self.rv:.2f}x {unit}"
        if self.level == FULL:
            return f"{base} ({self.source})"
        return (f"{self.level}: {base} on {self.source}; consolidated volume not available "
                f"for this candle yet")

    def quality_rv(self) -> tuple[float | None, str]:
        """(relative volume, note) for the quality score: unverified evidence scores
        neutral rather than as a measurement."""
        if self.level == FULL:
            return self.rv, ""
        if self.level == LIMITED:
            return self.rv, f"LIMITED: {self.source} proxy"
        if self.level == UNVERIFIED:
            return None, f"UNVERIFIED: {self.source} only (scored neutral)"
        return None, "volume unavailable on this feed (scored neutral)"


def _no_volume(src: VolumeSource) -> str:
    return ("no volume reported for this candle" if src.feed_label == "feed"
            else f"no volume on the {src.feed_label} feed for this candle")


def read_volume(proxy_rv: float | None, full_rv: float | None, volume_available: bool,
                src: VolumeSource) -> VolumeReading:
    """Pick the relative volume for one candle: consolidated when available, otherwise
    the feed's own (a proxy on a partial feed)."""
    if src.scope != PARTIAL:
        if not volume_available or proxy_rv is None:
            return VolumeReading(None, UNAVAILABLE, _no_volume(src))
        label = "simulated demo volume" if src.scope == DEMO else f"{src.feed_label} volume"
        return VolumeReading(proxy_rv, FULL, label)
    if full_rv is not None:
        return VolumeReading(full_rv, FULL, "consolidated volume (SIP)")
    if not volume_available or proxy_rv is None:
        return VolumeReading(None, UNAVAILABLE, _no_volume(src))
    level = LIMITED if src.policy == "limited" else UNVERIFIED
    return VolumeReading(proxy_rv, level, f"{src.feed_label}-only volume")


def condition_level(c) -> str | None:
    v = c.get("verification") if isinstance(c, dict) else getattr(c, "verification", None)
    return v


def volume_confirmation(conditions) -> str:
    """Weakest verification level among required volume conditions (NOT_REQUIRED when
    the signal does not depend on volume)."""
    levels = []
    for c in conditions or []:
        req = c.get("required", True) if isinstance(c, dict) else getattr(c, "required", True)
        lvl = condition_level(c)
        if lvl and req:
            levels.append(lvl)
    if not levels:
        return NOT_REQUIRED
    return max(levels, key=lambda x: _RANK[x])


def masked_overlay(bars, end: list[int], ov: VolumeOverlay | None):
    """Bars with SIP volume substituted where covered, and the covered flags. Coverage is
    a prefix in time, so the relative volume of a covered candle (whose baseline is older
    candles) uses consolidated volume only."""
    if ov is None:
        return None, [False] * len(bars)
    from dataclasses import replace
    covered = [b.time in ov.volumes and e <= ov.covered_until for b, e in zip(bars, end)]
    sub = [replace(b, volume=ov.volumes[b.time]) if c else b for b, c in zip(bars, covered)]
    return sub, covered
