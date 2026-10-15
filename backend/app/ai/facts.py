"""The verified fact sheet Claude explains.

Every number Claude may use is assembled here, deterministically, from the market-data
service, the analysis engine (bias, indicators), the key-level engine and the
trade-planning engine. Claude receives only this sheet; the validator later checks every
number in Claude's text against it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from ..market.calendar import NY
from ..validation import for_strategy

FACTS_VERSION = "4"  # bump when the sheet layout or the prompt changes (cache key)


def _iso(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, NY).isoformat() if ts else None


def _pct(a: float, b: float) -> float:
    return round(100 * (a - b) / b, 2) if b else 0.0


def _level(l: dict, price: float) -> dict:
    mid = (l["low"] + l["high"]) / 2
    zone = l["high"] - l["low"] > 1e-9
    near = l["low"] if l["low"] > price else l["high"] if l["high"] < price else price
    return {"label": l["label"], "timeframe": l.get("timeframe"),
            "price": round(l["low"], 2) if not zone else None,
            "zone_low": round(l["low"], 2) if zone else None,
            "zone_high": round(l["high"], 2) if zone else None,
            "side": l.get("side") or ("resistance" if mid > price else "support"),
            "strength": l.get("strength"),
            "distance_pct": _pct(near, price),
            "evidence": l.get("evidence")}


def _ma_alignment(price: float, ind: dict, mode: str) -> dict:
    out: dict = {}
    names = []
    if ind.get("ema_fast") is not None:
        n = f"EMA{ind['ema_fast_period']}"
        out[n] = ind["ema_fast"]
        names.append((n, ind["ema_fast"]))
    if ind.get("ema_slow") is not None:
        n = f"EMA{ind['ema_slow_period']}"
        out[n] = ind["ema_slow"]
        names.append((n, ind["ema_slow"]))
    if ind.get("sma_long") is not None:
        out["SMA200"] = ind["sma_long"]
        names.append(("SMA200", ind["sma_long"]))
    rel = [f"price {'above' if price > v else 'below'} {n}" for n, v in names]
    vals = [v for _, v in names]
    if len(vals) >= 2 and all(a > b for a, b in zip(vals, vals[1:])):
        stack = "bullish stack (faster above slower)"
    elif len(vals) >= 2 and all(a < b for a, b in zip(vals, vals[1:])):
        stack = "bearish stack (faster below slower)"
    else:
        stack = "mixed"
    out["alignment"] = "; ".join(rel + [stack]) if names else "not available"
    return out


def _evidence(v: dict | None) -> dict:
    if not v:
        return {"label": "Unvalidated", "status": "UNVALIDATED",
                "summary": "No strategy is selected, so there is no historical evidence.",
                "notice": "Technical analysis only."}
    out = {"label": v["label"], "status": v["status"], "summary": v["summary"],
           "notice": v["notice"], "validation_trades": v.get("n"),
           "net_r_per_trade": v.get("net_r"), "net_r_95ci": v.get("net_ci"),
           "profit_factor": v.get("profit_factor"),
           "gross_r_per_trade_before_costs": v.get("gross_r"),
           "baseline": v.get("baseline")}
    if v.get("timeframe"):
        t = v["timeframe"]
        out["this_timeframe"] = {"timeframe": t["timeframe"], "trades": t["n"],
                                 "net_r_per_trade": t["net_r"], "net_r_95ci": t["net_ci"]}
    return out


def _live_entry(row: dict | None) -> dict | None:
    """The scanner's live entry state for the setup (same row the scanner shows)."""
    if not row:
        return None
    lv = row.get("nearest_level") or {}
    return {"signal_state": row.get("signal_state"), "actionable_now": row.get("actionable"),
            "checks_failed": row.get("live_checks_failed") or [],
            "live_price": row.get("live_price"),
            "live_risk_reward": row.get("live_risk_reward"),
            "volume_confirmation": row.get("volume_confirmation"),
            "nearest_key_level": ({k: lv.get(k) for k in ("side", "label", "distance_pct",
                                                          "distance_atr", "inside")}
                                  if lv else None),
            "liquidity_avg_daily_dollar_volume": row.get("liquidity_adv"),
            "data_freshness": row.get("data_freshness")}


