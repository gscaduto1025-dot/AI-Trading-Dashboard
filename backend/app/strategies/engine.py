"""Runs every enabled strategy independently and turns results into Setup records.

Status rules (per ticker x strategy x timeframe x direction):
* Global gates (stale/unavailable/invalid data, insufficient liquidity, data gaps,
  abnormal opening gap) => NO_TRADE with those reasons; nothing may trigger.
* Otherwise, the most recent *accepted* trigger this session => TRIGGERED. A trigger is
  final once emitted: later candles only add an informational `outcome`.
* Otherwise a developing pattern while the session is open => WATCHING.
* Otherwise NO_TRADE with the strategy's reasons (including any rejected triggers).
"""

from __future__ import annotations

from datetime import datetime

from ..data.base import Bar
from ..market.calendar import NY, MarketCalendar
from .base import Strategy
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


def _trigger_dict(t) -> dict:
    return {"time": t.time, "confirmed_at": t.confirmed_at, "entry": round(t.entry, 4),
            "stop": round(t.stop, 4), "target1": round(t.target1, 4),
            "target2": round(t.target2, 4), "risk_reward": round(t.risk_reward, 2),
            "accepted": t.blocked is None, "blocked": t.blocked}


def evaluate_context(ctx: StrategyContext, strategies: list[Strategy] | None = None,
                     timeframes: list[str] | None = None) -> list[Setup]:
    out: list[Setup] = []
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
                if ctx.gate_reasons:
                    setup.reasons = list(ctx.gate_reasons)
                    out.append(setup)
                    continue
                if tfd is None:
                    setup.reasons = [f"No {tf} candles available."]
                    out.append(setup)
                    continue
                res = strat.evaluate(ctx, tfd, direction)
                setup.triggers_today = [_trigger_dict(t) for t in res.triggers]
                accepted = [t for t in res.triggers if t.blocked is None]
                if accepted:
                    t = accepted[-1]
                    i = next(k for k, b in enumerate(tfd.bars) if b.time == t.time)
                    setup.status = TRIGGERED
                    setup.htf_bias = ctx.htf_bias(t.confirmed_at)
                    setup.entry, setup.stop = t.entry, t.stop
                    setup.target1, setup.target2 = t.target1, t.target2
                    setup.risk_reward = t.risk_reward
                    setup.risk_per_share = abs(t.entry - t.stop)
                    setup.confirmation = t.conditions
                    setup.invalidation = t.invalidation
                    setup.evidence = t.evidence
                    setup.supporting_levels = t.levels
                    setup.targets_basis = t.targets_basis
                    setup.trigger_time, setup.confirmed_at = t.time, t.confirmed_at
                    setup.outcome = outcome_after(tfd, i, direction, t.stop, t.target1,
                                                  t.target2)
                    setup.reasons = [f"All {strat.name.lower()} conditions met on the "
                                     f"{tf} candle closing "
                                     f"{datetime.fromtimestamp(t.confirmed_at, NY):%H:%M} ET."]
                elif res.watch and ctx.session_open:
                    w = res.watch
                    setup.status = WATCHING
                    setup.reasons = w["reasons"]
                    setup.confirmation = w["conditions"]
                    setup.supporting_levels = w.get("levels", [])
                    setup.invalidation = w.get("invalidation", [])
                    setup.entry = w.get("entry")
                else:
                    setup.reasons = list(res.reasons)
                    if res.watch and not ctx.session_open:
                        setup.reasons.insert(0, "Session closed before confirmation: "
                                             + res.watch["reasons"][0])
                    for t in res.triggers:
                        if t.blocked:
                            setup.reasons.append(
                                f"Conditions met at {datetime.fromtimestamp(t.time, NY):%H:%M} "
                                f"but rejected: {t.blocked}.")
                            setup.confirmation = t.conditions
                            setup.supporting_levels = t.levels
                if ctx.levels_note:
                    setup.reasons.append(ctx.levels_note)
                out.append(setup)
    out.sort(key=lambda s: (STATUS_ORDER[s.status], -(s.confirmed_at or 0), s.ticker,
                            s.strategy, s.timeframe, s.direction))
    return out


def evaluate_symbol(ticker: str, frames: dict[str, list[Bar]], as_of: datetime,
                    cal: MarketCalendar, cfg: StrategyConfig,
                    data_problems: list[str] | None = None,
                    strategies: list[Strategy] | None = None) -> tuple[StrategyContext, list[Setup]]:
    ctx = build_context(ticker, frames, as_of, cal, cfg, data_problems)
    return ctx, evaluate_context(ctx, strategies)
