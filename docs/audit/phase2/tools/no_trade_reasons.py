import json, sys, re, collections, urllib.request
# Usage: run against a backend started on 127.0.0.1:8810 (see docs/audit/PHASE2_REPORT.md).
B = "http://127.0.0.1:8810"
DATA = re.compile(r"stale|gap|no trades|candle slots|unavailable|insufficient (daily )?history|"
                  r"unverified|lagging|missing|invalid data|data:|feed", re.I)
def get(p):
    with urllib.request.urlopen(B + p, timeout=600) as r: return json.load(r)
out = {}
for mode, path in (("day", "/api/strategies/setups"), ("swing", "/api/swing/setups")):
    s = get(path)
    per = collections.defaultdict(lambda: {"total": 0, "status": collections.Counter(),
                                           "data_related": 0, "genuine": 0,
                                           "reasons": collections.Counter()})
    for x in s["setups"]:
        p = per[x["strategy"]]
        p["total"] += 1
        p["status"][x["status"]] += 1
        if x["status"] == "NO_TRADE":
            r = (x.get("reasons") or ["(no reason)"])[0]
            p["reasons"][re.sub(r"[-\d.,]+", "#", r)[:110]] += 1
            p["data_related" if DATA.search(" ".join(x.get("reasons") or [])) else "genuine"] += 1
    out[mode] = {"evaluated_at": s["evaluated_at"], "strategies": {
        k: {"total": v["total"], "status": dict(v["status"]), "no_trade_data_related": v["data_related"],
            "no_trade_genuine": v["genuine"], "top_reasons": v["reasons"].most_common(4)}
        for k, v in sorted(per.items())}}
json.dump(out, open(sys.argv[1], "w"), indent=1, default=str)
for mode, m in out.items():
    print(mode, m["evaluated_at"])
    for k, v in m["strategies"].items():
        print(f"  {k}: {v['total']} {v['status']} data-related NO_TRADE={v['no_trade_data_related']} genuine={v['no_trade_genuine']}")
        for r, n in v["top_reasons"][:2]: print(f"      {n}x {r}")
