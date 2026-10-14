"""Runs every enabled swing strategy independently on daily and weekly candles.

Status rules (per ticker x strategy x timeframe x direction):
* Gates (unavailable/stale/invalid data, < 60 daily candles, insufficient liquidity)
  => NO_TRADE with those reasons; nothing may trigger.
* The most recent *accepted* trigger => TRIGGERED while it is fresh (at most
  max_trigger_age candles old on its timeframe) and still live: no close beyond its
  invalidation price and neither the stop nor T2 reached since. Triggers are final once
  emitted; later candles only add the informational `outcome`.
* Otherwise a developing pattern with an objective trigger and invalidation => WATCHING.
* Otherwise NO_TRADE with the strategy's reasons (incl. rejected or expired triggers).
Every status is confirmed by the trade-planning engine (`app.planning`), which owns entry,
stop, targets, reward/risk, the no-trade rules and the setup-quality score; an
opposite-direction trigger on the same ticker/timeframe is added as a conflicting signal.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from ..data.base import Bar
from ..market.calendar import NY, MarketCalendar
from ..strategies.common import NO_TRADE, TRIGGERED, WATCHING, outcome_after, signal_id
from ..planning.plan import SIGNAL_NONE, PlanInput, build_plan
from .base import LONG, SHORT, SwingStrategy, plan_input, sgn
from .config import SWING_PRIMARY_TIMEFRAMES, SwingConfig
from .consolidation_breakout import ConsolidationBreakout
from .context import SwingContext, build_swing_context, fmt_day
from .daily_breakout import DailyBreakout
from .sr_reversal import SRReversal
from .trend_continuation import TrendContinuation
from .trend_reversal import TrendReversal

SWING_STRATEGIES: list[SwingStrategy] = [DailyBreakout(), TrendContinuation(),
                                         ConsolidationBreakout(), SRReversal(), TrendReversal()]
STATUS_ORDER = {TRIGGERED: 0, WATCHING: 1, NO_TRADE: 2}
LIVE_SCAN_CANDLES = 60  # triggers older than this are irrelevant to the current status
MIN_SCAN_INDEX = 60
SORT_KEYS = ("quality", "ticker", "strategy", "direction", "risk_reward", "distance")


def _r(x, nd=4):
    return None if x is None else round(float(x), nd)


def _key_levels_near(ctx: SwingContext, tf: str, price: float | None) -> list[dict]:
    """Nearest three key levels on each side of price from the swing key-level report."""
    if price is None:
        return []
    lv = [l for l in ctx.key_levels if l["timeframe"] == tf and l.get("valid", True)]
    above = sorted((l for l in lv if l["low"] > price), key=lambda l: l["low"])[:3]
    below = sorted((l for l in lv if l["high"] < price), key=lambda l: -l["high"])[:3]
    inside = [l for l in lv if l["low"] <= price <= l["high"]][:2]
    return [{"label": l["label"], "type": l["type"], "low": l["low"], "high": l["high"],
             "timeframe": l["timeframe"], "strength": l["strength"]["score"],
             "position": "above" if l in above else "below" if l in below else "at price"}
            for l in inside + above + below]


def _invalidated(d, i: int, direction: str, price: float) -> int | None:
    s = sgn(direction)
    for j in range(i + 1, len(d.bars)):
        if s * (d.bars[j].close - price) < 0:
            return j
    return None


def _trigger_dict(t) -> dict:
    return {"time": t.time, "confirmed_at": t.confirmed_at, "entry_trigger": _r(t.entry_trigger),
            "entry": _r(t.entry), "stop": _r(t.stop), "target1": _r(t.target1),
            "target2": _r(t.target2), "risk_reward": _r(t.risk_reward, 2),
            "accepted": t.blocked is None, "blocked": t.blocked}


def evaluate_swing_context(ctx: SwingContext, strategies: list[SwingStrategy] | None = None,
                           timeframes: list[str] | None = None, full_history: bool = False,
                           is_demo: bool = False) -> list[dict]:
    rows: list[tuple[dict, PlanInput | None, object]] = []
    evaluated_at = ctx.as_of.astimezone(NY).isoformat()
    for strat in strategies or SWING_STRATEGIES:
        if not strat.enabled(ctx):
            continue
        for tf in timeframes or SWING_PRIMARY_TIMEFRAMES:
            d = ctx.tfs.get(tf)
            for direction in (LONG, SHORT):
                rows.append(_evaluate_one(ctx, strat, tf, d, direction, full_history,
                                          evaluated_at, is_demo))
    live = {(st["timeframe"], st["direction"]) for st, inp, _ in rows
            if inp is not None and st["status"] == TRIGGERED}
    out = []
    for setup, inp, d in rows:
        if inp is not None:
            opp = SHORT if setup["direction"] == LONG else LONG
            if (setup["timeframe"], opp) in live:
                inp = replace(inp, conflicts=inp.conflicts + [
                    f"an opposite {opp} setup also triggered on {setup['timeframe']}"])
            _apply_plan(setup, build_plan(inp, ctx.planning))
        if setup["quality"] is None:
            setup["quality"] = {"score": 0.0, "components": [], "conflict_penalty": 0,
                                "conflicts": [], "note": "No plan: no candles on this timeframe."}
        if ctx.levels_note:
            setup["reasons"].append(ctx.levels_note)
        out.append(_finish(setup, ctx, d))
    return sort_setups(out, "quality")


def _apply_plan(setup: dict, plan: dict) -> None:
    provisional = setup["status"]
    num = lambda k: plan[k]["price"] if plan.get(k) else None
    setup.update({
        "plan": plan, "status": plan["status"], "quality": plan["quality"],
        "entry_trigger": num("entry_trigger"),
        "entry_zone": [plan["entry_zone"]["low"], plan["entry_zone"]["high"]]
        if plan["entry_zone"] else None,
        "entry": num("entry"), "stop": num("stop"), "target1": num("target1"),
        "target2": num("target2"), "risk_reward": plan["risk_reward_t1"],
        "risk_per_share": plan["risk_per_share"],
        "targets_basis": [f"T{k} {plan[f'target{k}']['price']:.2f}: {plan[f'target{k}']['basis']}"
                          for k in (1, 2) if plan.get(f"target{k}")]})
    if plan["status"] != provisional:
        setup["reasons"] = list(plan["reasons"])


def _evaluate_one(ctx, strat, tf, d, direction, full_history, evaluated_at, is_demo):
    """Returns (setup dict with descriptive fields, PlanInput to plan, timeframe data)."""
    n = len(d.bars) if d else 0
    last = d.bars[-1] if n else None
    htf = ctx.htf(tf, n - 1) if n > 1 else {"label": "neutral", "detail": "no data"}
    setup = {
        "id": None, "ticker": ctx.ticker, "mode": "swing", "strategy": strat.id,
        "strategy_name": strat.name, "direction": direction, "timeframe": tf,
        "status": NO_TRADE, "quality": None, "plan": None,
        "market_structure": {}, "daily_levels": [], "weekly_levels": [],
        "entry_trigger": None, "entry_zone": None, "entry": None, "stop": None,
        "target1": None, "target2": None, "risk_reward": None, "risk_per_share": None,
        "distance_to_trigger_pct": None, "distance_to_trigger_atr": None,
        "confirmation": [], "invalidation": [], "evidence": [], "supporting_levels": [],
        "reasons": [], "targets_basis": [], "trigger_time": None, "confirmed_at": None,
        "outcome": None, "last_trigger": None, "triggers": [],
        "last_close": _r(last.close) if last else None,
        "last_candle": fmt_day(last.time) if last else None, "htf_bias": htf["label"],
        "evaluated_at": evaluated_at, "is_demo": is_demo,
    }
    if d is not None and n:
        st = d.structure_at(n)
        setup["market_structure"] = {
            "timeframe": tf, "structure": st["label"], "swing_highs": st["highs"],
            "swing_lows": st["lows"], "ema_stack": d.ema_stack(n - 1),
            "ema20": _r(d.ema20[-1]), "ema50": _r(d.ema50[-1]), "ema200": _r(d.ema200[-1]),
            "higher_timeframe": htf["label"], "higher_timeframe_detail": htf["detail"],
            "supporting": ctx.supporting(d.end[-1])}
    setup["daily_levels"] = _key_levels_near(ctx, "1Day", setup["last_close"])
    setup["weekly_levels"] = _key_levels_near(ctx, "1Week", setup["last_close"])

    def none_input(reasons):
        setup["reasons"] = list(reasons)
        if d is None or not n:
            return setup, None, d
        return setup, plan_input(ctx, d, n - 1, direction, SIGNAL_NONE, None, None, [], [], [],
                                 notes=reasons, strategy_id=strat.id,
                                 strategy_name=strat.name), d

    if ctx.gate_reasons:
        return none_input(ctx.gate_reasons)
    if d is None or n < MIN_SCAN_INDEX + 1:
        return none_input([f"Insufficient candle history: {n} completed {tf} candles "
                           f"(need {MIN_SCAN_INDEX + 1}) for swing analysis."])
    start = MIN_SCAN_INDEX if full_history else max(MIN_SCAN_INDEX, n - LIVE_SCAN_CANDLES)
    res = strat.evaluate(ctx, d, direction, start)
    setup["triggers"] = [_trigger_dict(t) for t in res.triggers]
    accepted = [t for t in res.triggers if t.blocked is None]
    max_age = ctx.config.gates.max_trigger_age[tf]
    stale_note = None
    if accepted:
        t = accepted[-1]
        age = n - 1 - t.index
        outcome = outcome_after(d, t.index, direction, t.stop, t.target1, t.target2)
        inv = _invalidated(d, t.index, direction, t.invalidation_price)
        if outcome["state"] in ("stopped", "target1_then_stopped", "target2"):
            stale_note = f"Last trigger {fmt_day(t.time)} already resolved ({outcome['state']})."
            setup["last_trigger"] = {"time": t.time, "outcome": outcome}  # for the alert engine
        elif inv is not None:
            stale_note = (f"Last trigger {fmt_day(t.time)} was invalidated by the close on "
                          f"{fmt_day(d.bars[inv].time)}.")
        elif age >= max_age:
            stale_note = (f"Last trigger {fmt_day(t.time)} is {age} candle(s) old; triggers "
                          f"stay actionable for {max_age} {tf} candle(s).")
        else:
            reasons = [f"All {strat.name.lower()} conditions met on the {tf} candle of "
                       f"{fmt_day(t.time)} (completed close)."]
            setup.update({
                "status": TRIGGERED, "confirmation": [c.to_dict() for c in t.conditions],
                "invalidation": t.invalidation, "evidence": t.evidence,
                "supporting_levels": t.levels, "trigger_time": t.time,
                "confirmed_at": t.confirmed_at, "outcome": outcome,
                "id": signal_id(ctx.ticker, strat.id, tf, direction, "swing", t.time),
                "reasons": reasons})
            return setup, replace(t.plan_input, strategy=strat.id, strategy_name=strat.name,
                                  notes=reasons), d
    recent_rejected = [t for t in res.triggers if t.blocked and n - 1 - t.index < max_age]
    if res.watch:
        w = res.watch
        reasons = list(w.reasons)
        if stale_note:
            reasons.append(stale_note)
        reasons += [r for r in res.reasons if r not in reasons]
        for t in recent_rejected[-1:]:
            reasons.append(f"Conditions met on {fmt_day(t.time)} but rejected: {t.blocked}")
        setup.update({"status": WATCHING,
                      "confirmation": [c.to_dict() for c in w.conditions],
                      "invalidation": w.invalidation, "supporting_levels": w.levels,
                      "reasons": reasons})
        return setup, replace(w.plan_input, strategy=strat.id, strategy_name=strat.name,
                              notes=reasons), d
    reasons = list(res.reasons) or [
        f"No accepted {strat.name.lower()} trigger in the last {max_age} {tf} candle(s)."]
    if stale_note:
        reasons.insert(0, stale_note)
    for t in recent_rejected[-1:]:
        reasons.append(f"Conditions met on {fmt_day(t.time)} but rejected: {t.blocked}")
        setup.update({"confirmation": [c.to_dict() for c in t.conditions],
                      "supporting_levels": t.levels, "invalidation": t.invalidation,
                      "evidence": t.evidence, "reasons": reasons})
        # Re-plan the rejected trigger so its calculations can be inspected (NO_TRADE).
        return setup, replace(t.plan_input, strategy=strat.id, strategy_name=strat.name,
                              notes=reasons), d
    return none_input(reasons)


def _finish(setup: dict, ctx: SwingContext, d) -> dict:
    if setup["id"] is None:
        setup["id"] = signal_id(ctx.ticker, setup["strategy"], setup["timeframe"],
                                setup["direction"], "swing", 0)
    trig, close = setup["entry_trigger"], setup["last_close"]
    if trig is not None and close:
        s = sgn(setup["direction"])
        dist = s * (trig - close)  # > 0: price still has to travel to the trigger
        setup["distance_to_trigger_pct"] = round(100 * dist / close, 2)
        if d is not None and d.bars:
            setup["distance_to_trigger_atr"] = round(dist / d.atr_before(len(d.bars)), 2)
    return setup


def sort_setups(setups: list[dict], key: str = "quality", descending: bool | None = None
                ) -> list[dict]:
    """Sort keys: quality, ticker, strategy, direction, risk_reward, distance (absolute
    distance from the entry trigger). TRIGGERED/WATCHING always rank above NO_TRADE."""
    if key not in SORT_KEYS:
        raise ValueError(f"sort must be one of {', '.join(SORT_KEYS)}")
    if descending is None:
        descending = key in ("quality", "risk_reward")
    big = float("inf")

    def k(s):
        if key == "quality":
            v = s["quality"]["score"]
        elif key == "risk_reward":
            v = s["risk_reward"] if s["risk_reward"] is not None else (-big if descending else big)
        elif key == "distance":
            dd = s["distance_to_trigger_pct"]
            v = abs(dd) if dd is not None else (-big if descending else big)
        else:
            v = s[key]
        return v

    active = [s for s in setups if s["status"] != NO_TRADE]
    rest = [s for s in setups if s["status"] == NO_TRADE]
    tie = lambda s: (STATUS_ORDER[s["status"]], s["ticker"], s["strategy"], s["timeframe"],
                     s["direction"])
    active = sorted(sorted(active, key=tie), key=k, reverse=descending)
    rest = sorted(rest, key=tie)
    return active + rest


def evaluate_swing_symbol(ticker: str, frames: dict[str, list[Bar]], as_of: datetime,
                          cal: MarketCalendar, cfg: SwingConfig,
                          data_problems: list[str] | None = None,
                          strategies: list[SwingStrategy] | None = None,
                          full_history: bool = False, is_demo: bool = False,
                          planning=None) -> tuple[SwingContext, list[dict]]:
    ctx = build_swing_context(ticker, frames, as_of, cal, cfg, data_problems, planning)
    return ctx, evaluate_swing_context(ctx, strategies, full_history=full_history,
                                       is_demo=is_demo)
