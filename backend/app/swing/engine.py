"""Runs every enabled swing strategy independently on daily and weekly candles.

Status rules (per ticker x strategy x timeframe x direction):
* Gates (unavailable/stale/invalid data, < 60 daily candles, insufficient liquidity)
  => NO_TRADE with those reasons; nothing may trigger.
* The most recent *accepted* trigger => TRIGGERED while it is fresh (at most
  max_trigger_age candles old on its timeframe) and still live: no close beyond its
  invalidation price and neither the stop nor T2 reached since. Triggers are final once
  emitted; later candles only add the informational `outcome`.
* Otherwise a developing pattern => WATCHING, with a hypothetical plan - unless that plan
  would be rejected at the trigger (reward/risk below min_rr or stop wider than
  max_risk_atr), which stays NO_TRADE with the plan shown for reference.
* Otherwise NO_TRADE with the strategy's reasons (incl. rejected or expired triggers).

Setup quality (0-100, a transparent ranking aid - not a probability of profit):
  40 x share of required conditions passed + 20 x share of supporting conditions passed
  + 15 x min(R:R / 3, 1) + 15 x higher-timeframe alignment (aligned 1, neutral 0.5,
  opposed 0) + 10 x min(zone reactions, 5) / 5 for the setup's own zone.
"""

from __future__ import annotations

from datetime import datetime

from ..data.base import Bar
from ..market.calendar import NY, MarketCalendar
from ..strategies.common import NO_TRADE, TRIGGERED, WATCHING, outcome_after, signal_id
from .base import LONG, SHORT, SwingStrategy, sgn
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


def quality(conditions: list, rr: float | None, htf_label: str, direction: str,
            levels: list[dict]) -> dict:
    req = [c for c in conditions if c.required and c.passed is not None]
    opt = [c for c in conditions if not c.required and c.passed is not None]
    req_s = sum(1 for c in req if c.passed) / len(req) if req else 0.0
    opt_s = sum(1 for c in opt if c.passed) / len(opt) if opt else 0.5
    rr_s = min((rr or 0) / 3, 1.0)
    want = "bullish" if direction == LONG else "bearish"
    htf_s = 1.0 if htf_label == want else 0.5 if htf_label == "neutral" else 0.0
    reactions = max((l.get("reactions") or 0 for l in levels), default=0)
    zone_s = min(reactions, 5) / 5
    comps = {"required_conditions": round(40 * req_s, 1),
             "supporting_conditions": round(20 * opt_s, 1),
             "reward_risk": round(15 * rr_s, 1), "higher_timeframe": round(15 * htf_s, 1),
             "zone_reactions": round(10 * zone_s, 1)}
    return {"score": round(sum(comps.values()), 1), "components": comps,
            "note": "Ranking aid from explicit rules; not a probability of success."}


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
    out: list[dict] = []
    evaluated_at = ctx.as_of.astimezone(NY).isoformat()
    for strat in strategies or SWING_STRATEGIES:
        if not strat.enabled(ctx):
            continue
        for tf in timeframes or SWING_PRIMARY_TIMEFRAMES:
            d = ctx.tfs.get(tf)
            for direction in (LONG, SHORT):
                out.append(_evaluate_one(ctx, strat, tf, d, direction, full_history,
                                         evaluated_at, is_demo))
    return sort_setups(out, "quality")


