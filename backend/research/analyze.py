"""Research analysis of collected backtest trades (research/collect_trades.py output).

Descriptive only: no rule is changed or fitted. Statistics: Wilson 95% interval for win
rates, a seeded percentile bootstrap (5,000 resamples) for expectancy, and a t statistic.
Usage: .venv/bin/python research/analyze.py <segments.json>... > report.json
"""

from __future__ import annotations

import json
import math
import random
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
RNG_SEED = 20261015


# ----------------------------------------------------------------------------- stats

def wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(100 * (c - h), 1), round(100 * (c + h), 1)]


def boot_ci(xs: list[float], reps: int = 5000):
    if len(xs) < 2:
        return None
    rng = random.Random(RNG_SEED + len(xs))
    n = len(xs)
    means = sorted(sum(rng.choice(xs) for _ in range(n)) / n for _ in range(reps))
    return [round(means[int(0.025 * reps)], 3), round(means[int(0.975 * reps) - 1], 3)]


def summary(trades: list[dict], ci: bool = True) -> dict:
    n = len(trades)
    if not n:
        return {"n": 0}
    rs = [t["r"] for t in trades]
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    mean = sum(rs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / (n - 1)) if n > 1 else 0.0
    gl = -sum(losses)
    out = {"n": n, "win_rate": round(100 * len(wins) / n, 1),
           "win_ci": wilson(len(wins), n), "exp_r": round(mean, 3),
           "median_r": round(sorted(rs)[n // 2], 3),
           "pf": round(sum(wins) / gl, 2) if gl > 0 else None,
           "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
           "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
           "total_r": round(sum(rs), 1),
           "t": round(mean / (sd / math.sqrt(n)), 2) if n > 1 and sd > 0 else None}
    if ci:
        out["exp_ci"] = boot_ci(rs)
    return out


# ------------------------------------------------------------------------- features

def tod_bucket(ts: int) -> str:
    d = datetime.fromtimestamp(ts, NY)
    m = d.hour * 60 + d.minute
    for lo, hi, name in ((570, 600, "09:30-10:00"), (600, 660, "10:00-11:00"),
                         (660, 780, "11:00-13:00"), (780, 900, "13:00-15:00"),
                         (900, 960, "15:00-16:00")):
        if lo <= m < hi:
            return name
    return "other"


def features(t: dict, seg: str) -> dict:
    p = t.get("plan_at_entry") or {}
    sg = 1 if t["direction"] == "LONG" else -1
    risk = t["risk_per_share"]
    atr = p.get("atr")
    q = p.get("quality_components") or {}
    t1_dist_r = (t["target1"] - t["entry"]) * sg / risk if risk else None
    exit_kind = ("gap stop" if "stop (gap)" in t["exit_reason"] else
                 "stop, same candle as target" if "same candle" in t["exit_reason"] else
                 "stop" if t["exit_reason"].startswith("stop") else
                 "target1" if "target1" in t["exit_reason"] else
                 "target2" if "target2" in t["exit_reason"] else t["exit_reason"])
    d = datetime.fromtimestamp(t["entry_time"], NY)
    return {
        "seg": seg, "mode": t["mode"], "strategy": t["strategy"], "ticker": t["ticker"],
        "timeframe": t["timeframe"], "direction": t["direction"], "regime": t.get("regime"),
        "tod": tod_bucket(t["entry_time"]) if t["mode"] == "day" else d.strftime("%a"),
        "quarter": f"{d.year}-Q{(d.month - 1) // 3 + 1}",
        "r": t["r"], "gross_r": t["gross_r"], "cost_r": round(t["gross_r"] - t["r"], 4),
        "mfe_r": t["mfe_r"], "mae_r": t["mae_r"], "bars": t["bars_held"],
        "exit": exit_kind, "planned_rr": t["planned_rr"], "t1_dist_r": t1_dist_r,
        "quality": t["quality"], "vol": t.get("volume_confirmation"),
        "risk_pct": round(100 * risk / t["entry"], 4),
        "stop_atr": round(risk / atr, 3) if atr else None,
        "htf_points": q.get("higher_timeframe"), "trend_points": q.get("trend_structure"),
        "relvol_points": q.get("relative_volume"), "conflicts": len(p.get("conflicts") or []),
        "entry_lag_bars": None, "t1_source": p.get("target1_source"),
        "entry": t["entry"], "open": t["open"], "plan_entry": t["plan_entry"],
        "stop": t["stop"], "target1": t["target1"], "risk": risk, "id": t["id"],
        "entry_time": t["entry_time"], "exit_time": t["exit_time"],
        "signal_time": t["signal_time"],
    }


def by(rows, key, min_n=1):
    g = defaultdict(list)
    for r in rows:
        g[r[key]].append(r)
    return {str(k): summary(v) for k, v in sorted(g.items(), key=lambda kv: str(kv[0]))
            if len(v) >= min_n}


def hist(xs, edges):
    out = []
    for lo, hi in zip(edges, edges[1:]):
        out.append({"lo": lo, "hi": hi, "n": sum(1 for x in xs if lo <= x < hi)})
    return out


def quantiles(xs, qs=(0.1, 0.25, 0.5, 0.75, 0.9)):
    s = sorted(xs)
    return {f"p{int(q * 100)}": round(s[min(len(s) - 1, int(q * len(s)))], 3) for q in qs} \
        if s else {}


# ---------------------------------------------------------------------------- main

def analyze(rows: list[dict]) -> dict:
    out = {"overall": summary(rows)}
    for key in ("strategy", "ticker", "timeframe", "direction", "regime", "tod", "exit",
                "vol", "t1_source", "quarter"):
        out[f"by_{key}"] = by(rows, key)
    out["by_strategy_direction"] = by(
        [r | {"sd": f"{r['strategy']} {r['direction']}"} for r in rows], "sd")
    out["by_strategy_timeframe"] = by(
        [r | {"st": f"{r['strategy']} {r['timeframe']}"} for r in rows], "st")
    gross = [r | {"r": r["gross_r"]} for r in rows]
    for key in ("strategy", "timeframe", "ticker", "direction", "tod"):
        out[f"gross_by_{key}"] = by(gross, key)
    out["gross_by_strategy_timeframe"] = by(
        [r | {"st": f"{r['strategy']} {r['timeframe']}"} for r in gross], "st")
    rb = lambda r: ("<0.15%" if r["risk_pct"] < 0.15 else "0.15-0.25%" if r["risk_pct"] < 0.25
                    else "0.25-0.5%" if r["risk_pct"] < 0.5 else ">=0.5%")  # noqa: E731
    out["by_risk_pct"] = by([r | {"rb": rb(r)} for r in rows], "rb")
    out["gross_by_risk_pct"] = by([r | {"rb": rb(r)} for r in gross], "rb")
    rs = [r["r"] for r in rows]
    out["dist_r"] = hist(rs, [-3, -1.5, -1.1, -0.9, -0.5, 0, 0.5, 1, 1.5, 2, 3, 5, 50])
    out["q_r"] = quantiles(rs)
    out["q_mfe"] = quantiles([r["mfe_r"] for r in rows])
    out["q_mae"] = quantiles([r["mae_r"] for r in rows])
    out["q_bars"] = quantiles([r["bars"] for r in rows])
    out["q_planned_rr"] = quantiles([r["planned_rr"] for r in rows if r["planned_rr"]])
    out["q_t1_dist_r"] = quantiles([r["t1_dist_r"] for r in rows if r["t1_dist_r"]])
    out["q_cost_r"] = quantiles([r["cost_r"] for r in rows])
    out["q_stop_atr"] = quantiles([r["stop_atr"] for r in rows if r["stop_atr"]])
    out["q_risk_pct"] = quantiles([r["risk_pct"] for r in rows])
    # cost sensitivity: gross (no costs), as modelled, double the modelled costs
    out["cost_sensitivity"] = {
        "gross": summary([r | {"r": r["gross_r"]} for r in rows], ci=True),
        "modelled": summary(rows, ci=False),
        "double": summary([r | {"r": r["r"] - r["cost_r"]} for r in rows], ci=True)}
    losers = [r for r in rows if r["r"] <= 0]
    out["losers"] = {
        "n": len(losers),
        "by_exit": {k: len(v) for k, v in _group(losers, "exit").items()},
        "mfe_ge_0.5R": sum(1 for r in losers if r["mfe_r"] >= 0.5),
        "mfe_ge_1R": sum(1 for r in losers if r["mfe_r"] >= 1.0),
        "stopped_within_2_bars": sum(1 for r in losers if r["exit"].startswith("stop")
                                     and r["bars"] <= 2),
        "q_mfe": quantiles([r["mfe_r"] for r in losers]),
        "q_bars": quantiles([r["bars"] for r in losers]),
    }
    winners = [r for r in rows if r["r"] > 0]
    out["winners"] = {"n": len(winners), "q_mae": quantiles([r["mae_r"] for r in winners]),
                      "by_exit": {k: len(v) for k, v in _group(winners, "exit").items()}}
    # MFE reach: how often price went at least x R in favour (before the exit)
    out["mfe_reach"] = {f">={x}R": round(100 * sum(1 for r in rows if r["mfe_r"] >= x)
                                         / len(rows), 1) for x in (0.5, 1, 1.5, 2, 3)}
    # T1 distance vs reach
    reach_t1 = [r for r in rows if r["t1_dist_r"] and r["mfe_r"] >= r["t1_dist_r"]]
    out["reached_t1_by_mfe"] = len(reach_t1)
    # Internal consistency: MFE beyond T1 (+ a tick) but exit was not a target and not a
    # same-candle stop -> potential execution bug.
    sus = [r["id"] for r in rows if r["t1_dist_r"] and r["mfe_r"] > r["t1_dist_r"] + 0.05
           and not r["exit"].startswith("target") and "same candle" not in r["exit"]]
    out["consistency_suspects"] = sus
    # quality score vs outcome
    out["by_quality"] = by([r | {"qb": ("<50" if r["quality"] < 50 else "50-60" if
                                        r["quality"] < 60 else "60-70" if r["quality"] < 70
                                        else ">=70")} for r in rows], "qb")
    out["by_stop_atr"] = by([r | {"sb": ("<0.5" if r["stop_atr"] < 0.5 else "0.5-1" if
                                         r["stop_atr"] < 1 else "1-2" if r["stop_atr"] < 2
                                         else ">=2")} for r in rows if r["stop_atr"]], "sb")
    out["by_htf"] = by([r | {"h": ("aligned" if r["htf_points"] and r["htf_points"] >= 14
                                   else "neutral" if r["htf_points"] and r["htf_points"] > 0
                                   else "opposed")} for r in rows], "h")
    out["by_trend_struct"] = by([r | {"h": ("aligned" if r["trend_points"] and
                                            r["trend_points"] >= 14 else "mixed/unknown"
                                            if r["trend_points"] and r["trend_points"] > 0
                                            else "opposed")} for r in rows], "h")
    return out


def _group(rows, key):
    g = defaultdict(list)
    for r in rows:
        g[r[key]].append(r)
    return g


def main() -> None:
    rows, meta = [], {}
    for path in sys.argv[1:]:
        d = json.load(open(path))
        for seg, s in d["segments"].items():
            n_sig = n_trades = 0
            skip = defaultdict(int)
            for run in s["runs"]:
                n_sig += run["signals_detected"]
                n_trades += len(run["trades"])
                for sk in run["skipped"]:
                    skip[sk["reason"].split(":")[0]] += 1
                rows += [features(t, seg) for t in run["trades"]]
            meta[seg] = {"signals": n_sig, "trades": n_trades, "skipped": dict(skip),
                         "spans": [(r["start"], r["end"]) for r in s["runs"]],
                         "symbols": s["symbols"]}
    report = {"meta": meta, "segments": {}}
    for seg in sorted({r["seg"] for r in rows}):
        report["segments"][seg] = analyze([r for r in rows if r["seg"] == seg])
    json.dump(report, sys.stdout, indent=1, default=str)
    with open("/dev/stderr", "w") as f:
        f.write(f"{len(rows)} trades analysed\n")


if __name__ == "__main__":
    main()
