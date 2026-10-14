"""Shared building blocks for swing strategies: plans, targets, acceptance rules."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..strategies.common import Condition
from .context import SwingContext, SwingTF, fmt_day

LONG, SHORT = "LONG", "SHORT"


def sgn(direction: str) -> int:
    return 1 if direction == LONG else -1


@dataclass
class SwingTrigger:
    """A completed-candle trigger. Immutable once emitted."""

    index: int
    time: int
    confirmed_at: int
    entry_trigger: float  # the level price had to close beyond
    entry: float  # trigger candle close
    entry_zone: tuple[float, float]
    stop: float
    target1: float
    target2: float
    risk_reward: float
    targets_basis: list[str]
    conditions: list[Condition]
    evidence: list[dict]
    levels: list[dict]
    invalidation: list[str]
    invalidation_price: float  # a close beyond this cancels the setup
    blocked: str | None = None


@dataclass
class SwingWatch:
    """A developing setup with a complete hypothetical plan."""

    entry_trigger: float
    entry_zone: tuple[float, float]
    stop: float
    target1: float
    target2: float
    risk_reward: float
    targets_basis: list[str]
    conditions: list[Condition]
    reasons: list[str]
    levels: list[dict]
    invalidation: list[str]
    plan_problem: str | None = None  # why a trigger at this plan would be rejected


@dataclass
class SwingResult:
    triggers: list[SwingTrigger] = field(default_factory=list)
    watch: SwingWatch | None = None
    reasons: list[str] = field(default_factory=list)


class SwingStrategy:
    id = ""
    name = ""
    config_key = ""

    def enabled(self, ctx: SwingContext) -> bool:
        return getattr(ctx.config, self.config_key).enabled

    def evaluate(self, ctx: SwingContext, d: SwingTF, direction: str, start: int) -> SwingResult:
        """Scan candles start..n-1 for completed-candle triggers; describe a developing
        setup (watch) on the latest candle."""
        raise NotImplementedError


def zone_ref(z, tf: str, label: str | None = None) -> dict:
    kinds = {p.kind for p in z.pivots}
    return {"label": label or f"{'Weekly' if tf == '1Week' else 'Daily'} zone "
                              f"({len(z.pivots)} reactions)",
            "type": "ZONE", "low": round(z.low, 4), "high": round(z.high, 4), "timeframe": tf,
            "strength": None, "reactions": len(z.pivots),
            "role_reversal": kinds == {"high", "low"}}


MAJOR_EXTREME_LOOKBACK = {"1Day": 250, "1Week": 52}  # ~one year


def target_candidates(ctx: SwingContext, d: SwingTF, i: int, direction: str, entry: float
                      ) -> list[tuple[float, str]]:
    """Significant levels beyond `entry` known at candle i: support/resistance zones (near
    edge, >= 2 reactions) on this timeframe and, for daily candles, weekly zones, plus the
    ~1-year extreme (highest high / lowest low of candles before i). Minor single swing
    points are not treated as targets - every small swing would cap T1 at ~1R."""
    zones = [(z, d.tf) for z in d.zones_at(i)]
    if d.tf == "1Day":
        zones += [(z, "1Week") for z in ctx.weekly_zones_for_daily(i)]
    out = []
    s = sgn(direction)
    for z, tf in zones:
        edge = z.low if direction == LONG else z.high
        if s * (edge - entry) > 0:
            out.append((edge, f"{'weekly' if tf == '1Week' else 'daily'} zone "
                              f"{z.low:.2f}-{z.high:.2f}"))
    look = MAJOR_EXTREME_LOOKBACK.get(d.tf, 250)
    prior = d.bars[max(0, i - look):i]
    if prior:
        if direction == LONG:
            b = max(prior, key=lambda x: x.high)
            px, word = b.high, "high"
        else:
            b = min(prior, key=lambda x: x.low)
            px, word = b.low, "low"
        if s * (px - entry) > 0:
            out.append((px, f"1-year {word} {fmt_day(b.time)}"))
    return out


def plan_targets(ctx: SwingContext, d: SwingTF, i: int, direction: str, entry: float,
                 stop: float, extra: list[tuple[float, str]] | None = None):
    """T1 = first candidate >= min_target_r R away; T2 = next >= t2_extra_r R beyond T1;
    measured R fallbacks otherwise. Returns (t1, t2, rr, basis)."""
    t = ctx.config.targets
    s = sgn(direction)
    risk = abs(entry - stop)
    cands = target_candidates(ctx, d, i, direction, entry) + list(extra or [])
    cands = sorted(((p, w) for p, w in cands if s * (p - entry) > 0),
                   key=lambda x: s * (x[0] - entry))
    t1 = next(((p, w) for p, w in cands if s * (p - entry) >= t.min_target_r * risk), None) \
        or (entry + s * t.fallback_t1_r * risk, f"measured {t.fallback_t1_r}R (no level)")
    t2 = next(((p, w) for p, w in cands if s * (p - t1[0]) >= t.t2_extra_r * risk), None) \
        or (entry + s * max(t.fallback_t2_r * risk, abs(t1[0] - entry) + t.t2_extra_r * risk),
            f"measured {t.fallback_t2_r}R (no further level)")
    rr = abs(t1[0] - entry) / risk if risk > 0 else 0.0
    return t1[0], t2[0], rr, [f"T1 {t1[0]:.2f}: {t1[1]}", f"T2 {t2[0]:.2f}: {t2[1]}"]


def entry_zone(ctx: SwingContext, d: SwingTF, i: int, trigger: float, direction: str):
    a = d.atr_before(i) * ctx.config.targets.entry_zone_atr
    return (trigger, trigger + a) if direction == LONG else (trigger - a, trigger)


def accept(ctx: SwingContext, d: SwingTF, i: int, direction: str, trigger_level: float,
           stop: float, conditions: list[Condition], evidence: list[dict], levels: list[dict],
           invalidation: list[str], invalidation_price: float,
           extra_targets: list[tuple[float, str]] | None = None) -> SwingTrigger:
    """Trigger on the close of candle i, with the shared rules:
    trigger candle not abnormal (range <= max_trigger_range_atr x ATR), close not more than
    max_extension_atr x ATR past the trigger level (no chasing), stop on the correct side and
    at most max_risk_atr x ATR away, reward/risk to T1 >= min_rr."""
    g = ctx.config.gates
    b = d.bars[i]
    atr = d.atr_before(i)
    entry = b.close
    t1, t2, rr, basis = plan_targets(ctx, d, i, direction, entry, stop, extra_targets)
    s = sgn(direction)
    ext = s * (entry - trigger_level) / atr if atr else 0.0
    conds = list(conditions)
    abnormal = b.high - b.low > g.max_trigger_range_atr * atr
    conds.append(Condition(f"Trigger candle range <= {g.max_trigger_range_atr} ATR", not abnormal,
                           True, f"range {b.high - b.low:.2f} vs ATR {atr:.2f}", b.time))
    conds.append(Condition(f"Close within {g.max_extension_atr} ATR of the trigger (no chasing)",
                           ext <= g.max_extension_atr, True,
                           f"{ext:.2f} ATR beyond {trigger_level:.2f}", b.time))
    risk_ok = s * (entry - stop) > 0
    risk_atr = abs(entry - stop) / atr if atr else 0.0
    conds.append(Condition(f"Stop distance <= {g.max_risk_atr} ATR", risk_atr <= g.max_risk_atr,
                           True, f"{abs(entry - stop):.2f} = {risk_atr:.2f} ATR", b.time))
    rr_ok = rr >= ctx.config.targets.min_rr - 1e-9
    conds.append(Condition(f"Reward/risk to T1 >= {ctx.config.targets.min_rr}", risk_ok and rr_ok,
                           True, f"{rr:.2f}:1 (entry {entry:.2f}, stop {stop:.2f}, T1 {t1:.2f})",
                           b.time))
    blocked = None
    if abnormal:
        blocked = f"abnormal trigger candle (range {b.high - b.low:.2f} > {g.max_trigger_range_atr} ATR)"
    elif ext > g.max_extension_atr:
        blocked = f"close {ext:.2f} ATR past the trigger level (chasing)"
    elif not risk_ok:
        blocked = "stop is not beyond the entry"
    elif risk_atr > g.max_risk_atr:
        blocked = f"stop {risk_atr:.2f} ATR away (> {g.max_risk_atr})"
    elif not rr_ok:
        blocked = f"reward/risk {rr:.2f} < {ctx.config.targets.min_rr}"
    return SwingTrigger(i, b.time, d.end[i], trigger_level, entry,
                        entry_zone(ctx, d, i, trigger_level, direction), stop, t1, t2, rr, basis,
                        conds, evidence, levels, invalidation, invalidation_price, blocked)


def watch_plan(ctx: SwingContext, d: SwingTF, direction: str, trigger: float, stop: float,
               conditions: list[Condition], reasons: list[str], levels: list[dict],
               invalidation: list[str], extra: list[tuple[float, str]] | None = None
               ) -> SwingWatch:
    """Hypothetical plan if the trigger fires now (levels known as of the latest candle)."""
    n = len(d.bars)
    t1, t2, rr, basis = plan_targets(ctx, d, n, direction, trigger, stop, extra)
    min_rr = ctx.config.targets.min_rr
    g = ctx.config.gates
    atr = d.atr_before(n)
    risk_atr = abs(trigger - stop) / atr if atr else 0.0
    conds = list(conditions) + [
        Condition(f"Stop distance <= {g.max_risk_atr} ATR", risk_atr <= g.max_risk_atr, True,
                  f"{abs(trigger - stop):.2f} = {risk_atr:.2f} ATR"),
        Condition(f"Reward/risk to T1 >= {min_rr} (at the trigger)", rr >= min_rr - 1e-9, True,
                  f"{rr:.2f}:1 (trigger {trigger:.2f}, stop {stop:.2f}, T1 {t1:.2f})")]
    problem = None
    if risk_atr > g.max_risk_atr:
        problem = f"the stop would be {risk_atr:.2f} ATR away (> {g.max_risk_atr})"
    elif rr < min_rr - 1e-9:
        problem = f"the plan's reward/risk {rr:.2f} is below {min_rr}"
    return SwingWatch(trigger, entry_zone(ctx, d, n, trigger, direction), stop, t1, t2, rr,
                      basis, conds, reasons, levels, invalidation, problem)
