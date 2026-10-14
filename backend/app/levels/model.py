"""Key-level data model, parameters and the transparent strength score.

Every tunable number used by the detector lives here so the methodology is auditable in
one place (see README "Key-level engine" for the prose version).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
METHOD_VERSION = "keylevels-1.0"

# ---------------------------------------------------------------- timeframe plans

# Frames fetched per mode. Weekly is always included for weekly zones; daily for PDH/PWH.
MODE_FRAMES: dict[str, list[str]] = {
    "day": ["5Min", "15Min", "1Hour", "1Day", "1Week"],
    "swing": ["1Hour", "4Hour", "1Day", "1Week"],
}
MODE_SWING_TFS = {"day": ["5Min", "15Min", "1Hour", "1Day"],
                  "swing": ["1Hour", "4Hour", "1Day", "1Week"]}
MODE_ZONE_TFS = {"day": ["1Hour", "1Day", "1Week"], "swing": ["4Hour", "1Day", "1Week"]}
MODE_BREAKOUT_TF = {"day": "5Min", "swing": "1Day"}
MODE_EMA_TFS = {"day": ["5Min", "1Day"], "swing": ["1Day"]}
FETCH_LIMIT = {"1Min": 800, "5Min": 600, "15Min": 400, "30Min": 400, "1Hour": 500,
               "4Hour": 400, "1Day": 500, "1Week": 260}

# Higher-timeframe significance (also the "T" component of the strength score).
TF_WEIGHT = {"1Min": 2, "5Min": 4, "15Min": 6, "30Min": 8, "1Hour": 10, "4Hour": 13,
             "1Day": 16, "1Week": 20}
TF_LABEL = {"1Min": "1-minute", "5Min": "5-minute", "15Min": "15-minute", "30Min": "30-minute",
            "1Hour": "1-hour", "4Hour": "4-hour", "1Day": "Daily", "1Week": "Weekly"}

# ------------------------------------------------------------------- parameters

ATR_PERIOD = 14
SWING_K = {"1Min": 3, "5Min": 3, "15Min": 3, "30Min": 3, "1Hour": 3, "4Hour": 2, "1Day": 3,
           "1Week": 2}  # fractal half-width: k bars on each side
SWING_LEVELS_PER_TF = 2  # most recent confirmed swing highs (and lows) reported per timeframe
OPENING_RANGE_MINUTES = 30
EMA_PERIODS = (9, 20, 50, 200)
EMA_WARMUP_MULTIPLE = 3  # an EMA is trusted once it has >= 3 x period candles

ZONE_LOOKBACK = {"1Hour": 500, "4Hour": 400, "1Day": 500, "1Week": 260}
ZONE_JOIN_ATR = 0.5  # a pivot joins a cluster if within 0.5 ATR of the cluster's top
ZONE_MAX_SPAN_ATR = 0.75  # ...and the cluster stays no wider than 0.75 ATR
ZONE_MIN_WIDTH_ATR = 0.25  # zones are widened symmetrically to at least 0.25 ATR
ZONE_MIN_REACTIONS = 2  # a "major" zone needs at least two confirmed swing reactions
ZONE_INVALID_CROSSES = 3  # closes crossing through the zone this often => no longer respected

EQUAL_EXTREME_ATR = 0.1  # swing highs/lows within 0.1 ATR are "equal"
LIQUIDITY_BAND_ATR = 0.25  # liquidity area extends 0.25 ATR beyond the extreme
LIQUIDITY_SWEEP_LOOKBACK = 10  # report sweeps that happened within the last 10 candles
LIQUIDITY_PER_SIDE = 3

BREAKOUT_BUFFER_ATR = 0.1  # close must clear the level by 0.1 ATR
BREAKOUT_SCAN_BARS = {"5Min": 156, "15Min": 52, "1Hour": 35, "1Day": 20, "1Week": 8}

CONFLUENCE_DAILY_ATR = 0.15  # levels within 0.15 daily-ATR of each other "overlap"
RECENCY_HALF_LIFE_DAYS = 30.0

SPLIT_RATIOS = (2, 3, 4, 5, 8, 10, 15, 20, 25, 30, 40, 50)
SPLIT_RATIO_TOLERANCE = 0.05

STRENGTH_NOTE = ("Heuristic ranking of how much historical evidence supports a level "
                 "(reactions, recency, timeframe, confluence). It is not a probability that "
                 "price will reverse there.")


# ------------------------------------------------------------------------ records

@dataclass(frozen=True)
class Swing:
    tf: str
    kind: str  # "high" | "low"
    index: int
    time: int  # candle start (unix)
    price: float
    confirmed_at: int  # unix time the confirming candle closed

    def to_event(self) -> dict:
        return {"time": self.time, "price": round(self.price, 4), "kind": f"swing {self.kind}",
                "timeframe": self.tf, "confirmed_at": self.confirmed_at}


@dataclass
class KeyLevel:
    type: str
    label: str
    category: str  # reference | zone | swing | indicator | liquidity | breakout
    timeframe: str
    low: float
    high: float
    formed_at: int  # unix: when the level first became knowable
    valid: bool
    status: str
    summary: str
    details: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    timeframes: list[str] = field(default_factory=list)
    # Scoring inputs supplied by detectors that know better than the generic rule.
    reactions: int | None = None
    last_reaction: int | None = None
    strength: dict = field(default_factory=dict)
    side: str = ""

    @property
    def price(self) -> float:
        return (self.low + self.high) / 2

    @property
    def id(self) -> str:
        raw = f"{self.type}|{self.label}|{self.timeframe}|{self.low:.4f}|{self.high:.4f}"
        return hashlib.sha1(raw.encode()).hexdigest()[:12]

    def to_dict(self, ticker: str, calculated_at: datetime) -> dict:
        return {
            "id": self.id,
            "ticker": ticker,
            "type": self.type,
            "label": self.label,
            "category": self.category,
            "timeframe": self.timeframe,
            "timeframes": self.timeframes or [self.timeframe],
            "price": round(self.price, 4),
            "low": round(self.low, 4),
            "high": round(self.high, 4),
            "is_zone": self.high - self.low > 1e-9,
            "side": self.side,
            "calculated_at": calculated_at.astimezone(NY).isoformat(),
            "formed_at": datetime.fromtimestamp(self.formed_at, NY).isoformat(),
            "valid": self.valid,
            "status": self.status,
            "evidence": {"summary": self.summary, "details": self.details,
                         "events": self.events[-12:]},
            "strength": self.strength,
        }


# ----------------------------------------------------------------------- scoring

def strength_label(score: int) -> str:
    return "strong" if score >= 60 else "moderate" if score >= 35 else "weak"


def score_levels(levels: list[KeyLevel], swings: list[Swing], daily_atr: float,
                 as_of_ts: int) -> None:
    """Attach a 0-100 heuristic score to every level (in place).

    score = R + C + T + F
      R reactions  = 7 * min(reactions, 5)                              (0-35)
      C recency    = 25 * 0.5 ** (days since last reaction / 30)        (0-25)
      T timeframe  = TF_WEIGHT[highest timeframe]                        (4-20)
      F confluence = 5 * min(other overlapping levels, 4)                (0-20)
    reactions defaults to the number of confirmed swing pivots (any timeframe in the plan)
    within the level's range +/- 0.15 daily ATR; zones supply their own pivot count.
    """
    tol = CONFLUENCE_DAILY_ATR * daily_atr
    anchors = [l for l in levels if l.category in ("reference", "zone", "indicator")]
    for lvl in levels:
        lo, hi = lvl.low - tol, lvl.high + tol
        near = [s for s in swings if lo <= s.price <= hi and s.confirmed_at <= as_of_ts]
        reactions = lvl.reactions if lvl.reactions is not None else len(near)
        last = lvl.last_reaction
        if last is None:
            last = max((s.time for s in near), default=lvl.formed_at)
        age_days = max(0.0, (as_of_ts - last) / 86400)
        tfs = lvl.timeframes or [lvl.timeframe]
        overlapping = {
            other.type for other in anchors
            if other is not lvl and other.type != lvl.type
            and other.low - tol <= lvl.high and lvl.low <= other.high + tol
        }
        r = 7 * min(reactions, 5)
        c = 25 * 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
        t = max(TF_WEIGHT.get(tf, 4) for tf in tfs)
        f = 5 * min(len(overlapping), 4)
        score = int(round(r + c + t + f))
        lvl.strength = {
            "score": score,
            "label": strength_label(score),
            "components": {"reactions": r, "recency": round(c, 1), "timeframe": t,
                           "confluence": f},
            "inputs": {"reactions": reactions, "days_since_last_reaction": round(age_days, 1),
                       "highest_timeframe": max(tfs, key=lambda x: TF_WEIGHT.get(x, 0)),
                       "overlapping_levels": sorted(overlapping)},
            "note": STRENGTH_NOTE,
        }
