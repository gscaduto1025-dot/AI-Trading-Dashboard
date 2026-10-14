#!/usr/bin/env python3
"""API smoke test against a RUNNING backend (used by scripts/integration.sh).

Exercises every feature area through HTTP, as the dashboard does. Standard library only.
Usage: python3 scripts/smoke_api.py [base_url]   (default http://127.0.0.1:8765)
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765").rstrip("/")
results: list[tuple[str, bool, str]] = []


def call(method: str, path: str, body=None, expect=(200,)):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    if status not in expect:
        raise AssertionError(f"{method} {path} -> {status}: {raw[:200]!r}")
    ctype_json = raw[:1] in (b"{", b"[")
    return json.loads(raw) if ctype_json else raw.decode()


def check(name: str, fn) -> None:
    try:
        detail = fn() or ""
        results.append((name, True, str(detail)))
    except Exception as exc:  # report every failure, keep going
        results.append((name, False, f"{type(exc).__name__}: {exc}"))


def health():
    h = call("GET", "/api/health")
    assert h["status"] == "ok" and h["live_trading"] is False
    return f"data_mode={h['data_mode']} demo={h['is_demo']} ai={h['ai_enabled']}"


def market():
    w = call("GET", "/api/watchlist")
    assert w["quotes"] and all("price" in q for q in w["quotes"])
    sym = w["quotes"][0]["symbol"]
    b = call("GET", f"/api/bars/{sym}?timeframe=5Min")
    assert b["bars"] and b["status"]["freshness"]
    return f"{len(w['quotes'])} quotes, {len(b['bars'])} bars for {sym}"


def analysis():
    a = call("GET", "/api/analysis/SPY?mode=day")
    lv = call("GET", "/api/levels/SPY?mode=day&timeframe=5Min")
    assert a["bias"] and lv["levels"]
    return f"bias={a['bias']['label']} levels={len(lv['levels'])}"


def strategies():
    d = call("GET", "/api/strategies/setups")
    s = call("GET", "/api/swing/setups")
    p = call("GET", "/api/plans/SPY?mode=day")
    statuses = {x["status"] for x in d["setups"] + s["setups"]}
    assert statuses <= {"TRIGGERED", "WATCHING", "NO_TRADE"}
    return f"day={len(d['setups'])} swing={len(s['setups'])} plans={len(p['plans'])}"


def scanner():
    call("POST", "/api/scanner/run")
    j = call("GET", "/api/scanner")
    assert j["watchlist"] and len(j["tickers"]) == len(j["watchlist"])
    return f"{len(j['tickers'])} tickers, {j['total_rows']} rows"


def alerts():
    a = call("GET", "/api/alerts")
    p = call("GET", "/api/alerts/preferences")
    assert "counts" in a and p["types"]
    return f"{a['counts']['total']} alerts"


def ai():
    r = call("GET", "/api/ai/SPY?mode=day")
    assert "available" in r
    return "available" if r["available"] else f"unavailable: {r.get('reason', '')[:60]}"


def options():
    c = call("GET", "/api/options/capabilities")
    o = call("GET", "/api/options/SPY?mode=day")
    assert c["order_execution"] is False and "available" in o
    return "available" if o["available"] else "unavailable (demo or no keys)"


def journal():
    t = call("POST", "/api/journal/paper/trades", {
        "ticker": "SPY", "direction": "SHORT", "planned_stop": 102,
        "fills": [{"side": "SELL", "quantity": 10, "price": 100, "fees": 1,
                   "executed_at": "2026-01-05T10:00:00-05:00"}]})
    t = call("POST", f"/api/journal/paper/trades/{t['id']}/fills",
             {"side": "BUY", "quantity": 4, "price": 98, "executed_at": "2026-01-05T11:00:00-05:00"})
    t = call("POST", f"/api/journal/paper/trades/{t['id']}/fills",
             {"side": "BUY", "quantity": 6, "price": 99, "executed_at": "2026-01-05T12:00:00-05:00"})
    assert t["pnl"]["status"] == "closed" and t["pnl"]["realized_net"] == 13.0  # 8 + 6 - 1
    assert t["pnl"]["partial_exits"] == 1 and t["pnl"]["r_multiple"] == 0.65
    call("POST", "/api/journal/backtest/trades", {}, expect=(400,))
    call("DELETE", f"/api/journal/paper/trades/{t['id']}")
    return "short with partial exit: net 13.00, 0.65R"


def backtest():
    r = call("POST", "/api/backtests", {"mode": "swing", "data_source": "bundled",
                                        "symbols": ["AAPL"], "start": "2015-09-01",
                                        "end": "2017-02-16"})
    for _ in range(120):
        j = call("GET", f"/api/backtests/{r['id']}")
        if j["status"] != "running":
            break
        time.sleep(1)
    assert j["status"] == "done", j.get("error")
    rep = j["report"]
    assert rep["verdict"]["label"] != "POSITIVE EVIDENCE (hypothetical)"  # tiny sample
    tid = rep["trades"][0]["id"]
    ch = call("GET", f"/api/backtests/{r['id']}/trades/{tid}/chart")
    assert ch["simulated"] and ch["bars"]
    return f"{rep['overall']['trades']} trades, verdict {rep['verdict']['label']}"


for name, fn in [("health", health), ("market data", market), ("analysis + levels", analysis),
                 ("strategies + plans", strategies), ("scanner", scanner), ("alerts", alerts),
                 ("Claude explanation", ai), ("options", options), ("trade log", journal),
                 ("backtest", backtest)]:
    check(name, fn)

width = max(len(n) for n, _, _ in results)
for name, ok, detail in results:
    print(f"  {'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
failed = [n for n, ok, _ in results if not ok]
print(f"API smoke: {len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
