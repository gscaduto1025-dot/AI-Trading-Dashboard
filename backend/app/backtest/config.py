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
    # Regulatory fees on the SELL side (long exit / short entry). Rates change; check the
    # current SEC Section 31 rate and FINRA TAF schedule before relying on exact figures.
    sec_fee_rate: float = 0.0000278               # x sale notional ($27.80 per $1M)
    finra_taf_per_share: float = 0.000166         # per share sold (per-trade cap ignored)
    short_borrow_bps_per_year: float = 100.0      # stock-loan fee on shorts (1 %/yr, GC-ish)
    # ---- portfolio simulation (position sizing)
    starting_capital: float = 100_000.0
    max_position_pct: float = 25.0     # notional cap per position, % of equity
    max_leverage: float = 1.0          # buying power = equity x this (1.0 = no margin)
    max_open_positions: int = 10
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
        if out.starting_capital < 1000 or not 0 < out.max_position_pct <= 100:
            raise ValueError("starting_capital must be >= 1000 and max_position_pct in (0, 100]")
        if not 0.5 <= out.max_leverage <= 4 or out.max_open_positions < 1:
            raise ValueError("max_leverage must be 0.5-4 and max_open_positions >= 1")
        if min(out.sec_fee_rate, out.finra_taf_per_share, out.short_borrow_bps_per_year) < 0:
            raise ValueError("fees must be non-negative")
        if not 2 <= out.walk_forward_folds <= 12:
            raise ValueError("walk_forward_folds must be 2-12")
        return out
