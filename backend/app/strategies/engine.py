"""Runs every enabled strategy independently and turns results into Setup records.

Status rules (per ticker x strategy x timeframe x direction):
* Global gates (stale/unavailable/invalid data, insufficient liquidity, data gaps,
  abnormal opening gap) => NO_TRADE with those reasons; nothing may trigger.
* Otherwise, the most recent *accepted* trigger this session => TRIGGERED. A trigger is
  final once emitted: later candles only add an informational `outcome`; once that outcome
  reaches the stop or T2 the plan is no longer open and the status is NO_TRADE.
* Otherwise a developing pattern with an objective trigger and invalidation level while
  the session is open => WATCHING.
* Otherwise NO_TRADE with the strategy's reasons (including any rejected triggers).
Every status is then confirmed by the trade-planning engine (`app.planning`), which owns
entry, stop, targets, reward/risk and the no-trade rules; an opposite-direction trigger on
the same timeframe is added as a conflicting signal.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from ..data.base import Bar
from ..market.calendar import NY, MarketCalendar
from ..planning.plan import SIGNAL_NONE, PlanInput, build_plan
from .base import Strategy, plan_input, watch_signal
from .breakout_retest import BreakoutRetest
from .common import (LONG, NO_TRADE, SHORT, TRIGGERED, WATCHING, Setup, StrategyContext,
                     build_context, outcome_after)
from .config import PRIMARY_TIMEFRAMES, StrategyConfig
from .liquidity_sweep import LiquiditySweepReversal
from .opening_range import OpeningRangeBreakout
from .sr_rejection import SRRejection
from .trend_pullback import TrendPullback

STRATEGIES: list[Strategy] = [BreakoutRetest(), OpeningRangeBreakout(),
                              LiquiditySweepReversal(), TrendPullback(), SRRejection()]
STATUS_ORDER = {TRIGGERED: 0, WATCHING: 1, NO_TRADE: 2}
RESOLVED = ("stopped", "target1_then_stopped", "target2")


def _trigger_dict(t) -> dict:
    r = lambda x: None if x is None else round(x, 4)
    return {"time": t.time, "confirmed_at": t.confirmed_at, "entry": r(t.entry),
            "stop": r(t.stop), "target1": r(t.target1), "target2": r(t.target2),
            "risk_reward": t.risk_reward, "accepted": t.blocked is None, "blocked": t.blocked}


def _apply_plan(setup: Setup, plan: dict) -> None:
    """The plan is the single source of the setup's status and numbers."""
    setup.plan = plan
    setup.status = plan["status"]
    num = lambda k: plan[k]["price"] if plan.get(k) else None
    setup.entry, setup.stop = num("entry"), num("stop")
    setup.target1, setup.target2 = num("target1"), num("target2")
    setup.risk_reward = plan["risk_reward_t1"]
    setup.risk_per_share = plan["risk_per_share"]
    setup.targets_basis = [f"T{k} {plan[f'target{k}']['price']:.2f}: {plan[f'target{k}']['basis']}"
                           for k in (1, 2) if plan.get(f"target{k}")]


def evaluate_context(ctx: StrategyContext, strategies: list[Strategy] | None = None,
                     timeframes: list[str] | None = None) -> list[Setup]:
    out: list[tuple[Setup, PlanInput | None]] = []
    evaluated_at = ctx.as_of.astimezone(NY).isoformat()
    session_state = "open" if ctx.session_open else "closed"
    for strat in strategies or STRATEGIES:
        if not strat.enabled(ctx):
            continue
        for tf in timeframes or PRIMARY_TIMEFRAMES:
            tfd = ctx.tfs.get(tf)
            for direction in (LONG, SHORT):
                last_t = tfd.end[-1] if tfd and tfd.end else int(ctx.as_of.timestamp())
                setup = Setup(ctx.ticker, strat.id, strat.name, direction, tf, NO_TRADE,
                              ctx.htf_bias(last_t), evaluated_at, ctx.session.day.isoformat(),
                              session_state)
                out.append((setup, _setup_input(ctx, strat, tfd, setup, direction)))
    # Cross-setup contradiction: opposite directions triggering on the same ticker/timeframe.
    live = {(s.timeframe, s.direction) for s, inp in out
            if inp is not None and inp.signal == TRIGGERED and s.status == TRIGGERED}
    setups = []
    for setup, inp in out:
        if inp is not None:
            opp = SHORT if setup.direction == LONG else LONG
            if (setup.timeframe, opp) in live:
                inp = replace(inp, conflicts=inp.conflicts + [
                    f"an opposite {opp} setup also triggered on {setup.timeframe}"])
            provisional = setup.status
            plan = build_plan(inp, ctx.planning)
            resolved = setup.outcome and setup.outcome["state"] in RESOLVED
            if plan["status"] == TRIGGERED and resolved:
                # The trade already reached its stop or T2: no longer an open plan.
                msg = (f"Trigger at {datetime.fromtimestamp(setup.trigger_time, NY):%H:%M} ET "
                       f"already resolved ({setup.outcome['state'].replace('_', ' ')}); "
                       f"no longer an open plan.")
                plan = {**plan, "status": NO_TRADE, "reasons": [msg] + plan["reasons"],
                        "checks": plan["checks"] + [{"name": "Trade still open", "passed": False,
                                                     "detail": msg}]}
            _apply_plan(setup, plan)
            if plan["status"] != provisional:
                setup.reasons = plan["reasons"]
            if ctx.levels_note:
                setup.reasons.append(ctx.levels_note)
        setups.append(setup)
    setups.sort(key=lambda s: (STATUS_ORDER[s.status], -(s.confirmed_at or 0), s.ticker,
                               s.strategy, s.timeframe, s.direction))
    return setups


