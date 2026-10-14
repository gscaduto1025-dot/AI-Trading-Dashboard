"""Strategy interface and the shared trigger-acceptance rule."""

from __future__ import annotations

from dataclasses import dataclass, field

from .common import (LONG, Condition, StrategyContext, TFData, TriggerEvent, compute_targets,
                     fmt)


@dataclass
class StrategyResult:
    triggers: list[TriggerEvent] = field(default_factory=list)  # accepted and blocked
    watch: dict | None = None  # {"reasons", "conditions", "levels", "entry"?, "stop"?}
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


def accept(ctx: StrategyContext, tfd: TFData, i: int, direction: str, entry: float,
           stop: float, conditions: list[Condition], evidence: list[dict], levels: list[dict],
           invalidation: list[str], extra_targets: list[tuple[float, str]] | None = None
           ) -> TriggerEvent:
    """Build the trigger for candle i; set `blocked` if a shared rule rejects it.

    Shared rules: the candle must be inside the entry window and not abnormal, the stop
    must be on the correct side of the entry, and reward/risk to T1 >= targets.min_rr.
    """
    t1, t2, rr, basis = compute_targets(ctx, direction, entry, stop, extra_targets)
    rr_ok = rr >= ctx.config.targets.min_rr - 1e-9  # tolerate float noise on exact multiples
    b = tfd.bars[i]
    block = ctx.entry_block(tfd, i)
    risk_ok = (entry - stop) * sgn(direction) > 0
    conds = list(conditions)
    conds.append(Condition("Candle inside entry window and not abnormal", block is None, True,
                           block or f"{fmt(b.time)} candle accepted", b.time))
    conds.append(Condition(f"Reward/risk to T1 >= {ctx.config.targets.min_rr}",
                           risk_ok and rr_ok, True,
                           f"{rr:.2f}:1 (entry {entry:.2f}, stop {stop:.2f}, T1 {t1:.2f})", b.time))
    blocked = None
    if block:
        blocked = block
    elif not risk_ok:
        blocked = "stop is not beyond the entry"
    elif not rr_ok:
        blocked = f"reward/risk {rr:.2f} < {ctx.config.targets.min_rr} (nearest target too close)"
    return TriggerEvent(b.time, tfd.end[i], entry, stop, t1, t2, rr, basis, conds, evidence,
                        levels, invalidation, blocked)