def build_facts(analysis: dict, setup: dict | None, plan: dict | None, mode: str,
                is_demo: bool, evidence: dict | None = None,
                scanner_row: dict | None = None) -> dict:
    """Assemble the deterministic sheet for one symbol/mode and (optionally) one setup.
    `evidence` = the strategy's validation block (app.validation.for_strategy);
    `scanner_row` = the scanner's row for this setup (live entry state), if scanned."""
    price = analysis["price"]
    ind = analysis.get("indicators") or {}
    bias = analysis.get("bias") or {}
    status = analysis.get("data_status") or {}
    levels = [_level(l, price) for l in analysis.get("key_levels") or []]
    levels.sort(key=lambda l: abs(l["distance_pct"]))
    supports = [l for l in levels if l["side"] == "support" and l["distance_pct"] <= 0]
    resistances = [l for l in levels if l["side"] == "resistance" and l["distance_pct"] >= 0]
    vwap = ind.get("vwap")
    facts: dict = {
        "symbol": analysis["symbol"],
        "trading_mode": mode,
        "chart_timeframe": analysis["timeframe"],
        "data_timestamp": status.get("as_of") or _iso(analysis.get("analyzed_through")),
        "last_completed_candle": _iso(analysis.get("analyzed_through")),
        "data_source": ("DEMO DATA - simulated prices, not a real market read" if is_demo
                        else f"{status.get('feed_label', 'market data')} "
                             f"({status.get('latency', 'unknown latency')})"),
        "last_price": price,
        "market_bias": {
            "label": (bias.get("label") or "Neutral").lower(),
            "score": bias.get("score"), "max_score": bias.get("max_score"),
            "factors": [{"factor": f["label"], "signal": f["signal"], "detail": f["detail"]}
                        for f in bias.get("factors", [])]},
        "moving_averages": _ma_alignment(price, ind, mode),
        "vwap": None if vwap is None else {
            "value": vwap, "relation": f"price {'above' if price > vwap else 'below'} VWAP",
            "distance_pct": _pct(price, vwap)},
        "volume": {"relative_volume": ind.get("volume_ratio"),
                   "coverage": status.get("coverage")},
        "volatility": {"atr": ind.get("atr"), "rsi": ind.get("rsi")},
        "key_levels": levels[:10],
        "nearest_support": supports[0] if supports else None,
        "nearest_resistance": resistances[0] if resistances else None,
        "setup": None,
        "strategy_evidence": _evidence(None if setup is None else evidence or for_strategy(
            (plan or {}).get("strategy") or "", (plan or {}).get("timeframe"))),
        "live_entry": _live_entry(scanner_row),
    }
    if setup is not None and plan is not None:
        price_of = lambda k: (None if not plan.get(k) else
                              {"price": round(plan[k]["price"], 2), "basis": plan[k]["basis"]})
        ctx = {}
        if setup.get("market_structure"):  # swing
            ms = setup["market_structure"]
            ctx = {"structure": ms.get("structure"), "ema_stack": ms.get("ema_stack"),
                   "higher_timeframe": ms.get("higher_timeframe"),
                   "higher_timeframe_detail": ms.get("higher_timeframe_detail"),
                   "supporting_timeframes": ms.get("supporting")}
        elif isinstance(setup.get("htf_bias"), dict):  # day
            h = setup["htf_bias"]
            ctx = {"higher_timeframe": h.get("label"), "higher_timeframe_detail": h.get("detail")}
        zone = plan.get("entry_zone")
        facts["setup"] = {
            "name": plan["strategy_name"], "direction": plan["direction"],
            "timeframe": plan["timeframe"], "status": plan["status"],
            "status_reasons": plan["reasons"][:3],
            "context": ctx,
            "entry_trigger": price_of("entry_trigger"),
            "entry": price_of("entry"),
            "entry_zone": None if not zone else {"low": round(zone["low"], 2),
                                                 "high": round(zone["high"], 2)},
            "stop": price_of("stop"),
            "target1": price_of("target1"), "target2": price_of("target2"),
            "risk_per_share": plan["risk_per_share"],
            "reward_per_share_t1": plan["reward_per_share_t1"],
            "reward_per_share_t2": plan["reward_per_share_t2"],
            "risk_reward_t1": plan["risk_reward_t1"],
            "risk_reward_t2": plan["risk_reward_t2"],
            "min_risk_reward": plan["min_rr"],
            "confirmation": [{"condition": c["name"], "status": "met" if c["passed"]
                              else "not applicable" if c["passed"] is None else "not met",
                              "required": c["required"], "detail": c["detail"]}
                             for c in plan["confirmation"]],
            "invalidation": plan["invalidation"],
            "conflicting_signals": plan["conflicts"],
            "failed_checks": [f"{c['name']}: {c['detail']}" for c in plan["checks"]
                              if c["passed"] is False],
            "quality_score": plan["quality"]["score"] if plan.get("quality") else None,
        }
    else:
        facts["setup_note"] = "No detected setup for this symbol in this mode; status NO_TRADE."
    return facts


def engine_status(facts: dict) -> str:
    return facts["setup"]["status"] if facts.get("setup") else "NO_TRADE"


def engine_evidence(facts: dict) -> dict:
    return facts.get("strategy_evidence") or _evidence(None)


def engine_setup_name(facts: dict) -> str:
    if facts.get("setup"):
        s = facts["setup"]
        return f"{s['name']} ({s['direction']}, {s['timeframe']})"
    return "No setup"


def cache_key(facts: dict, model: str, effort: str) -> str:
    raw = json.dumps(facts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{FACTS_VERSION}|{model}|{effort}|{raw}".encode()).hexdigest()
