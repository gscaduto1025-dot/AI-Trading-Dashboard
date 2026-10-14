"""Rules-based conditional trade plans.

`build_plan(PlanInput)` turns a strategy's objective evidence into a complete plan, or
explains why there is none. It never invents an entry, never moves a target to satisfy
the reward/risk threshold, and never returns TRIGGERED without a confirmed trigger.

Status rules
------------
NO_TRADE when any of these holds (each is reported as a failed check):
  * unreliable data: stale/unavailable/invalid candles, non-finite or non-positive prices,
    too little candle history, quoted spread wider than max_spread_pct;
  * no valid technical setup (no objective entry trigger), or no structural stop anchor;
  * invalid stop placement: the invalidation level is not beyond the entry on the loss side;
  * zero/negligible risk (< min_risk_atr x ATR) or a stop wider than max_risk_atr x ATR;
  * reward/risk to T1 below min_rr (T1 is the nearest confirmed level - never moved);
  * contradictory signals: >= max_conflicts conflicting signals;
  * a strategy entry rule rejected the trigger candle (entry window, abnormal candle, chasing);
  * the strategy reports a trigger but a required confirmation condition failed.
WATCHING when the plan is valid but entry confirmation has not happened yet.
TRIGGERED only when the confirmation candle has closed and every check passed.

Prices
------
Entry trigger: the objective level a completed candle must close beyond.
Entry: TRIGGERED -> the confirmation candle's close; WATCHING -> the trigger level (planned).
Stop: structural invalidation level -/+ stop_buffer_atr x ATR.
Targets: T1 is the nearest confirmed target candidate beyond the entry (key levels and
strategy measured moves; at least level_clearance_atr x ATR away so a level at the entry
price is not a target). T2 is the next candidate at least t2_min_gap_atr x ATR beyond T1.
Only when no candidate lies ahead is an ATR projection used (entry +/- atr_projection x
ATR; T2 = T1 +/- atr_extension x ATR), and the basis says so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from typing import Any

from .config import PlanningConfig

Condition = Any  # strategies' Condition (name, passed, required, detail, at, to_dict())

LONG, SHORT = "LONG", "SHORT"
TRIGGERED, WATCHING, NO_TRADE = "TRIGGERED", "WATCHING", "NO_TRADE"
SIGNAL_NONE = "NONE"


def sgn(direction: str) -> int:
    return 1 if direction == LONG else -1


def _ok_price(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x) and x > 0


def _r(x, nd=4):
    return None if x is None else round(float(x), nd)


@dataclass(frozen=True)
class PriceRef:
    """A price with the reason it exists."""

    price: float
    basis: str
    source: str = ""  # e.g. PDH, zone, swing, measured, range
    timeframe: str | None = None
    strength: float | None = None  # key-level strength 0-100 when rated

    def to_dict(self) -> dict:
        return {"price": _r(self.price), "basis": self.basis, "source": self.source,
                "timeframe": self.timeframe, "strength": self.strength}


@dataclass
class QualityInputs:
    htf: str = "unknown"  # aligned | neutral | opposed | unknown
    htf_detail: str = ""
    level_strength: float | None = None  # 0-100
    level_detail: str = ""
    rel_volume: float | None = None
    rel_volume_detail: str = ""
    structure: str = "unknown"  # aligned | mixed | opposed | unknown
    structure_detail: str = ""


@dataclass
class PlanInput:
    ticker: str
    mode: str  # "day" | "swing"
    strategy: str
    strategy_name: str
    direction: str
    timeframe: str
    signal: str  # TRIGGERED (confirmed), WATCHING (pending confirmation), NONE
    atr: float | None
    atr_label: str
    entry_trigger: PriceRef | None = None
    entry_price: PriceRef | None = None  # TRIGGERED: the confirmation close
    stop_anchor: PriceRef | None = None
    candidates: list[PriceRef] = field(default_factory=list)
    confirmation: list[Condition] = field(default_factory=list)
    invalidation: list[str] = field(default_factory=list)
    quality: QualityInputs = field(default_factory=QualityInputs)
    conflicts: list[str] = field(default_factory=list)
    data_issues: list[str] = field(default_factory=list)
    blocked: str | None = None  # a strategy entry rule rejected the trigger candle
    notes: list[str] = field(default_factory=list)  # strategy explanation
    spread_pct: float | None = None
    signal_time: int | None = None


# ----------------------------------------------------------------------- targets

def select_targets(direction: str, entry: float, atr: float, candidates: list[PriceRef],
                   cfg: PlanningConfig, mode: str, atr_label: str = "ATR"
                   ) -> tuple[PriceRef, PriceRef]:
    """Nearest candidate ahead = T1; next one far enough beyond it = T2; ATR projections
    only when nothing lies ahead. Independent of the stop: a target is never chosen to
    produce a particular reward/risk."""
    s = sgn(direction)
    clear = cfg.level_clearance_atr * atr
    ahead = sorted((c for c in candidates if _ok_price(c.price) and s * (c.price - entry) >= clear),
                   key=lambda c: (s * (c.price - entry), -(c.strength or 0)))
    if ahead:
        c = ahead[0]
        t1 = PriceRef(c.price, f"Nearest confirmed target ahead of entry: {c.basis}", c.source,
                      c.timeframe, c.strength)
    else:
        m = cfg.atr_projection[mode]
        t1 = PriceRef(entry + s * m * atr,
                      f"No confirmed level or measured move ahead of the entry, so an ATR "
                      f"projection is used: entry {entry:.2f} {'+' if s > 0 else '-'} {m:g} x "
                      f"{atr_label} {atr:.2f}", "atr_projection")
    gap = cfg.t2_min_gap_atr * atr
    beyond = [c for c in ahead if s * (c.price - t1.price) >= gap]
    if beyond:
        c = beyond[0]
        t2 = PriceRef(c.price, f"Next confirmed target beyond T1 (>= {cfg.t2_min_gap_atr:g} ATR "
                               f"further): {c.basis}", c.source, c.timeframe, c.strength)
    else:
        m = cfg.atr_extension[mode]
        t2 = PriceRef(t1.price + s * m * atr,
                      f"No confirmed level beyond T1, so an ATR extension is used: T1 "
                      f"{t1.price:.2f} {'+' if s > 0 else '-'} {m:g} x {atr_label} {atr:.2f}",
                      "atr_extension")
    return t1, t2


# ----------------------------------------------------------------------- quality

def quality_score(inp: PlanInput, rr: float | None, min_rr: float, cfg: PlanningConfig) -> dict:
    """Transparent 0-100 ranking of how much objective evidence supports the plan.
    Not a probability of success."""
    w = cfg.weights
    q = inp.quality
    req = [c for c in inp.confirmation if c.required and c.passed is not None]
    pa = sum(1 for c in req if c.passed) / len(req) if req else 0.0
    rv = q.rel_volume
    parts = [
        ("higher_timeframe", {"aligned": 1.0, "neutral": 0.5, "opposed": 0.0}.get(q.htf, 0.5),
         f"{q.htf}" + (f": {q.htf_detail}" if q.htf_detail else "")),
        ("level_strength", 0.5 if q.level_strength is None else
         max(0.0, min(q.level_strength / 100, 1.0)),
         q.level_detail or ("not rated" if q.level_strength is None
                            else f"strength {q.level_strength:.0f}/100")),
        ("price_action", pa, f"{sum(1 for c in req if c.passed)} of {len(req)} required "
                             f"confirmation conditions passed"),
        ("relative_volume", 0.5 if rv is None else max(0.0, min(rv - 0.5, 1.0)),
         q.rel_volume_detail or ("unavailable (scored neutral)" if rv is None
                                 else f"{rv:.2f}x average (0.5x = 0, 1.5x+ = full)")),
        ("trend_structure", {"aligned": 1.0, "mixed": 0.5, "opposed": 0.0}.get(q.structure, 0.5),
         f"{q.structure}" + (f": {q.structure_detail}" if q.structure_detail else "")),
        ("risk_reward", 0.0 if not rr else min(rr / (2 * min_rr), 1.0),
         "n/a" if not rr else f"{rr:.2f}:1 (full points at {2 * min_rr:g}:1)"),
    ]
    comps = [{"name": n, "weight": w[n], "value": round(v, 3), "points": round(w[n] * v, 1),
              "detail": d} for n, v, d in parts]
    penalty = cfg.conflict_penalty * len(inp.conflicts)
    raw = sum(c["points"] for c in comps)
    return {"score": round(max(0.0, min(100.0, raw - penalty)), 1), "components": comps,
            "conflict_penalty": round(penalty, 1), "conflicts": list(inp.conflicts),
            "note": "Ranking of supporting evidence from explicit rules. It is not a predicted "
                    "win probability."}


# --------------------------------------------------------------------------- plan

def _check(name: str, passed: bool | None, detail: str) -> dict:
    return {"name": name, "passed": passed, "detail": detail}


def build_plan(inp: PlanInput, cfg: PlanningConfig) -> dict:
    s = sgn(inp.direction)
    word_up = "above" if s > 0 else "below"
    mode = inp.mode
    min_rr = cfg.min_rr_for(mode)
    checks: list[dict] = []
    calcs: list[str] = []
    fails: list[str] = []
    plan = {
        "ticker": inp.ticker, "mode": mode, "strategy": inp.strategy,
        "strategy_name": inp.strategy_name, "direction": inp.direction,
        "timeframe": inp.timeframe, "status": NO_TRADE, "reasons": [],
        "entry_trigger": inp.entry_trigger.to_dict() if inp.entry_trigger else None,
        "entry": None, "entry_zone": None, "stop": None, "target1": None, "target2": None,
        "risk_per_share": None, "reward_per_share_t1": None, "reward_per_share_t2": None,
        "risk_reward_t1": None, "risk_reward_t2": None, "risk_reward_worst_fill": None,
        "min_rr": min_rr, "atr": {"value": _r(inp.atr), "label": inp.atr_label},
        "confirmation": [c.to_dict() for c in inp.confirmation],
        "invalidation": list(inp.invalidation), "conflicts": list(inp.conflicts),
        "checks": checks, "calculations": calcs, "signal_time": inp.signal_time,
        "quality": None,
    }

    def finish(status: str) -> dict:
        plan["status"] = status
        rr = plan["risk_reward_t1"]
        plan["quality"] = quality_score(inp, rr, min_rr, cfg)
        if status == NO_TRADE:
            plan["reasons"] = fails + [n for n in inp.notes if n not in fails]
        return plan

    # 1. Data reliability.
    data_ok = not inp.data_issues
    checks.append(_check("Market data and conditions acceptable", data_ok,
                         "; ".join(inp.data_issues) if inp.data_issues else
                         "completed, validated, fresh candles"))
    if not data_ok:
        fails.append("Unreliable or insufficient market data: " + "; ".join(inp.data_issues))
        return finish(NO_TRADE)
    if inp.spread_pct is not None:
        sp_ok = inp.spread_pct <= cfg.max_spread_pct[mode]
        checks.append(_check(f"Quoted spread <= {cfg.max_spread_pct[mode]}%", sp_ok,
                             f"{inp.spread_pct:.3f}% of price"))
        if not sp_ok:
            fails.append(f"Unreliable prices: quoted spread {inp.spread_pct:.3f}% > "
                         f"{cfg.max_spread_pct[mode]}%.")
            return finish(NO_TRADE)
    else:
        checks.append(_check("Quoted spread acceptable", None, "no bid/ask quote on this feed"))

    # 2. A valid technical setup with an objective trigger.
    has_setup = inp.signal in (TRIGGERED, WATCHING) and inp.entry_trigger is not None \
        and _ok_price(inp.entry_trigger.price)
    checks.append(_check("Valid technical setup with an objective entry trigger", has_setup,
                         inp.entry_trigger.basis if has_setup else
                         (inp.notes[0] if inp.notes else "no qualifying pattern")))
    if not has_setup:
        fails.append("No valid technical setup: " + (inp.notes[0] if inp.notes else
                                                     "no objective entry trigger."))
        plan["reasons"] = fails
        return finish(NO_TRADE)
    atr_ok = inp.atr is not None and math.isfinite(inp.atr) and inp.atr > 0
    checks.append(_check("Volatility (ATR) available", atr_ok,
                         f"{inp.atr_label} {inp.atr:.4f}" if atr_ok else "insufficient candle history"))
    if not atr_ok:
        fails.append("Insufficient candle history: ATR unavailable, so stops and targets "
                     "cannot be sized.")
        return finish(NO_TRADE)
    atr = inp.atr
    trig = inp.entry_trigger

    # 3. Entry.
    if inp.signal == TRIGGERED:
        if inp.entry_price is None or not _ok_price(inp.entry_price.price):
            fails.append("Unreliable price: trigger reported without a valid confirmation close.")
            checks.append(_check("Valid entry price", False, "missing confirmation close"))
            return finish(NO_TRADE)
        entry = inp.entry_price.price
        entry_basis = inp.entry_price.basis
        kind = "confirmation close"
    else:
        entry = trig.price
        entry_basis = (f"Planned at the entry trigger {trig.price:.2f}; the order is conditional "
                       f"on a completed candle closing {word_up} it.")
        kind = "planned (conditional)"
    plan["entry"] = {"price": _r(entry), "basis": entry_basis, "kind": kind}
    # Entry zone: from the trigger level up to the confirmation close (TRIGGERED) or a
    # fixed volatility band (WATCHING); entries beyond it count as chasing.
    band = cfg.entry_zone_atr[mode] * atr
    far_edge = entry if inp.signal == TRIGGERED and s * (entry - trig.price) > band \
        else trig.price + s * band
    lo, hi = sorted((trig.price, far_edge))
    plan["entry_zone"] = {"low": _r(lo), "high": _r(hi),
                          "basis": f"From the trigger {trig.price:.2f} to "
                                   f"{'the confirmation close' if far_edge == entry else f'{band / atr:.2f} x ATR beyond it'}"
                                   f" ({far_edge:.2f}); fills beyond this are chasing."}

    # 4. Stop from the structural invalidation level.
    anchor = inp.stop_anchor
    if anchor is None or not _ok_price(anchor.price):
        checks.append(_check("Structural stop level", False, "no invalidation level identified"))
        fails.append("No structural invalidation level for the stop - a stop would be arbitrary.")
        return finish(NO_TRADE)
    buf_atr = cfg.stop_buffer_atr[mode]
    stop = anchor.price - s * buf_atr * atr
    plan["stop"] = {"price": _r(stop), "anchor": _r(anchor.price), "buffer_atr": buf_atr,
                    "buffer": _r(buf_atr * atr),
                    "basis": f"{anchor.basis} {'-' if s > 0 else '+'} {buf_atr:g} x "
                             f"{inp.atr_label} {atr:.2f} volatility buffer"}
    calcs.append(f"Stop = invalidation {anchor.price:.2f} {'-' if s > 0 else '+'} {buf_atr:g} x "
                 f"ATR {atr:.2f} = {stop:.2f}")
    if abs(entry - stop) < 1e-9:
        checks.append(_check("Non-zero risk", False, f"stop {stop:.2f} equals entry {entry:.2f}"))
        plan["risk_per_share"] = 0.0
        fails.append(f"Zero risk: the stop {stop:.2f} equals the entry {entry:.2f}, so "
                     f"reward/risk is undefined.")
        return finish(NO_TRADE)
    side_ok = s * (entry - anchor.price) > 0 and s * (entry - stop) > 0
    checks.append(_check(f"Stop {'below' if s > 0 else 'above'} the entry (invalidation on the "
                         f"loss side)", side_ok,
                         f"entry {entry:.2f}, invalidation {anchor.price:.2f}, stop {stop:.2f}"))
    if not side_ok:
        fails.append(f"Invalid stop placement: the invalidation level {anchor.price:.2f} is not "
                     f"{'below' if s > 0 else 'above'} the entry {entry:.2f}.")
        return finish(NO_TRADE)
    risk = s * (entry - stop)
    plan["risk_per_share"] = _r(risk)
    calcs.append(f"Risk per share = |entry {entry:.2f} - stop {stop:.2f}| = {risk:.4f}"
                 f" ({risk / atr:.2f} ATR)")
    risk_ok = risk >= cfg.min_risk_atr * atr
    checks.append(_check(f"Risk >= {cfg.min_risk_atr:g} ATR (not zero)", risk_ok,
                         f"{risk:.4f} = {risk / atr:.3f} ATR"))
    if not risk_ok:
        fails.append(f"Zero or negligible risk: {risk:.4f} per share is below "
                     f"{cfg.min_risk_atr:g} x ATR, so reward/risk would be meaningless.")
        return finish(NO_TRADE)
    max_risk = cfg.max_risk_atr[mode]
    wide_ok = risk <= max_risk * atr
    checks.append(_check(f"Stop distance <= {max_risk:g} ATR", wide_ok, f"{risk / atr:.2f} ATR"))

    # 5. Targets (independent of the stop) and reward/risk.
    t1, t2 = select_targets(inp.direction, entry, atr, inp.candidates, cfg, mode, inp.atr_label)
    plan["target1"], plan["target2"] = t1.to_dict(), t2.to_dict()
    rew1, rew2 = s * (t1.price - entry), s * (t2.price - entry)
    rr1, rr2 = rew1 / risk, rew2 / risk
    plan.update({"reward_per_share_t1": _r(rew1), "reward_per_share_t2": _r(rew2),
                 "risk_reward_t1": round(rr1, 2), "risk_reward_t2": round(rr2, 2)})
    worst = trig.price + s * band if inp.signal == WATCHING else far_edge
    w_risk, w_rew = s * (worst - stop), s * (t1.price - worst)
    plan["risk_reward_worst_fill"] = round(w_rew / w_risk, 2) if w_risk > 0 else None
    calcs += [f"Reward per share (T1) = |T1 {t1.price:.2f} - entry {entry:.2f}| = {rew1:.4f}",
              f"Reward per share (T2) = |T2 {t2.price:.2f} - entry {entry:.2f}| = {rew2:.4f}",
              f"R:R (T1) = {rew1:.4f} / {risk:.4f} = {rr1:.2f}",
              f"R:R (T2) = {rew2:.4f} / {risk:.4f} = {rr2:.2f}",
              f"Required minimum R:R (T1) = {min_rr:g} (planning config)"]
    if plan["risk_reward_worst_fill"] is not None:
        calcs.append(f"R:R at the far edge of the entry zone ({worst:.2f}) = "
                     f"{w_rew:.4f} / {w_risk:.4f} = {plan['risk_reward_worst_fill']:.2f}")
    rr_ok = rr1 >= min_rr - 1e-9
    checks.append(_check(f"Reward/risk to T1 >= {min_rr:g}", rr_ok,
                         f"{rr1:.2f}:1 with T1 at the nearest target - targets are never moved "
                         f"to meet the threshold"))
    conflict_ok = len(inp.conflicts) < cfg.max_conflicts
    checks.append(_check(f"Fewer than {cfg.max_conflicts} conflicting signals", conflict_ok,
                         "; ".join(inp.conflicts) if inp.conflicts else "none"))
    checks.append(_check("Strategy entry rules (window, abnormal candle, chasing)",
                         inp.blocked is None, inp.blocked or "passed"))
    if inp.blocked:
        fails.append(f"Entry rule rejected the trigger candle: {inp.blocked}.")
    if not wide_ok:
        fails.append(f"Stop too wide: {risk / atr:.2f} ATR > {max_risk:g} ATR.")
    if not rr_ok:
        fails.append(f"Poor risk/reward: T1 {t1.price:.2f} gives {rr1:.2f}:1 < {min_rr:g}:1 "
                     f"({t1.basis}). The target is not moved to fit.")
    if not conflict_ok:
        fails.append("Contradictory signals: " + "; ".join(inp.conflicts) + ".")
    if fails:
        return finish(NO_TRADE)

    # 6. Confirmation.
    if inp.signal == TRIGGERED:
        missing = [c.name for c in inp.confirmation if c.required and c.passed is False]
        checks.append(_check("Entry confirmation on a completed candle", not missing,
                             "missing: " + ", ".join(missing) if missing else entry_basis))
        if missing:
            fails.append("Missing confirmation: " + ", ".join(missing) + ".")
            return finish(NO_TRADE)
        plan["reasons"] = [f"Confirmed: {entry_basis}"] + inp.notes
        return finish(TRIGGERED)
    pending = [c.name for c in inp.confirmation if c.required and c.passed is not True]
    checks.append(_check("Entry confirmation on a completed candle", None,
                         "pending: " + (", ".join(pending) if pending else
                                        f"a close {word_up} {trig.price:.2f}")))
    plan["reasons"] = inp.notes + [f"Entry confirmation has not occurred: wait for "
                                   f"{', '.join(pending) or f'a close {word_up} {trig.price:.2f}'}."]
    return finish(WATCHING)
