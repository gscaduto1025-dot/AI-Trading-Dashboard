"""Report for the frozen-protocol 50-ticker validation (research/validation/protocol.json).

By mode, strategy, timeframe, ticker, sector and direction: trades, win rate (Wilson CI),
net expectancy in R (bootstrap CI), profit factor, max drawdown of cumulative R (1R per
trade, ordered by exit), and transaction-cost sensitivity (modelled costs scaled x0 to x2).
Pre-registered secondary endpoints get Holm-adjusted p-values (normal approximation).

Usage (from backend/): .venv/bin/python research/validation_report.py <run.jsonl>... > out.json
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze import boot_ci, features, summary  # noqa: E402

VAL = Path(__file__).resolve().parent / "validation"
COST_K = (0.0, 0.2, 0.25, 0.5, 1.0, 2.0)  # x0.2 ~ the measured NBBO entry cost
MIN_N = 30


def max_dd(rows: list[dict]) -> float:
    peak = cum = dd = 0.0
    for r in sorted(rows, key=lambda r: r["exit_time"]):
        cum += r["r"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return round(dd, 2)


def pval(xs: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    if sd == 0:
        return None
    return round(math.erfc(abs(m / (sd / math.sqrt(n))) / math.sqrt(2)), 5)


def block(rows: list[dict], costs: bool = False) -> dict:
    if not rows:
        return {"n": 0}
    s = summary(rows)
    s["max_dd_r"] = max_dd(rows)
    s["gross_exp_r"] = round(sum(r["gross_r"] for r in rows) / len(rows), 3)
    s["mean_cost_r"] = round(sum(r["cost_r"] for r in rows) / len(rows), 3)
    if len(rows) < MIN_N:
        s["anecdotal"] = True
    if costs:
        s["cost_sensitivity"] = {}
        for k in COST_K:
            xs = [r["gross_r"] - k * r["cost_r"] for r in rows]
            s["cost_sensitivity"][f"x{k:g}"] = {
                "exp_r": round(sum(xs) / len(xs), 3), "exp_ci": boot_ci(xs),
                "pf": _pf(xs), "max_dd_r": max_dd([r | {"r": x} for r, x in zip(rows, xs)])}
    return s


def _pf(xs):
    gl = -sum(x for x in xs if x <= 0)
    return round(sum(x for x in xs if x > 0) / gl, 2) if gl > 0 else None


def by(rows, key, costs=False):
    g: dict = {}
    for r in rows:
        g.setdefault(r[key], []).append(r)
    return {k: block(v, costs) for k, v in sorted(g.items(), key=lambda kv: -len(kv[1]))}


def holm(ps: dict) -> dict:
    items = sorted((p, k) for k, p in ps.items() if p is not None)
    m, out, run = len(items), {}, 0.0
    for i, (p, k) in enumerate(items):
        run = max(run, min(1.0, (m - i) * p))
        out[k] = round(run, 5)
    return out


def main() -> None:
    recs = [json.loads(x) for p in sys.argv[1:] for x in open(p)]
    sectors = {}
    for f in VAL.glob("universe-*.json"):
        mode = f.name.split("-")[1]
        for r in json.loads(f.read_text())["selected"]:
            sectors[(mode, r["symbol"])] = r["sector"]
    rows, data = [], {}
    for rec in recs:
        d = data.setdefault(rec["mode"], {"symbols": set(), "data_errors": [], "signals": 0,
                                         "not_live": 0, "skipped": 0, "runtime_s": 0,
                                         "rules_hash": set()})
        d["symbols"].add(rec["symbol"])
        d["data_errors"] += [e | {"run": rec["run"]} for e in rec["data_errors"]]
        d["signals"] += rec["signals_detected"]
        d["not_live"] += len(rec["not_live"])
        d["skipped"] += len(rec["skipped"])
        d["runtime_s"] += rec["seconds"]
        d["rules_hash"].add(rec["rules_hash"])
        for t in rec["trades"]:
            f = features(t, rec["mode"])
            f["sector"] = sectors.get((rec["mode"], t["ticker"]), "?")
            f["strat_tf"] = f"{f['strategy']} {f['timeframe']}"
            rows.append(f)
    out = {"data": {m: d | {"symbols": len(d["symbols"]), "rules_hash": sorted(d["rules_hash"])}
                    for m, d in data.items()}, "modes": {}}
    for mode in sorted({r["mode"] for r in rows}):
        rs = [r for r in rows if r["mode"] == mode]
        h2 = [r for r in rs if r["htf_points"] and r["htf_points"] >= 14]
        h3 = [r for r in rs if r["quality"] >= 70]
        tests = {"gross_all": pval([r["gross_r"] for r in rs]),
                 "gross_h2_htf_aligned": pval([r["gross_r"] for r in h2]),
                 "gross_h3_quality70": pval([r["gross_r"] for r in h3]),
                 "net_measured_cost_x0.2": pval([r["gross_r"] - 0.2 * r["cost_r"] for r in rs])}
        half = sorted(rs, key=lambda r: r["entry_time"])
        k = len(half) // 2
        out["modes"][mode] = {
            "overall": block(rs, costs=True),
            "halves": {"first": block(half[:k]), "second": block(half[k:])},
            "preregistered": {"h2_htf_aligned": block(h2, costs=True),
                              "h3_quality_ge_70": block(h3, costs=True),
                              "p_two_sided": tests, "p_holm": holm(tests)},
            "by_strategy": by(rs, "strategy", costs=True),
            "by_timeframe": by(rs, "timeframe"),
            "by_strategy_timeframe": by(rs, "strat_tf"),
            "by_sector": by(rs, "sector", costs=True),
            "by_direction": by(rs, "direction"),
            "by_ticker": by(rs, "ticker"),
            "by_exit": by(rs, "exit"),
            "by_volume_confirmation": by(rs, "vol"),
        }
    json.dump(out, sys.stdout, indent=1, default=sorted)


if __name__ == "__main__":
    main()
