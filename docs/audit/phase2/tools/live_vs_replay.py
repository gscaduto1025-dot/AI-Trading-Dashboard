import json, sys, urllib.request, urllib.parse
# Usage: run against a backend started on 127.0.0.1:8810 (see docs/audit/PHASE2_REPORT.md).
B = "http://127.0.0.1:8810"
def get(p):
    with urllib.request.urlopen(B + p, timeout=600) as r: return json.load(r)
IGNORE = {"is_demo", "evaluated_at"}
def norm(s): return {k: v for k, v in s.items() if k not in IGNORE}
out = {}
for kind, path in (("day", "/api/strategies/setups"), ("swing", "/api/swing/setups")):
    live = get(path)
    when = live["evaluated_at"]
    rep = get(path + "?as_of=" + urllib.parse.quote(when))
    L = {s["id"]: norm(s) for s in live["setups"]}
    R = {s["id"]: norm(s) for s in rep["setups"]}
    same_ids = set(L) == set(R)
    diffs = []
    for i in sorted(set(L) & set(R)):
        if json.dumps(L[i], sort_keys=True, default=str) != json.dumps(R[i], sort_keys=True, default=str):
            keys = [k for k in L[i] if json.dumps(L[i].get(k), sort_keys=True, default=str) != json.dumps(R[i].get(k), sort_keys=True, default=str)]
            diffs.append({"id": i, "ticker": L[i]["ticker"], "strategy": L[i]["strategy"], "keys": keys[:8]})
    out[kind] = {"evaluated_at": when, "replay_evaluated_at": rep["evaluated_at"], "live_setups": len(L),
                 "replay_setups": len(R), "same_ids": same_ids, "only_live": sorted(set(L) - set(R))[:5],
                 "only_replay": sorted(set(R) - set(L))[:5], "identical": len(L) - len(diffs) if same_ids else None,
                 "differing": diffs[:12], "n_differing": len(diffs)}
print(json.dumps(out, indent=1, default=str))
