"""Options-analysis thresholds. Every number is explicit and configurable.

Overrides: OPTIONS_CONFIG_PATH or backend/options.json, shaped like `to_dict()`; unknown
keys are rejected. Values keyed by mode ({"day": .., "swing": ..}) apply to setups from
that engine. Percentages are fractions (0.10 = 10 %).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path


def _per_mode(day, swing):
    return field(default_factory=lambda: {"day": day, "swing": swing})


# Contract-ranking weights (points out of 100). A rules-based ordering for comparison,
# never a probability of profit.
DEFAULT_WEIGHTS = {
    "spread": 20,      # tighter bid/ask spread (as % of mid) scores higher
    "activity": 15,    # today's volume and open interest relative to the minimums
    "delta_fit": 25,   # |delta| close to the target delta for the trading mode
    "time_decay": 20,  # smaller theta as a share of the premium per day
    "payoff": 20,      # Greek-estimated option gain at stock T1 vs loss at the stock stop
                       # (0 points when either estimate is low-reliability)
}


@dataclass(frozen=True)
class OptionsConfig:
    # ---- which contracts are candidates (all expirations are still listed)
    dte_window: dict = _per_mode([0, 14], [14, 120])   # days to expiration, inclusive
    strike_range_pct: dict = _per_mode(0.06, 0.15)     # strikes within +/- this of price...
    # ...extended so the stock stop and targets are always inside the strike range
    listing_horizon_days: int = 180                    # expirations listed up to this far out
    # ---- liquidity and quote quality (hard filters)
    max_spread_pct: dict = _per_mode(0.10, 0.12)       # (ask - bid) / mid
    very_wide_spread_pct: float = 0.25                 # labelled "very wide"
    spread_floor: float = 0.05                         # a spread <= $0.05 is never "wide"
    min_volume: dict = _per_mode(100, 25)              # contracts traded in the latest session
    min_open_interest: dict = _per_mode(500, 250)
    max_quote_age_seconds: dict = _per_mode(120, 900)  # older quotes are stale
    max_oi_age_days: int = 5                           # open interest is published daily
    # ---- Greeks
    delta_band: dict = _per_mode([0.35, 0.70], [0.40, 0.80])  # |delta| range for candidates
    target_delta: dict = _per_mode(0.50, 0.60)
    max_iv: float = 5.0                                # IV above 500 % is treated as bad data
    # ---- risk labels (warnings, not filters)
    short_dated_dte: int = 7
    heavy_decay_pct: dict = _per_mode(0.15, 0.03)      # |theta| / mid per day
    # ---- output
    max_listed: int = 80                               # contracts listed per analysis
    max_compared: int = 5                              # candidates in the comparison
    weights: dict = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    def to_dict(self) -> dict:
        return asdict(self)

    def with_overrides(self, overrides: dict) -> OptionsConfig:
        valid = {f.name for f in fields(self)}
        bad = set(overrides) - valid
        if bad:
            raise ValueError(f"Unknown options config keys: {sorted(bad)}")
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
        if abs(sum(out.weights.values()) - 100) > 1e-6:
            raise ValueError("options weights must sum to 100")
        for mode in ("day", "swing"):
            lo, hi = out.dte_window[mode]
            dlo, dhi = out.delta_band[mode]
            if not (0 <= lo <= hi):
                raise ValueError(f"dte_window.{mode} must be [min, max] with 0 <= min <= max")
            if not (0 < dlo < dhi <= 1):
                raise ValueError(f"delta_band.{mode} must satisfy 0 < low < high <= 1")
        return out


def load_options_config(path: str | os.PathLike | None = None) -> OptionsConfig:
    path = path or os.environ.get("OPTIONS_CONFIG_PATH")
    p = Path(path) if path else Path(__file__).resolve().parents[2] / "options.json"
    if not p.exists():
        return OptionsConfig()
    return OptionsConfig().with_overrides(json.loads(p.read_text()))
