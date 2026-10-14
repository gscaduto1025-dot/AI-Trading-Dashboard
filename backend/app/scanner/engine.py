"""Multi-stock opportunity scanner.

Every row is a setup produced by the day-trading or swing-trading strategy engine and
planned by the trade-planning engine; the scanner only collects, annotates and ranks
them. It never invents a setup, a price or a status, and it never calls Claude. After a
scan, the alert engine (app.alerts) diffs the snapshot against its persisted state.

Scheduling: `due()` compares an "eligible candle" signature - the watchlist, the number
of completed 5-minute candles in the current regular session and the last completed daily
session - with the one of the last scan. The server-side loop rescans only when that
signature changes (a new 5-minute / 15-minute / hourly / daily candle can exist), so
unchanged data is never recomputed and API calls stay inside the feed's rate limit (the
market-data client's token bucket paces every request; candle sets are cached per
timeframe; strategy results are cached per candle).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from ..data.base import DEFAULT_TIMEFRAME, DataError
from ..market.calendar import NY

log = logging.getLogger("tradescope.scanner")

PATTERN = {  # strategy -> setup family shown and filterable in the scanner
    "breakout_retest": "retest", "opening_range_breakout": "breakout",
    "liquidity_sweep_reversal": "reversal", "trend_pullback": "pullback",
    "sr_rejection": "reversal", "swing_breakout": "breakout",
    "swing_trend_continuation": "pullback", "swing_consolidation_breakout": "breakout",
    "swing_sr_reversal": "reversal", "swing_trend_reversal": "reversal",
}
STATUS_ORDER = {"TRIGGERED": 0, "WATCHING": 1, "NO_TRADE": 2}
NEAR_ATR = 0.5  # a key level within this many ATRs of price counts as "near"
FRESH_OK = ("fresh", "demo")
SR_CATEGORIES = ("zone", "reference", "swing", "liquidity")


def _iso(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, NY).isoformat() if ts else None


def _num(plan: dict | None, k: str):
    return plan[k]["price"] if plan and plan.get(k) else None


# ----------------------------------------------------------------------------- rows

def setup_row(s: dict, mode: str, price: float | None, bias: str | None,
              data: dict) -> dict:
    """One scanner row, copied from an engine setup and its plan (no recalculation)."""
    plan = s.get("plan") or {}
    trig = _num(plan, "entry_trigger")
    sgn = 1 if s["direction"] == "LONG" else -1
    status = s["status"]
    fresh = data.get("freshness") in FRESH_OK
    issues = []
    if status == "NO_TRADE":
        issues = [c["name"] + ": " + c["detail"] for c in plan.get("checks", [])
                  if c["passed"] is False] or list(s.get("reasons", []))[:2]
    last = s.get("last_trigger") or {}  # swing: a trigger that already resolved
    trig_time = s.get("trigger_time") or last.get("time")
    outcome = s.get("outcome") or last.get("outcome")
    if not fresh:
        issues.insert(0, f"Data {data.get('freshness')}: {data.get('reason') or 'not usable'}")
    return {
        "id": s["id"], "ticker": s["ticker"], "mode": mode, "price": price,
        "market_bias": bias, "setup": s["strategy_name"], "strategy": s["strategy"],
        "pattern": PATTERN.get(s["strategy"], "other"), "timeframe": s["timeframe"],
        "direction": s["direction"], "side": "bullish" if s["direction"] == "LONG" else "bearish",
        "entry_trigger": trig, "entry": _num(plan, "entry"), "stop": _num(plan, "stop"),
        "target1": _num(plan, "target1"), "target2": _num(plan, "target2"),
        "risk_reward": plan.get("risk_reward_t1"),
        "quality": (plan.get("quality") or {}).get("score"),
        "status": status,
        "meets_requirements": status != "NO_TRADE",
        "actionable": status == "TRIGGERED" and fresh,
        "requirement_issues": issues,
        "reason": (s.get("reasons") or [""])[0],
        "distance_to_trigger_pct": (round(100 * sgn * (trig - price) / price, 2)
                                    if trig is not None and price else None),
        "last_updated": s.get("evaluated_at"),
        "session_date": s.get("session_date"),
        "key_level": next(({"label": l.get("label"), "low": l.get("low"), "high": l.get("high"),
                            "timeframe": l.get("timeframe")}
                           for l in s.get("supporting_levels") or []), None),
        "trigger_time": trig_time,
        "outcome": (outcome or {}).get("state"),
        "outcome_time": (outcome or {}).get("time"),
        "entry_conditions": [{"name": c["name"], "passed": c.get("passed"),
                              "detail": c.get("detail")} for c in plan.get("confirmation", [])],
        "invalidation": list(plan.get("invalidation") or s.get("invalidation") or []),
        "data_freshness": data.get("freshness"),
        "data_as_of": data.get("as_of"),
        "last_candle": data.get("last_candle"),
    }


def include_row(s: dict) -> bool:
    """Detected setups only: active ones, and patterns that were found but fail a
    requirement (they have an objective trigger). Plain 'no pattern' rows are dropped."""
    if s["status"] != "NO_TRADE":
        return True
    plan = s.get("plan") or {}
    return plan.get("entry_trigger") is not None


def rank_key(r: dict):
    return (STATUS_ORDER[r["status"]], -(r["quality"] or 0), -(r["risk_reward"] or 0),
            r["ticker"], r["mode"], r["setup"], r["direction"])


def filter_rows(rows: list[dict], *, mode: str | None = None, side: str | None = None,
                status: str | None = None, min_rr: float | None = None,
                min_quality: float | None = None, strategy: str | None = None,
                include_no_trade: bool = True) -> list[dict]:
    """Scanner filters. `side`: bullish|bearish. `status`: TRIGGERED|WATCHING|NO_TRADE
    (comma list allowed). `strategy`: a strategy id or a setup family (breakout, retest,
    pullback, reversal)."""
    statuses = {x.strip().upper() for x in status.split(",")} if status else None
    out = []
    for r in rows:
        if mode and r["mode"] != mode:
            continue
        if side and r["side"] != side:
            continue
        if statuses and r["status"] not in statuses:
            continue
        if not include_no_trade and r["status"] == "NO_TRADE":
            continue
        if min_rr is not None and (r["risk_reward"] is None or r["risk_reward"] < min_rr):
            continue
        if min_quality is not None and (r["quality"] is None or r["quality"] < min_quality):
            continue
        if strategy and strategy not in (r["strategy"], r["pattern"]):
            continue
        out.append(r)
    return out


# --------------------------------------------------------------------------- levels

def _proximity(analysis: dict) -> dict:
    """Nearest important support and resistance (key-level engine) and whether price is
    within NEAR_ATR x ATR of them."""
    price = analysis["price"]
    atr = (analysis.get("indicators") or {}).get("atr") or 0
    best = {"support": None, "resistance": None}
    for l in analysis.get("key_levels") or []:
        if l.get("category") not in SR_CATEGORIES:
            continue  # moving averages / VWAP are not support-resistance zones here
        inside = l["low"] <= price <= l["high"]
        if inside:  # price inside a zone: classify it by the zone's role
            role = l.get("type") or ""
            side = ("resistance" if role == "RESISTANCE_ZONE" or "resistance" in l["label"].lower()
                    else "support")
            edge = price
        elif l["high"] < price:
            side, edge = "support", l["high"]
        else:
            side, edge = "resistance", l["low"]
        dist = abs(price - edge)
        cur = best[side]
        if cur is None or dist < cur["distance"]:
            best[side] = {"label": l["label"], "low": l["low"], "high": l["high"],
                          "timeframe": l.get("timeframe"), "strength": l.get("strength"),
                          "distance": round(dist, 4),
                          "distance_pct": round(100 * dist / price, 2) if price else None,
                          "distance_atr": round(dist / atr, 2) if atr else None,
                          "near": bool(atr) and dist <= NEAR_ATR * atr, "inside": inside}
    return best


def _data_state(service, symbol: str, mode: str) -> dict:
    tf = DEFAULT_TIMEFRAME[mode]
    try:
        c = service.market.get_candles(symbol, tf, "regular", 300)
    except DataError as exc:
        return {"freshness": "unavailable", "reason": str(exc), "as_of": None,
                "last_candle": None, "timeframe": tf}
    block = c.analysis_block()
    done = c.completed_bars
    freshness = c.status.get("freshness")
    if block and block[0] != "INSUFFICIENT_DATA" and freshness in FRESH_OK:
        freshness = "invalid"  # integrity failure
    return {"freshness": freshness, "reason": block[1] if block else c.status.get("reason"),
            "as_of": c.status.get("as_of"), "timeframe": tf,
            "last_candle": _iso(done[-1].time) if done else None}


# -------------------------------------------------------------------------- scanner

class OpportunityScanner:
    def __init__(self, service) -> None:
        self.service = service
        self.snapshot: dict | None = None
        self.scan_count = 0
        self._signature: tuple | None = None
        self._lock = threading.Lock()
        self.last_error: str | None = None

    def signature(self) -> tuple:
        market = self.service.market
        cut = market.cutoff()
        cal = market.calendar
        sess = cal.last_session_started(cut, "regular")
        if sess.open <= cut < sess.close:
            intraday = int((cut - sess.open).total_seconds() // 300)  # completed 5m candles
        else:
            intraday = "closed"
        daily = cal.last_completed_session(cut).day.isoformat()
        return (tuple(market.symbols), sess.day.isoformat(), intraday, daily)

    def due(self) -> bool:
        return self.snapshot is None or self.signature() != self._signature

    def run(self, force: bool = False, trigger: str = "scheduled") -> dict:
        """Scan the watchlist if new eligible candles exist (or `force`). Returns the
        latest snapshot either way."""
        with self._lock:
            sig = self.signature()
            if not force and self.snapshot is not None and sig == self._signature:
                return self.snapshot
            started = time.monotonic()
            self.snapshot = self._scan(trigger)
            self.snapshot["duration_ms"] = int((time.monotonic() - started) * 1000)
            self.snapshot["signature"] = list(sig)
            self._signature = sig
            self.scan_count += 1
            alerts = getattr(self.service, "alerts", None)
            self.snapshot["new_alerts"] = []
            if alerts is not None:
                try:  # a failure here must never lose the scan itself
                    self.snapshot["new_alerts"] = alerts.process(self.snapshot)
                except Exception as exc:
                    log.exception("Alert engine failed")
                    self.snapshot["errors"].append({"symbol": "*", "mode": "alerts",
                                                    "code": "ALERT_ENGINE", "error": str(exc)})
            return self.snapshot

    # ---------------------------------------------------------------------- the scan

    def _scan(self, trigger: str) -> dict:
        svc = self.service
        market = svc.market
        started_at = datetime.now(NY).isoformat(timespec="seconds")
        errors: list[dict] = []
        results: list[dict] = []
        states: list[dict] = []
        try:
            quotes = {q["symbol"]: q for q in market.get_quotes()}
        except DataError as exc:
            quotes = {}
            errors.append({"symbol": "*", "mode": "quotes", "code": exc.code, "error": str(exc)})
        engine: dict[str, dict] = {}
        for mode, call in (("day", svc.strategy_setups), ("swing", svc.swing_setups)):
            try:
                engine[mode] = call()  # also appends new triggers to the signal log
            except DataError as exc:
                engine[mode] = {"setups": [], "symbols": {}}
                errors.append({"symbol": "*", "mode": mode, "code": exc.code, "error": str(exc)})
        tickers, rows = [], []
        for sym in market.symbols:
            q = quotes.get(sym) or {}
            info = {"ticker": sym, "price": q.get("price"), "change_pct": q.get("change_pct"),
                    "price_time": q.get("price_time"), "data": {}, "bias": {}, "levels": {},
                    "flags": [], "counts": {"TRIGGERED": 0, "WATCHING": 0, "NO_TRADE": 0}}
            for mode in ("day", "swing"):
                data = _data_state(svc, sym, mode)
                info["data"][mode] = data
                if data["freshness"] not in FRESH_OK:
                    info["flags"].append(f"{mode}: data {data['freshness']} - "
                                         f"{data['reason'] or 'not usable'}")
                analysis = None
                try:
                    analysis = svc.analysis(sym, mode, DEFAULT_TIMEFRAME[mode])
                except Exception as exc:  # AnalysisBlocked / DataError: flagged, no setups
                    msg = getattr(exc, "message", str(exc))
                    errors.append({"symbol": sym, "mode": mode,
                                   "code": getattr(exc, "code", "ERROR"), "error": msg})
                if analysis:
                    info["bias"][mode] = analysis["bias"]["label"].lower()
                    info["levels"][mode] = _proximity(analysis)
                    if info["price"] is None:
                        info["price"] = analysis["price"]
                    svc.db.record_setup(analysis, svc.is_demo)  # recent-setups feed
                    s = analysis["setup"]
                    results.append({"symbol": sym, "mode": mode, "timeframe": analysis["timeframe"],
                                    "status": s["status"], "title": s["title"],
                                    "direction": s["direction"], "risk_reward": s["risk_reward"],
                                    "confidence": s["confidence"]})
                state = engine[mode]["symbols"].get(sym)
                if state and state.get("status") == "unavailable":
                    info["flags"].append(f"{mode}: MARKET DATA UNAVAILABLE - {state.get('error')}")
                for s in engine[mode]["setups"]:
                    if s["ticker"] != sym:
                        continue
                    row = setup_row(s, mode, info["price"], info["bias"].get(mode), data)
                    states.append(row)  # every evaluated setup, for the alert engine
                    if not include_row(s):
                        continue
                    rows.append(row)
                    info["counts"][row["status"]] += 1
            info["flagged"] = bool(info["flags"])
            tickers.append(info)
        rows.sort(key=rank_key)
        svc.last_scan = int(time.time())
        return {
            "scan_id": self.scan_count + 1, "trigger": trigger, "started_at": started_at,
            "finished_at": datetime.now(NY).isoformat(timespec="seconds"),
            "is_demo": svc.is_demo, "feed": market.feed_info(),
            "market_phase": market.market_status().get("phase"),
            "watchlist": list(market.symbols), "tickers": tickers, "rows": rows,
            "errors": errors, "results": results, "setup_states": states,
            "strategies": sorted({r["strategy"] for r in rows}),
        }
