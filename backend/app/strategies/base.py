"""Strategy interface and the bridge to the trade-planning engine.

Strategies detect patterns and report objective facts: the trigger level a completed
candle closed beyond, the structural level that invalidates the idea (stop anchor), the
supporting levels and the confirmation conditions. Entry, stop, targets, reward/risk and
the final status come from `app.planning.build_plan`, shared with the swing engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..planning.plan import (SIGNAL_NONE, TRIGGERED, WATCHING, PlanInput, PriceRef,
                             QualityInputs, build_plan)
from .common import (LONG, Condition, StrategyContext, TFData, TriggerEvent, fmt,
                     level_target_candidates)

# Strategies that trade against the prevailing trend by design: an opposing higher
# timeframe is scored as weaker evidence but is not counted as a contradiction.
REVERSAL_STRATEGIES = {"liquidity_sweep_reversal", "sr_rejection"}


@dataclass
class StrategyResult:
    triggers: list[TriggerEvent] = field(default_factory=list)  # accepted and blocked
    # watch: {"time", "reasons", "conditions", "levels", "invalidation",
    #         "trigger": PriceRef | None, "stop_anchor": PriceRef | None, "extra": [...]}
    watch: dict | None = None
    reasons: list[str] = field(default_factory=list)  # why there is no trade


class Strategy:
    id: str = ""
    name: str = ""
    config_key: str = ""

    def enabled(self, ctx: StrategyContext) -> bool:
        return getattr(ctx.config, self.config_key).enabled

    def evaluate(self, ctx: StrategyContext, tfd: TFData, direction: str) -> StrategyResult:
        raise NotImplementedError


def sgn(direction: str) -> int:
    return 1 if direction == LONG else -1


def ref(price: float, basis: str, source: str = "", timeframe: str | None = None,
        strength: float | None = None) -> PriceRef:
    return PriceRef(float(price), basis, source, timeframe, strength)


def _extras(extra) -> list[PriceRef]:
    out = []
    for x in extra or []:
        if isinstance(x, PriceRef):
            out.append(x)
        else:
            p, label = x
            if p is not None:
                out.append(PriceRef(float(p), f"{label} {p:.2f}", "measured"))
    return out


def quality_inputs(ctx: StrategyContext, tfd: TFData, i: int, direction: str,
                   levels: list[dict]) -> QualityInputs:
    """Evidence for the setup-quality score, read at candle i (causal)."""
    s = sgn(direction)
    want = "bullish" if direction == LONG else "bearish"
    htf = ctx.htf_bias(tfd.end[i])
    htf_state = "aligned" if htf["label"] == want else "neutral" if htf["label"] == "neutral" \
        else "opposed"
    strengths = [l["strength"] for l in levels if l.get("strength") is not None]
    rv, rv_note = tfd.volume(i).quality_rv()
    b = tfd.bars[i]
    e9, e20, vw = tfd.ema9[i], tfd.ema20[i], tfd.vwap[i]
    if None in (e9, e20, vw):
        structure, detail = "unknown", "EMA/VWAP not available yet"
    else:
        votes = [s * (e9 - e20) > 0, s * (b.close - vw) > 0]
        structure = "aligned" if all(votes) else "opposed" if not any(votes) else "mixed"
        detail = f"EMA9 {e9:.2f} vs EMA20 {e20:.2f}; close {b.close:.2f} vs VWAP {vw:.2f}"
    return QualityInputs(
        htf_state, htf["detail"], max(strengths) if strengths else None,
        f"strongest supporting level {max(strengths):.0f}/100" if strengths else "",
        rv, rv_note,
        structure, detail)


def conflicts_for(ctx: StrategyContext, tfd: TFData, i: int, direction: str,
                  strategy_id: str) -> list[str]:
    """Signals that contradict a trend-following trade (reversal strategies exempt)."""
    if strategy_id in REVERSAL_STRATEGIES:
        return []
    want = "bullish" if direction == LONG else "bearish"
    opp = "bearish" if direction == LONG else "bullish"
    htf = ctx.htf_bias(tfd.end[i])
    out = []
    if htf["daily"] == opp:
        out.append(f"daily trend is {opp} against a {want} trade")
    if htf["hourly"] == opp:
        out.append(f"1-hour trend is {opp} against a {want} trade")
    return out


def plan_input(ctx: StrategyContext, tfd: TFData, i: int, direction: str, signal: str,
               trigger: PriceRef | None, stop_anchor: PriceRef | None,
               conditions: list[Condition], levels: list[dict], invalidation: list[str],
               extra=None, blocked: str | None = None, notes: list[str] | None = None,
               strategy_id: str = "", strategy_name: str = "") -> PlanInput:
    b = tfd.bars[i]
    entry = None
    if signal == TRIGGERED:
        entry = PriceRef(b.close, f"{tfd.tf} confirmation candle closed {b.close:.2f} at "
                                  f"{fmt(tfd.end[i])} ET", "close", tfd.tf)
    return PlanInput(
        ticker=ctx.ticker, mode="day", strategy=strategy_id, strategy_name=strategy_name,
        direction=direction, timeframe=tfd.tf, signal=signal,
        atr=tfd.atr_before(i), atr_label=f"{tfd.tf} ATR(14)", entry_trigger=trigger,
        entry_price=entry, stop_anchor=stop_anchor,
        candidates=level_target_candidates(ctx, direction) + _extras(extra),
        confirmation=list(conditions), invalidation=list(invalidation),
        quality=quality_inputs(ctx, tfd, i, direction, levels),
        conflicts=conflicts_for(ctx, tfd, i, direction, strategy_id),
        data_issues=list(ctx.gate_reasons), blocked=blocked, notes=list(notes or []),
        signal_time=b.time)


def accept(ctx: StrategyContext, tfd: TFData, i: int, direction: str, trigger: PriceRef,
           stop_anchor: PriceRef, conditions: list[Condition], evidence: list[dict],
           levels: list[dict], invalidation: list[str], extra_targets=None,
           strategy_id: str = "") -> TriggerEvent:
    """Build the trigger for completed candle i and its trade plan. `blocked` says why the
    plan rejected it (entry window / abnormal candle, invalid stop, zero risk, stop too
    wide, reward/risk below the minimum, contradictory signals), or None if accepted."""
    b = tfd.bars[i]
    inp = plan_input(ctx, tfd, i, direction, TRIGGERED, trigger, stop_anchor, conditions,
                     levels, invalidation, extra_targets, ctx.entry_block(tfd, i),
                     strategy_id=strategy_id)
    plan = build_plan(inp, ctx.planning)
    blocked = None if plan["status"] == TRIGGERED else (plan["reasons"] or ["rejected"])[0]
    stop = plan["stop"]["price"] if plan["stop"] else \
        stop_anchor.price - sgn(direction) * ctx.planning.stop_buffer_atr["day"] * inp.atr
    t1 = plan["target1"]["price"] if plan["target1"] else None
    t2 = plan["target2"]["price"] if plan["target2"] else None
    basis = [f"T{k} {t['price']:.2f}: {t['basis']}"
             for k, t in enumerate((plan["target1"], plan["target2"]), 1) if t]
    return TriggerEvent(b.time, tfd.end[i], b.close, stop, t1, t2, plan["risk_reward_t1"], basis,
                        list(conditions), evidence, levels, invalidation, blocked, inp, plan)


def watch_signal(watch: dict | None) -> str:
    """A developing pattern with an objective trigger and invalidation is WATCHING;
    without them there is not enough information for a plan."""
    if watch and watch.get("trigger") is not None and watch.get("stop_anchor") is not None:
        return WATCHING
    return SIGNAL_NONE
