"""Rules-based setup explanations: deterministic text built only from the engines' own
output (the same verified fact sheet the optional Claude layer receives). No AI, no API
call, no invented number: every price and ratio below is copied from the fact sheet.
"""

from __future__ import annotations

from .ai.facts import build_facts, engine_status
from .data.base import DEFAULT_TIMEFRAME
from .validation import for_strategy

SOURCE = "Rules-based explanation from the deterministic engines (not AI-generated)."


def _p(v) -> str:
    return "—" if v is None else f"{v:,.2f}"


def _level(l: dict | None) -> str:
    if not l:
        return "none found"
    where = (_p(l["price"]) if l.get("price") is not None
             else f"{_p(l['zone_low'])}-{_p(l['zone_high'])}")
    extra = f", strength {l['strength']}/100" if l.get("strength") is not None else ""
    return f"{l['label']} {where} ({l['timeframe'] or 'n/a'}{extra}, {l['distance_pct']:+.2f}% from price)"


def rules_text(facts: dict) -> dict:
    """Sections of the explanation, each a list of plain sentences."""
    s = facts.get("setup")
    ev = facts["strategy_evidence"]
    live = facts.get("live_entry") or {}
    status = engine_status(facts)
    bias = facts["market_bias"]
    ctx: dict = {}
    out: dict = {"source": SOURCE, "status": status, "symbol": facts["symbol"],
                 "mode": facts["trading_mode"], "data_timestamp": facts["data_timestamp"],
                 "data_source": facts["data_source"], "evidence_label": ev["label"],
                 "evidence_status": ev["status"]}
    market = [f"Market bias {bias['label']} (score {bias['score']}/{bias['max_score']}) on the "
              f"{facts['chart_timeframe']} candles; last price {_p(facts['last_price'])}."]
    for f in bias.get("factors") or []:
        market.append(f"{f['factor']}: {f['signal']} - {f['detail']}.")
    if facts.get("vwap"):
        market.append(f"VWAP {_p(facts['vwap']['value'])}: {facts['vwap']['relation']} "
                      f"({facts['vwap']['distance_pct']:+.2f}%).")
    ma = facts.get("moving_averages") or {}
    if ma.get("alignment"):
        market.append(f"Moving averages: {ma['alignment']}.")
    vol = facts.get("volume") or {}
    if vol.get("relative_volume") is not None:
        market.append(f"Relative volume {vol['relative_volume']}x.")
    out["market_context"] = market
    levels = [f"Nearest support: {_level(facts.get('nearest_support'))}.",
              f"Nearest resistance: {_level(facts.get('nearest_resistance'))}."]
    for l in (facts.get("key_levels") or [])[:6]:
        ev_text = (l.get("evidence") or "").rstrip(". ")
        levels.append(f"{_level(l)}" + (f" - {ev_text}" if ev_text else "") + ".")
    out["key_levels"] = levels
    if not s:
        out["detected"] = [f"No setup is detected for {facts['symbol']} in {facts['trading_mode']} "
                           f"trading; status NO_TRADE."]
        out["status_reason"] = [facts.get("setup_note") or "No pattern qualifies."]
        out["evidence"] = ["No strategy is selected, so there is no strategy evidence."]
        for k in ("why_it_qualifies", "confirmation_required", "invalidation", "trade_plan",
                  "risk_reward"):
            out[k] = []
        return out
    ctx = s.get("context") or {}
    out["detected"] = [f"{s['name']} ({s['direction']}, {s['timeframe']}) on {facts['symbol']}, "
                       f"{facts['trading_mode']} trading. Engine status: {status}."]
    why = list(s.get("status_reasons") or [])
    why += [f"{c['condition']}: {c['detail']} (met)." for c in s.get("confirmation") or []
            if c["status"] == "met"]
    if ctx.get("higher_timeframe"):
        why.append(f"Higher timeframe: {ctx['higher_timeframe']}"
                   + (f" - {ctx['higher_timeframe_detail']}" if ctx.get('higher_timeframe_detail')
                      else "") + ".")
    if ctx.get("structure"):
        why.append(f"Market structure: {ctx['structure']}.")
    if s.get("quality_score") is not None:
        why.append(f"Setup quality score {s['quality_score']}/100 (a technical rules score, "
                   "not a win probability).")
    out["why_it_qualifies"] = why
    pending = [f"{c['condition']}: {c['detail']}" + (" (required)." if c["required"] else ".")
               for c in s.get("confirmation") or [] if c["status"] == "not met"]
    trig = s.get("entry_trigger")
    if trig:
        pending.insert(0, f"Entry trigger {_p(trig['price'])}: {trig['basis']}.")
    out["confirmation_required"] = pending
    inv = list(s.get("invalidation") or [])
    if s.get("stop"):
        inv.append(f"Stop {_p(s['stop']['price'])}: {s['stop']['basis']}.")
    out["invalidation"] = inv
    plan = []
    for k, name in (("entry", "Entry"), ("stop", "Stop-loss"), ("target1", "Target 1"),
                    ("target2", "Target 2")):
        if s.get(k):
            plan.append(f"{name} {_p(s[k]['price'])} - {s[k]['basis']}.")
    if s.get("entry_zone"):
        plan.append(f"Entry zone {_p(s['entry_zone']['low'])}-{_p(s['entry_zone']['high'])}.")
    out["trade_plan"] = plan
    rr = []
    if s.get("risk_per_share") is not None:
        rr.append(f"Risk {_p(s['risk_per_share'])} per share; reward to T1 "
                  f"{_p(s['reward_per_share_t1'])} ({s['risk_reward_t1']}:1)"
                  + (f", to T2 {_p(s['reward_per_share_t2'])} ({s['risk_reward_t2']}:1)"
                     if s.get("risk_reward_t2") is not None else "")
                  + f"; minimum required {s['min_risk_reward']}:1.")
    if live.get("live_risk_reward") is not None:
        rr.append(f"At the live price {_p(live.get('live_price'))} the reward/risk to T1 is "
                  f"{live['live_risk_reward']}:1.")
    for c in s.get("conflicting_signals") or []:
        rr.append(f"Conflicting evidence: {c}.")
    out["risk_reward"] = rr
    reason = []
    if status == "TRIGGERED":
        if live.get("signal_state") == "HISTORICALLY_TRIGGERED":
            reason.append("TRIGGERED earlier, but it is not a fresh entry now: "
                          + "; ".join(x.rstrip(". ") for x in live.get("checks_failed") or [])
                          + ".")
        elif live.get("signal_state") == "CURRENTLY_ACTIONABLE":
            reason.append("TRIGGERED, and every entry check passes at the live price.")
        else:
            reason.append("TRIGGERED: the entry conditions were confirmed on a completed candle.")
    elif status == "WATCHING":
        reason.append("WATCHING: the pattern is forming; the confirmation above is still "
                      "required before it triggers.")
    else:
        why_not = [x.rstrip(". ") for x in (s.get("failed_checks") or s.get("status_reasons")
                                             or ["a requirement is not met"])]
        reason.append("NO_TRADE: " + "; ".join(why_not) + ".")
    if live.get("volume_confirmation") in ("LIMITED", "UNVERIFIED", "UNAVAILABLE"):
        reason.append(f"Volume confirmation is {live['volume_confirmation']} (IEX-only "
                      "volume for the newest candles).")
    out["status_reason"] = reason
    evid = [f"{ev['label']}: {ev['summary']}", ev["notice"]]
    if ev.get("this_timeframe"):
        t = ev["this_timeframe"]
        evid.append(f"On {t['timeframe']}: {t['trades']} validation trades, "
                    f"{t['net_r_per_trade']:+.2f}R per trade after costs.")
    out["evidence"] = evid
    return out


def explain_setup(service, symbol: str, mode: str, setup_id: str | None,
                  scanner_row: dict | None = None) -> dict:
    """Facts and rules-based explanation for one setup (or the symbol's best plan)."""
    setup, plan = service.setup_with_plan(symbol, mode, setup_id)
    tf = plan["timeframe"] if plan else DEFAULT_TIMEFRAME[mode]
    analysis = service.analysis(symbol, mode, tf)
    evidence = for_strategy(plan["strategy"], plan["timeframe"]) if plan else None
    facts = build_facts(analysis, setup, plan, mode, service.is_demo, evidence, scanner_row)
    return {"symbol": symbol, "mode": mode, "setup_id": setup["id"] if setup else None,
            "status": engine_status(facts), "facts": facts, "explanation": rules_text(facts),
            "ai": False, "is_demo": service.is_demo}
