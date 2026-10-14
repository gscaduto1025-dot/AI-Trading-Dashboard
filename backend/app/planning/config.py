"""Trade-planning and risk rules shared by the day- and swing-trading engines.

Every number here is explicit and configurable. Overrides: PLANNING_CONFIG_PATH or
backend/planning.json, shaped like `to_dict()`; unknown keys are rejected. Values keyed by
mode ({"day": .., "swing": ..}) apply to that engine. "ATR" is the 14-candle ATR of the
setup's own timeframe at the candle before the one being judged.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path


def _per_mode(day, swing):
    return field(default_factory=lambda: {"day": day, "swing": swing})


# Setup-quality weights (points out of 100). Documented in the README; never a probability.
DEFAULT_WEIGHTS = {
    "higher_timeframe": 15,  # higher-timeframe bias aligned / neutral / opposed
    "level_strength": 15,  # strength of the levels the setup is built on
    "price_action": 20,  # share of required confirmation conditions passed
    "relative_volume": 10,  # relative volume on the signal candle
    "trend_structure": 15,  # swing structure aligned with the trade (or shifted, reversals)
    "risk_reward": 25,  # reward/risk to T1, full points at 2 x min_rr
}


@dataclass(frozen=True)
class PlanningConfig:
    # ---- risk/reward
    min_rr: float = 2.0  # reward to T1 / risk; below this the setup is NO_TRADE
    min_rr_by_mode: dict = _per_mode(None, None)  # optional per-mode override of min_rr
    # ---- entries
    entry_zone_atr: dict = _per_mode(0.25, 0.5)  # entry zone: trigger .. trigger +/- this x ATR
    # ---- stops: structural invalidation level + a volatility buffer
    stop_buffer_atr: dict = _per_mode(0.1, 0.25)
    min_risk_atr: float = 0.05  # risk below this x ATR is noise (zero-risk guard)
    max_risk_atr: dict = _per_mode(3.0, 3.0)  # wider stops are rejected
    # ---- targets
    level_clearance_atr: float = 0.05  # a target level must lie at least this far beyond entry
    t2_min_gap_atr: float = 0.25  # T2 must lie at least this far beyond T1
    atr_projection: dict = _per_mode(2.0, 3.0)  # T1 = entry +/- this x ATR when nothing lies ahead
    atr_extension: dict = _per_mode(1.0, 1.5)  # T2 = T1 +/- this x ATR when nothing lies beyond T1
    # ---- no-trade conditions
    max_conflicts: int = 2  # this many conflicting signals = contradictory -> NO_TRADE
    max_spread_pct: dict = _per_mode(0.25, 0.75)  # quoted spread as % of price, when known
    # ---- quality score
    weights: dict = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    conflict_penalty: float = 10.0  # points removed per conflicting signal

    def min_rr_for(self, mode: str) -> float:
        v = (self.min_rr_by_mode or {}).get(mode)
        return float(v) if v is not None else self.min_rr

    def to_dict(self) -> dict:
        return asdict(self)

    def with_overrides(self, overrides: dict) -> PlanningConfig:
        valid = {f.name for f in fields(self)}
        bad = set(overrides) - valid
        if bad:
            raise ValueError(f"Unknown planning config keys: {sorted(bad)}")
        vals = dict(overrides)
        for k, v in vals.items():
            cur = getattr(self, k)
            if isinstance(cur, dict):
                if not isinstance(v, dict):
                    raise ValueError(f"{k} must be an object")
                unknown = set(v) - set(cur)
                if unknown:
                    raise ValueError(f"Unknown keys in {k}: {sorted(unknown)}")
                vals[k] = {**cur, **v}
        out = replace(self, **vals)
        if out.min_rr <= 0:
            raise ValueError("min_rr must be positive")
        if abs(sum(out.weights.values()) - 100) > 1e-6:
            raise ValueError("quality weights must sum to 100")
        return out


def load_planning_config() -> PlanningConfig:
    path = os.environ.get("PLANNING_CONFIG_PATH")
    default = Path(__file__).resolve().parents[2] / "planning.json"
    p = Path(path) if path else default
    cfg = PlanningConfig()
    if p.is_file():
        cfg = cfg.with_overrides(json.loads(p.read_text()))
    return cfg
