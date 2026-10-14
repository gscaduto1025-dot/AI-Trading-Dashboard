"""Shared building blocks for swing strategies: plans, targets, acceptance rules."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..planning.plan import (SIGNAL_NONE, TRIGGERED, WATCHING, PlanInput, PriceRef,
                             QualityInputs, build_plan)
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
    entry_zone: tuple[float, float] | None
    stop: float | None
    target1: float | None
    target2: float | None
    risk_reward: float | None
    targets_basis: list[str]
    conditions: list[Condition]
    evidence: list[dict]
    levels: list[dict]
    invalidation: list[str]
    invalidation_price: float  # a close beyond this cancels the setup
    blocked: str | None = None  # why the trade plan rejected it (None = accepted)
    plan_input: PlanInput | None = None
    plan: dict | None = None


@dataclass
class SwingWatch:
    """A developing setup: objective trigger and invalidation known, confirmation pending."""

    conditions: list[Condition]
    reasons: list[str]
    levels: list[dict]
    invalidation: list[str]
    plan_input: PlanInput
    plan: dict


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
        # Implementations pass strategy_id=self.id to accept()/watch_plan().
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


def target_candidates(ctx: SwingContext, d: SwingTF, i: int, direction: str) -> list[PriceRef]:
    """Confirmed levels known at candle i: support/resistance zones (near edge, >= 2
    reactions) on this timeframe and, for daily candles, weekly zones, plus the ~1-year
    extreme (highest high / lowest low of candles before i). Minor single swing points are
    not targets."""
    zones = [(z, d.tf) for z in d.zones_at(i)]
    if d.tf == "1Day":
        zones += [(z, "1Week") for z in ctx.weekly_zones_for_daily(i)]
    out = []
    for z, tf in zones:
        edge = z.low if direction == LONG else z.high
        n = len(z.pivots)
        out.append(PriceRef(edge, f"{'weekly' if tf == '1Week' else 'daily'} zone "
                                  f"{z.low:.2f}-{z.high:.2f} ({n} reactions), near edge {edge:.2f}",
                            "zone", tf, min(n, 5) * 20.0))
    look = MAJOR_EXTREME_LOOKBACK.get(d.tf, 250)
    prior = d.bars[max(0, i - look):i]
    if prior:
        b = max(prior, key=lambda x: x.high) if direction == LONG else \
            min(prior, key=lambda x: x.low)
        px = b.high if direction == LONG else b.low
        out.append(PriceRef(px, f"1-year {'high' if direction == LONG else 'low'} {px:.2f} "
                                f"({fmt_day(b.time)})", "swing", d.tf))
    return out


TREND_FOLLOWING = {"swing_breakout", "swing_trend_continuation", "swing_consolidation_breakout"}


def _tf_word(tf: str) -> str:
    return "Weekly" if tf == "1Week" else "Daily"


def quality_inputs(ctx: SwingContext, d: SwingTF, i: int, direction: str,
                   levels: list[dict]) -> QualityInputs:
    want = "bullish" if direction == LONG else "bearish"
    htf = ctx.htf(d.tf, i)
    hs = "aligned" if htf["label"] == want else "neutral" if htf["label"] == "neutral" \
        else "opposed"
    react = [l.get("reactions") for l in levels if l.get("reactions")]
    strength = min(max(react), 5) * 20.0 if react else None
    rv, rv_note = d.volume(i).quality_rv()
    st = d.structure_at(i + 1)
    trend = st.get("trend")
    ss = "unknown" if trend is None else "mixed" if trend == "mixed" else \
        "aligned" if trend == ("up" if direction == LONG else "down") else "opposed"
    return QualityInputs(hs, htf["detail"], strength,
                         f"setup zone with {max(react)} confirmed reactions" if react else "",
                         rv, rv_note,
                         ss, f"{st['label']}; EMA stack {d.ema_stack(i)}")


def conflicts_for(ctx: SwingContext, d: SwingTF, i: int, direction: str,
                  strategy_id: str) -> list[str]:
    if strategy_id not in TREND_FOLLOWING:
        return []  # reversal strategies trade against the prior trend by design
    want = "bullish" if direction == LONG else "bearish"
    opp = "bearish" if direction == LONG else "bullish"
    out = []
    htf = ctx.htf(d.tf, i)
    if htf["label"] == opp:
        out.append(f"higher timeframe is {opp} against a {want} trade ({htf['detail']})")
    sup = ctx.supporting(d.end[i])
    if sup.get("4Hour") == opp:
        out.append(f"4-hour trend is {opp} against a {want} trade")
    return out


def plan_input(ctx: SwingContext, d: SwingTF, i: int, direction: str, signal: str,
               trigger: PriceRef | None, anchor: PriceRef | None, conditions: list[Condition],
               levels: list[dict], invalidation: list[str], extra=None,
               blocked: str | None = None, notes: list[str] | None = None,
               strategy_id: str = "", strategy_name: str = "") -> PlanInput:
    b = d.bars[i]
    entry = None
    if signal == TRIGGERED:
        entry = PriceRef(b.close, f"{_tf_word(d.tf)} candle of {fmt_day(b.time)} closed "
                                  f"{b.close:.2f}", "close", d.tf)
    cands = target_candidates(ctx, d, min(i + 1, len(d.bars)) if signal == WATCHING else i,
                              direction)
    cands += [x if isinstance(x, PriceRef) else PriceRef(x[0], f"{x[1]} {x[0]:.2f}", "measured", d.tf)
              for x in (extra or []) if x is not None]
    return PlanInput(
        ticker=ctx.ticker, mode="swing", strategy=strategy_id, strategy_name=strategy_name,
        direction=direction, timeframe=d.tf, signal=signal,
        atr=d.atr_before(i + 1 if signal == WATCHING else i),
        atr_label=f"{_tf_word(d.tf).lower()} ATR(14)", entry_trigger=trigger, entry_price=entry,
        stop_anchor=anchor, candidates=cands, confirmation=list(conditions),
        invalidation=list(invalidation),
        quality=quality_inputs(ctx, d, i, direction, levels),
        conflicts=conflicts_for(ctx, d, i, direction, strategy_id),
        data_issues=list(ctx.gate_reasons), blocked=blocked, notes=list(notes or []),
        signal_time=b.time)


def accept(ctx: SwingContext, d: SwingTF, i: int, direction: str, trigger_level: float,
           anchor: float, conditions: list[Condition], evidence: list[dict], levels: list[dict],
           invalidation: list[str], invalidation_price: float,
           extra_targets: list[tuple[float, str]] | None = None, *, trigger_basis: str = "",
           anchor_basis: str = "", strategy_id: str = "") -> SwingTrigger:
    """Trigger on the close of candle i. Strategy entry rules checked here: the trigger
    candle is not abnormal (range <= max_trigger_range_atr x ATR) and the close is not more
    than max_extension_atr x ATR past the trigger (no chasing). Stop placement, risk,
    targets and reward/risk are decided by the trade-planning engine."""
    g = ctx.config.gates
    b = d.bars[i]
    atr = d.atr_before(i)
    s = sgn(direction)
    ext = s * (b.close - trigger_level) / atr if atr else 0.0
    conds = list(conditions)
    abnormal = b.high - b.low > g.max_trigger_range_atr * atr
    conds.append(Condition(f"Trigger candle range <= {g.max_trigger_range_atr} ATR", not abnormal,
                           True, f"range {b.high - b.low:.2f} vs ATR {atr:.2f}", b.time))
    conds.append(Condition(f"Close within {g.max_extension_atr} ATR of the trigger (no chasing)",
                           ext <= g.max_extension_atr, True,
                           f"{ext:.2f} ATR beyond {trigger_level:.2f}", b.time))
    pre = None
    if abnormal:
        pre = f"abnormal trigger candle (range {b.high - b.low:.2f} > {g.max_trigger_range_atr} ATR)"
    elif ext > g.max_extension_atr:
        pre = f"close {ext:.2f} ATR past the trigger level (chasing)"
    word = "above" if s > 0 else "below"
    trig = PriceRef(trigger_level, trigger_basis or f"{_tf_word(d.tf).lower()} close {word} "
                                                     f"{trigger_level:.2f}", "trigger", d.tf)
    anc = PriceRef(anchor, anchor_basis or f"structural invalidation {anchor:.2f}", "structure",
                   d.tf)
    inp = plan_input(ctx, d, i, direction, TRIGGERED, trig, anc, conds, levels, invalidation,
                     extra_targets, pre, strategy_id=strategy_id)
    plan = build_plan(inp, ctx.planning)
    blocked = None if plan["status"] == TRIGGERED else (plan["reasons"] or ["rejected"])[0]
    num = lambda k: plan[k]["price"] if plan.get(k) else None
    zone = (plan["entry_zone"]["low"], plan["entry_zone"]["high"]) if plan["entry_zone"] else None
    basis = [f"T{k} {plan[f'target{k}']['price']:.2f}: {plan[f'target{k}']['basis']}"
             for k in (1, 2) if plan.get(f"target{k}")]
    return SwingTrigger(i, b.time, d.end[i], trigger_level, b.close, zone, num("stop"),
                        num("target1"), num("target2"), plan["risk_reward_t1"], basis, conds,
                        evidence, levels, invalidation, invalidation_price, blocked, inp, plan)


def watch_plan(ctx: SwingContext, d: SwingTF, direction: str, trigger: float, anchor: float,
               conditions: list[Condition], reasons: list[str], levels: list[dict],
               invalidation: list[str], extra: list[tuple[float, str]] | None = None, *,
               trigger_basis: str = "", anchor_basis: str = "",
               strategy_id: str = "") -> SwingWatch:
    """Conditional plan if the trigger fires (levels known as of the latest candle)."""
    n = len(d.bars)
    word = "above" if direction == LONG else "below"
    trig = PriceRef(trigger, trigger_basis or f"{_tf_word(d.tf).lower()} close {word} "
                                               f"{trigger:.2f}", "trigger", d.tf)
    anc = PriceRef(anchor, anchor_basis or f"structural invalidation {anchor:.2f}", "structure",
                   d.tf)
    inp = plan_input(ctx, d, n - 1, direction, WATCHING, trig, anc, conditions, levels,
                     invalidation, extra, notes=reasons, strategy_id=strategy_id)
    return SwingWatch(list(conditions), list(reasons), levels, list(invalidation), inp,
                      build_plan(inp, ctx.planning))


__all__ = ["LONG", "SHORT", "SIGNAL_NONE", "SwingTrigger", "SwingWatch", "SwingResult",
           "SwingStrategy", "accept", "watch_plan", "plan_input", "zone_ref", "sgn"]