def _evaluate_one(ctx, strat, tf, d, direction, full_history, evaluated_at, is_demo) -> dict:
    n = len(d.bars) if d else 0
    last = d.bars[-1] if n else None
    htf = ctx.htf(tf, n - 1) if n > 1 else {"label": "neutral", "detail": "no data"}
    setup = {
        "id": None, "ticker": ctx.ticker, "mode": "swing", "strategy": strat.id,
        "strategy_name": strat.name, "direction": direction, "timeframe": tf,
        "status": NO_TRADE, "quality": {"score": 0.0, "components": {}, "note": ""},
        "market_structure": {}, "daily_levels": [], "weekly_levels": [],
        "entry_trigger": None, "entry_zone": None, "entry": None, "stop": None,
        "target1": None, "target2": None, "risk_reward": None, "risk_per_share": None,
        "distance_to_trigger_pct": None, "distance_to_trigger_atr": None,
        "confirmation": [], "invalidation": [], "evidence": [], "supporting_levels": [],
        "reasons": [], "targets_basis": [], "trigger_time": None, "confirmed_at": None,
        "outcome": None, "triggers": [], "last_close": _r(last.close) if last else None,
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
    if ctx.gate_reasons:
        setup["reasons"] = list(ctx.gate_reasons)
        return _finish(setup, ctx, d)
    if d is None or n < MIN_SCAN_INDEX + 1:
        setup["reasons"] = [f"Not enough completed {tf} candles ({n}) for swing analysis."]
        return _finish(setup, ctx, d)
    start = MIN_SCAN_INDEX if full_history else max(MIN_SCAN_INDEX, n - LIVE_SCAN_CANDLES)
    res = strat.evaluate(ctx, d, direction, start)
    setup["triggers"] = [_trigger_dict(t) for t in res.triggers]
    accepted = [t for t in res.triggers if t.blocked is None]
    max_age = ctx.config.gates.max_trigger_age[tf]
    live = None
    stale_note = None
    if accepted:
        t = accepted[-1]
        age = n - 1 - t.index
        outcome = outcome_after(d, t.index, direction, t.stop, t.target1, t.target2)
        inv = _invalidated(d, t.index, direction, t.invalidation_price)
        if outcome["state"] in ("stopped", "target1_then_stopped", "target2"):
            stale_note = f"Last trigger {fmt_day(t.time)} already resolved ({outcome['state']})."
        elif inv is not None:
            stale_note = (f"Last trigger {fmt_day(t.time)} was invalidated by the close on "
                          f"{fmt_day(d.bars[inv].time)}.")
        elif age >= max_age:
            stale_note = (f"Last trigger {fmt_day(t.time)} is {age} candle(s) old; triggers "
                          f"stay actionable for {max_age} {tf} candle(s).")
        else:
            live = (t, outcome)
    if live:
        t, outcome = live
        setup.update({
            "status": TRIGGERED, "entry_trigger": _r(t.entry_trigger),
            "entry_zone": [_r(t.entry_zone[0]), _r(t.entry_zone[1])], "entry": _r(t.entry),
            "stop": _r(t.stop), "target1": _r(t.target1), "target2": _r(t.target2),
            "risk_reward": _r(t.risk_reward, 2), "risk_per_share": _r(abs(t.entry - t.stop)),
            "confirmation": [c.to_dict() for c in t.conditions], "invalidation": t.invalidation,
            "evidence": t.evidence, "supporting_levels": t.levels,
            "targets_basis": t.targets_basis, "trigger_time": t.time,
            "confirmed_at": t.confirmed_at, "outcome": outcome,
            "id": signal_id(ctx.ticker, strat.id, tf, direction, "swing", t.time),
            "reasons": [f"All {strat.name.lower()} conditions met on the {tf} candle of "
                        f"{fmt_day(t.time)} (completed close)."]})
        setup["quality"] = quality(t.conditions, t.risk_reward, ctx.htf(tf, t.index)["label"],
                                   direction, t.levels)
    elif res.watch:
        w = res.watch
        setup.update({
            "status": WATCHING, "entry_trigger": _r(w.entry_trigger),
            "entry_zone": [_r(w.entry_zone[0]), _r(w.entry_zone[1])],
            "entry": _r(w.entry_trigger), "stop": _r(w.stop), "target1": _r(w.target1),
            "target2": _r(w.target2), "risk_reward": _r(w.risk_reward, 2),
            "risk_per_share": _r(abs(w.entry_trigger - w.stop)),
            "confirmation": [c.to_dict() for c in w.conditions], "invalidation": w.invalidation,
            "supporting_levels": w.levels, "targets_basis": w.targets_basis,
            "reasons": list(w.reasons)})
        if w.plan_problem:
            # The pattern is developing, but a trigger at this plan would be rejected.
            setup["status"] = NO_TRADE
            setup["reasons"] = [f"Developing, but {w.plan_problem}: {w.reasons[0]}"]
        else:
            setup["quality"] = quality(w.conditions, w.risk_reward, htf["label"], direction,
                                       w.levels)
        if stale_note:
            setup["reasons"].append(stale_note)
        setup["reasons"] += [r for r in res.reasons if r not in setup["reasons"]]
        for t in [t for t in res.triggers if t.blocked and n - 1 - t.index < max_age][-1:]:
            setup["reasons"].append(f"Conditions met on {fmt_day(t.time)} but rejected: "
                                    f"{t.blocked}.")
    else:
        setup["reasons"] = list(res.reasons) or [
            f"No accepted {strat.name.lower()} trigger in the last {max_age} {tf} candle(s)."]
        if stale_note:
            setup["reasons"].insert(0, stale_note)
        rejected = [t for t in res.triggers if t.blocked and n - 1 - t.index < max_age]
        for t in rejected[-1:]:
            setup["reasons"].append(f"Conditions met on {fmt_day(t.time)} but rejected: "
                                    f"{t.blocked}.")
            setup["confirmation"] = [c.to_dict() for c in t.conditions]
            setup["supporting_levels"] = t.levels
    if ctx.levels_note:
        setup["reasons"].append(ctx.levels_note)
    return _finish(setup, ctx, d)


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
                          full_history: bool = False, is_demo: bool = False
                          ) -> tuple[SwingContext, list[dict]]:
    ctx = build_swing_context(ticker, frames, as_of, cal, cfg, data_problems)
    return ctx, evaluate_swing_context(ctx, strategies, full_history=full_history,
                                       is_demo=is_demo)
