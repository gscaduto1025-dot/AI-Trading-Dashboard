"""Backtest assumptions. Every number is explicit, configurable per run, and reported with
the results. These are SIMULATION assumptions (how hypothetical orders fill); the signal
rules are the live strategy and planning engines, unchanged."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace

# Lookback windows: identical to the live service (Service.STRATEGY_FRAMES / SWING_FRAMES,
# asserted equal in tests) so indicators see the same history they would see live.
DAY_FRAMES = {"5Min": 600, "15Min": 400, "1Hour": 500, "1Day": 500, "1Week": 260}
SWING_FRAMES = {"1Day": 500, "1Week": 260, "4Hour": 200, "1Hour": 300}
EXEC_TF = {"day": "5Min", "swing": "1Day"}  # candles used to simulate fills and exits


def _per_mode(day, swing):
    return field(default_factory=lambda: {"day": day, "swing": swing})


@dataclass(frozen=True)
class BacktestConfig:
    # ---- execution (hypothetical fills)
    entry_delay_bars: int = 0          # extra execution candles to wait after the signal
    #   0 = market order at the OPEN of the first candle after the signal candle closes
    half_spread_bps: dict = _per_mode(2.0, 2.0)   # paid on every market order
    slippage_bps: dict = _per_mode(3.0, 5.0)      # adverse, on market/stop orders
    commission_per_share: float = 0.005           # per side (Alpaca is $0; conservative)
    tick: float = 0.01                 # a target limit fills only if price trades THROUGH it
    exit_policy: str = "t1"            # t1 | t2 | split (half at T1, rest at T2/stop)
    respect_entry_zone: bool = True    # skip fills outside the plan's entry zone (no chasing)
    max_hold_bars: dict = _per_mode(None, 20)  # day: flat at the session close; swing: N days
    one_position_per_setup: bool = True  # ticker x strategy x timeframe x direction
    risk_per_trade_pct: float = 1.0    # fixed-fractional equity curve (risk at the stop)
    # ---- validation
    in_sample_fraction: float = 0.7    # first 70 % of the period in-sample, rest out-of-sample
    walk_forward_folds: int = 4        # expanding-window folds over the period
    quality_thresholds: list = field(default_factory=lambda: [0, 50, 60, 70])
    min_trades_for_evidence: int = 100
    min_oos_trades_for_evidence: int = 30
    exhaustive: bool = False           # evaluate the engine at EVERY candle close (slow)

    def to_dict(self) -> dict:
        return asdict(self)

    def with_overrides(self, overrides: dict | None) -> BacktestConfig:
        if not overrides:
            return self
        valid = {f.name for f in fields(self)}
        bad = set(overrides) - valid
        if bad:
            raise ValueError(f"Unknown backtest settings: {sorted(bad)}")
        vals = dict(overrides)
        for k, v in vals.items():
            cur = getattr(self, k)
            if isinstance(cur, dict):
                if not isinstance(v, dict) or set(v) - set(cur):
                    raise ValueError(f"{k} must be an object with keys {sorted(cur)}")
                vals[k] = {**cur, **v}
        out = replace(self, **vals)
        if out.exit_policy not in ("t1", "t2", "split"):
            raise ValueError("exit_policy must be t1, t2 or split")
        if not 0.3 <= out.in_sample_fraction <= 0.9:
            raise ValueError("in_sample_fraction must be between 0.3 and 0.9")
        if out.entry_delay_bars < 0 or out.commission_per_share < 0 or out.tick <= 0:
            raise ValueError("entry_delay_bars, commission and tick must be non-negative")
        for m in ("day", "swing"):
            if out.half_spread_bps[m] < 0 or out.slippage_bps[m] < 0:
                raise ValueError("spread and slippage must be non-negative")
        if not 0 < out.risk_per_trade_pct <= 10:
            raise ValueError("risk_per_trade_pct must be in (0, 10]")
        if not 2 <= out.walk_forward_folds <= 12:
            raise ValueError("walk_forward_folds must be 2-12")
        return out
