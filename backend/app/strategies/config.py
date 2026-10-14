"""Explicit, configurable strategy definitions.

Every threshold a strategy uses lives here. Defaults can be overridden with a JSON file
(STRATEGY_CONFIG_PATH, or backend/strategies.json if present) shaped like `to_dict()`:
{"gates": {...}, "breakout_retest": {...}, ...}. Unknown keys are rejected.
Entry, stop buffer, targets and the minimum reward/risk are trade-planning rules shared
with the swing engine: see app/planning/config.py (backend/planning.json).
ATR always means Wilder's 14-candle ATR on the strategy's own timeframe at the candle
*before* the one being judged (so a candle never sizes its own buffer).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

DAY_TRADING_UNIVERSE = ["SPY", "QQQ", "NVDA", "TSLA", "AAPL", "AMD", "MSFT"]
PRIMARY_TIMEFRAMES = ["5Min", "15Min"]
CONTEXT_TIMEFRAMES = ["1Hour", "1Day"]


@dataclass(frozen=True)
class Gates:
    """Conditions under which no strategy may trigger."""

    min_avg_dollar_volume: float = 20_000_000  # 20-session average daily close x volume
    max_missing_bar_ratio: float = 0.2  # share of empty candle slots so far this session
    max_bar_range_atr: float = 4.0  # a candle wider than this x ATR is abnormal (spike/halt)
    halt_gap_slots: int = 3  # >= this many consecutive empty slots: possible halt
    max_open_gap_daily_atr: float = 3.0  # session open gap vs prior close, in daily ATRs
    entry_start_minutes: int = 5  # no triggers on the first candle(s) after 09:30
    stop_entries_minutes_before_close: int = 30  # early-close days honoured
    min_session_bars: int = 2


@dataclass(frozen=True)
class BreakoutRetest:
    enabled: bool = True
    breakout_buffer_atr: float = 0.1  # close beyond zone edge by this much
    min_breakout_rel_volume: float = 1.5  # when volume is available
    retest_window_bars: int = 12  # retest must come within N candles of the breakout
    retest_tolerance_atr: float = 0.15  # wick may reach this close to / into the zone edge
    confirm_window_bars: int = 3  # close above the retest candle's high within N candles
    reference_zone_half_width_atr: float = 0.1  # PDH/PDL/PWH/PWL become +/- this zones


@dataclass(frozen=True)
class OpeningRangeBreakout:
    enabled: bool = True
    range_minutes: int = 15  # 15 or 30
    breakout_buffer_atr: float = 0.05
    min_rel_volume: float = 1.2  # breakout candle vs same time of day, prior sessions
    latest_breakout_time: str = "11:30"  # ET; later breakouts are ignored
    min_range_daily_atr: float = 0.1  # range narrower than this x daily ATR: no signal
    max_range_daily_atr: float = 1.2  # wider: abnormal / too much risk
    stop_mode: str = "midpoint"  # "midpoint" or "opposite" side of the range
    measured_move_multiples: tuple = (1.0, 2.0)  # range-multiple target candidates


@dataclass(frozen=True)
class LiquiditySweep:
    enabled: bool = True
    min_penetration_atr: float = 0.05  # must trade beyond the level by at least this
    max_penetration_atr: float = 1.0  # deeper is a breakout, not a sweep
    return_window_bars: int = 3  # close back inside within N candles of the sweep
    confirm_close_position: float = 0.5  # reversal close in the lower (short) / upper half
    watch_distance_atr: float = 0.3
    include_intraday_swings: bool = True  # swings confirmed earlier in the session
    min_level_age_bars: int = 6  # intraday swings must be this many candles old when swept


@dataclass(frozen=True)
class TrendPullback:
    enabled: bool = True
    require_htf_alignment: bool = True
    touch_tolerance_atr: float = 0.1  # low within this of VWAP / EMA20 / support
    max_violation_atr: float = 0.25  # close may not go beyond the level by more than this
    min_pullback_bars: int = 2  # consecutive lower highs (long) before the touch
    touch_lookback_bars: int = 3  # the touch happened within the last N candles
    watch_distance_atr: float = 0.3


@dataclass(frozen=True)
class SRRejection:
    enabled: bool = True
    min_wick_to_body: float = 2.0
    min_wick_share: float = 0.5  # rejecting wick >= 50% of the candle range
    max_close_position: float = 0.4  # short: close in lower 40% (long: upper 40%)
    allow_engulfing: bool = True
    confirm_window_bars: int = 2  # close beyond the rejection candle's extreme within N
    min_zone_strength: int = 35  # key-level strength score


@dataclass(frozen=True)
class StrategyConfig:
    gates: Gates = field(default_factory=Gates)
    breakout_retest: BreakoutRetest = field(default_factory=BreakoutRetest)
    opening_range_breakout: OpeningRangeBreakout = field(default_factory=OpeningRangeBreakout)
    liquidity_sweep: LiquiditySweep = field(default_factory=LiquiditySweep)
    trend_pullback: TrendPullback = field(default_factory=TrendPullback)
    sr_rejection: SRRejection = field(default_factory=SRRejection)

    def to_dict(self) -> dict:
        return asdict(self)

    def with_overrides(self, overrides: dict) -> StrategyConfig:
        changes = {}
        valid = {f.name: f for f in fields(self)}
        for section, values in overrides.items():
            if section not in valid:
                raise ValueError(f"Unknown strategy config section {section!r}")
            current = getattr(self, section)
            allowed = {f.name for f in fields(current)}
            bad = set(values) - allowed
            if bad:
                raise ValueError(f"Unknown keys in {section}: {sorted(bad)}")
            if "measured_move_multiples" in values:
                values = dict(values, measured_move_multiples=tuple(values["measured_move_multiples"]))
            changes[section] = replace(current, **values)
        return replace(self, **changes)


def load_strategy_config() -> StrategyConfig:
    path = os.environ.get("STRATEGY_CONFIG_PATH")
    default = Path(__file__).resolve().parents[2] / "strategies.json"
    p = Path(path) if path else default
    cfg = StrategyConfig()
    if p.is_file():
        cfg = cfg.with_overrides(json.loads(p.read_text()))
    return cfg
