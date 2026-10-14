"""Swing-trading strategy definitions: explicit thresholds, separate from day trading.

Overrides: SWING_STRATEGY_CONFIG_PATH or backend/swing_strategies.json, shaped like
`to_dict()`; unknown keys are rejected. "ATR" is Wilder's 14-candle ATR on the strategy's
own timeframe (daily or weekly) at the candle before the one being judged. Values keyed by
timeframe ({"1Day": .., "1Week": ..}) are counted in that timeframe's candles.
Entry zones, stop buffers, targets, stop-width limits and the minimum reward/risk are
trade-planning rules shared with the day engine: app/planning/config.py.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

SWING_PRIMARY_TIMEFRAMES = ["1Day", "1Week"]
SWING_SUPPORT_TIMEFRAMES = ["4Hour", "1Hour"]


def _per_tf(day, week):
    return field(default_factory=lambda: {"1Day": day, "1Week": week})


@dataclass(frozen=True)
class SwingGates:
    min_avg_dollar_volume: float = 20_000_000  # 20-day average close x volume
    min_daily_history: int = 220  # EMA200 needs history; trend strategies require it
    max_trigger_range_atr: float = 4.0  # a trigger candle wider than this is abnormal
    max_extension_atr: float = 1.5  # entry candle may close at most this far past the trigger
    max_trigger_age: dict = _per_tf(3, 1)  # a trigger stays actionable this many candles


@dataclass(frozen=True)
class DailyBreakout:
    enabled: bool = True
    breakout_buffer_atr: float = 0.1
    min_rel_volume: float = 1.4  # vs the prior 20 candles, when volume exists
    min_zone_age: dict = _per_tf(10, 6)  # first reaction at least this many candles old
    require_htf_not_opposed: bool = True  # weekly structure/trend must not oppose
    watch_distance_atr: float = 1.0


@dataclass(frozen=True)
class TrendContinuation:
    enabled: bool = True
    pullback_lookback: dict = _per_tf(5, 3)  # the touch happened within N candles
    touch_tolerance_atr: float = 0.3  # low within this of EMA20 / EMA50 / support
    max_close_violation_atr: float = 0.5  # close may not end further beyond the level
    min_pullback_atr: float = 1.0  # retracement from the recent swing extreme
    watch_distance_atr: float = 0.5


@dataclass(frozen=True)
class ConsolidationBreakout:
    enabled: bool = True
    min_bars: dict = _per_tf(10, 6)
    max_bars: dict = _per_tf(40, 20)
    max_range_atr: float = 3.5  # range height <= this x ATR
    min_touches: int = 2  # candles reaching within edge_tolerance of each edge
    edge_tolerance_atr: float = 0.3
    breakout_buffer_atr: float = 0.1
    min_rel_volume: float = 1.3
    # "breakout_candle": beyond the breakout candle's opposite extreme (failed breakout),
    # never farther than the range midpoint; "midpoint"; or "opposite" side of the range.
    stop_mode: str = "breakout_candle"
    measured_move_multiples: tuple = (1.0, 2.0)


@dataclass(frozen=True)
class SRReversal:
    enabled: bool = True
    min_wick_to_body: float = 1.5
    min_wick_share: float = 0.45
    max_close_position: float = 0.4  # bearish: close in lower 40% (bullish: upper 40%)
    allow_engulfing: bool = True
    confirm_window: dict = _per_tf(2, 1)  # close beyond the reversal candle within N
    max_penetration_atr: float = 0.5  # deeper close through the zone = breakdown
    watch_distance_atr: float = 0.5


@dataclass(frozen=True)
class TrendReversal:
    enabled: bool = True
    lookback: dict = _per_tf(80, 40)  # where the prior trend must be found
    require_ema_shift: bool = True  # close beyond EMA20 and EMA20 sloping the new way
    ema_slope_bars: int = 5


@dataclass(frozen=True)
class SwingConfig:
    gates: SwingGates = field(default_factory=SwingGates)
    daily_breakout: DailyBreakout = field(default_factory=DailyBreakout)
    trend_continuation: TrendContinuation = field(default_factory=TrendContinuation)
    consolidation_breakout: ConsolidationBreakout = field(default_factory=ConsolidationBreakout)
    sr_reversal: SRReversal = field(default_factory=SRReversal)
    trend_reversal: TrendReversal = field(default_factory=TrendReversal)

    def to_dict(self) -> dict:
        return asdict(self)

    def with_overrides(self, overrides: dict) -> SwingConfig:
        changes = {}
        valid = {f.name for f in fields(self)}
        for section, values in overrides.items():
            if section not in valid:
                raise ValueError(f"Unknown swing config section {section!r}")
            current = getattr(self, section)
            allowed = {f.name for f in fields(current)}
            bad = set(values) - allowed
            if bad:
                raise ValueError(f"Unknown keys in {section}: {sorted(bad)}")
            vals = dict(values)
            if "measured_move_multiples" in vals:
                vals["measured_move_multiples"] = tuple(vals["measured_move_multiples"])
            for k, v in vals.items():  # per-timeframe dicts merge rather than replace
                if isinstance(getattr(current, k), dict):
                    vals[k] = {**getattr(current, k), **v}
            changes[section] = replace(current, **vals)
        return replace(self, **changes)


def load_swing_config() -> SwingConfig:
    path = os.environ.get("SWING_STRATEGY_CONFIG_PATH")
    default = Path(__file__).resolve().parents[2] / "swing_strategies.json"
    p = Path(path) if path else default
    cfg = SwingConfig()
    if p.is_file():
        cfg = cfg.with_overrides(json.loads(p.read_text()))
    return cfg