def _setup_input(ctx: StrategyContext, strat: Strategy, tfd, setup: Setup,
                 direction: str) -> PlanInput | None:
    """Pick what to plan for this strategy x timeframe x direction and fill the setup's
    descriptive fields. Status is provisional here; `build_plan` decides it."""
    if ctx.gate_reasons or tfd is None or not tfd.bars:
        reasons = list(ctx.gate_reasons) or [f"No {setup.timeframe} candles available."]
        setup.reasons = reasons
        i = len(tfd.bars) - 1 if tfd and tfd.bars else None
        if i is None:
            return None
        return plan_input(ctx, tfd, i, direction, SIGNAL_NONE, None, None, [], [], [],
                          notes=reasons, strategy_id=strat.id, strategy_name=strat.name)
    res = strat.evaluate(ctx, tfd, direction)
    setup.triggers_today = [_trigger_dict(t) for t in res.triggers]
    accepted = [t for t in res.triggers if t.blocked is None]
    if accepted:
        t = accepted[-1]
        i = next(k for k, b in enumerate(tfd.bars) if b.time == t.time)
        setup.status = TRIGGERED
        setup.htf_bias = ctx.htf_bias(t.confirmed_at)
        setup.confirmation = t.conditions
        setup.invalidation = t.invalidation
        setup.evidence = t.evidence
        setup.supporting_levels = t.levels
        setup.trigger_time, setup.confirmed_at = t.time, t.confirmed_at
        setup.outcome = outcome_after(tfd, i, direction, t.stop, t.target1, t.target2)
        setup.reasons = [f"All {strat.name.lower()} conditions met on the {setup.timeframe} "
                         f"candle closing {datetime.fromtimestamp(t.confirmed_at, NY):%H:%M} ET."]
        return replace(t.plan_input, strategy=strat.id, strategy_name=strat.name,
                       notes=list(setup.reasons))
    signal = watch_signal(res.watch) if ctx.session_open else SIGNAL_NONE
    i = len(tfd.bars) - 1
    if res.watch and signal == WATCHING:
        w = res.watch
        setup.status = WATCHING
        setup.reasons = list(w["reasons"])
        setup.confirmation = w["conditions"]
        setup.supporting_levels = w.get("levels", [])
        setup.invalidation = w.get("invalidation", [])
        return plan_input(ctx, tfd, i, direction, WATCHING, w["trigger"], w["stop_anchor"],
                          w["conditions"], w.get("levels", []), w.get("invalidation", []),
                          w.get("extra"), notes=setup.reasons, strategy_id=strat.id,
                          strategy_name=strat.name)
    reasons = list(res.reasons)
    if res.watch:
        lead = ("Session closed before confirmation: " if not ctx.session_open else
                "Developing, but no objective entry trigger and stop yet: ")
        reasons.insert(0, lead + res.watch["reasons"][0])
    rejected = [t for t in res.triggers if t.blocked]
    for t in rejected:
        reasons.append(f"Conditions met at {datetime.fromtimestamp(t.time, NY):%H:%M} "
                       f"but rejected: {t.blocked}")
    setup.reasons = reasons
    if rejected:
        # Show the most recent rejected plan so its calculations can be inspected.
        t = rejected[-1]
        setup.confirmation = t.conditions
        setup.supporting_levels = t.levels
        setup.invalidation = t.invalidation
        setup.evidence = t.evidence
        setup.plan = t.plan
        return replace(t.plan_input, strategy=strat.id, strategy_name=strat.name,
                       notes=reasons)
    return plan_input(ctx, tfd, i, direction, SIGNAL_NONE, None, None, [], [], [],
                      notes=reasons or ["No qualifying pattern."], strategy_id=strat.id,
                      strategy_name=strat.name)


def evaluate_symbol(ticker: str, frames: dict[str, list[Bar]], as_of: datetime,
                    cal: MarketCalendar, cfg: StrategyConfig,
                    data_problems: list[str] | None = None,
                    strategies: list[Strategy] | None = None,
                    planning=None, liquidity: dict | None = None, volume=None
                    ) -> tuple[StrategyContext, list[Setup]]:
    ctx = build_context(ticker, frames, as_of, cal, cfg, data_problems, planning, liquidity,
                        volume)
    return ctx, evaluate_context(ctx, strategies)
