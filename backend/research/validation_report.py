"""Report for the frozen-protocol 50-ticker validation (research/validation/protocol.json).

By mode, strategy, timeframe, ticker, sector and direction: trades, win rate (Wilson CI),
net expectancy in R (bootstrap CI), profit factor, max drawdown of cumulative R (1R per
trade, ordered by exit), and transaction-cost sensitivity (modelled costs scaled x0 to x2).
Pre-registered secondary endpoints get Holm-adjusted p-values (normal approximation).

Usage (from backend/): .venv/bin/python research/validation_report.py <run.jsonl>... > out.json
"""

from __future__ import annotations

import gzip
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze  # noqa: E402
from analyze import features, summary  # noqa: E402


def boot_ci(xs: list[float], reps: int = 5000):
    """Seeded percentile bootstrap of the mean (as analyze.boot_ci, sampling with the
    C-level random.choices so 10,000-trade groups stay fast)."""
    if len(xs) < 2:
        return None
    import random
    rng = random.Random(analyze.RNG_SEED + len(xs))
    n = len(xs)
    means = sorted(sum(rng.choices(xs, k=n)) / n for _ in range(reps))
    return [round(means[int(0.025 * reps)], 3), round(means[int(0.975 * reps) - 1], 3)]


analyze.boot_ci = boot_ci  # summary() uses the same sampler

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


def verdict(s: dict) -> str:
    """Descriptive label from the net-R bootstrap CI (per-group CIs are not multiplicity
    adjusted; only the pre-registered endpoints are confirmatory)."""
    ci = s.get("exp_ci")
    if s["n"] < MIN_N or not ci:
        return "insufficient sample (< 30 trades)"
    if ci[1] < 0:
        return "negative (95% CI below 0)"
    if ci[0] > 0:
        return "positive (95% CI above 0)"
    return "inconclusive (95% CI includes 0)"


def _slots(di: dict) -> int:
    """Regular-session 5-minute slots between a series' first and last candle."""
    from datetime import date as _d
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.market.calendar import MarketCalendar
    a, b = _d.fromisoformat(di["first"][:10]), _d.fromisoformat(di["last"][:10])
    return sum(int((s.close - s.open).total_seconds() // 300)
               for s in MarketCalendar().trading_days(a, b))


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def block(rows: list[dict], costs: bool = False) -> dict:
    if not rows:
        return {"n": 0}
    s = summary(rows)
    s["max_dd_r"] = max_dd(rows)
    s["gross_exp_r"] = round(sum(r["gross_r"] for r in rows) / len(rows), 3)
    s["mean_cost_r"] = round(sum(r["cost_r"] for r in rows) / len(rows), 3)
    s["verdict"] = verdict(s)
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
    recs = [json.loads(x) for p in sys.argv[1:]
            for x in (gzip.open(p, "rt") if p.endswith(".gz") else open(p))]
    proto = json.loads((VAL / "protocol.json").read_text())["historical_validation"]
    sealed = int(datetime(2026, 5, 1, 4, tzinfo=timezone.utc).timestamp())  # 00:00 ET
    sectors, sector_src = {}, {}
    for f in VAL.glob("universe-*.json"):
        mode = f.name.split("-")[1]
        for r in json.loads(f.read_text())["selected"]:
            sectors[(mode, r["symbol"])] = r["sector"]
            sector_src[(mode, r["symbol"])] = (
                "current GICS (not point-in-time)" if r["sector_source"] == "current GICS"
                else "GICS when it left the index (manual, not point-in-time)")
    rows, data = [], {}
    for rec in recs:
        d = data.setdefault(rec["mode"], {"symbols": set(), "data_errors": [], "signals": 0,
                                         "not_live": 0, "skipped": 0, "runtime_s": 0,
                                         "rules_hash": set(), "coverage": {},
                                         "latest_used": {"signal": 0, "entry": 0, "exit": 0,
                                                         "rejected_signal_seen": 0}})
        ex = "5Min" if rec["mode"] == "day" else "1Day"
        for di in rec["data_integrity"]:
            if di.get("timeframe") == ex:
                c = d["coverage"].setdefault(rec["symbol"], {"runs": 0, "issues": []})
                c["runs"] += 1
                c["first"] = min(filter(None, [c.get("first"), di.get("first")]), default=None)
                c["last"] = max(filter(None, [c.get("last"), di.get("last")]), default=None)
                c["missing_sessions"] = c.get("missing_sessions", 0) + di.get("missing_sessions", 0)
                c["issues"] += di.get("issues") or []
                c["bars"] = c.get("bars", 0) + (di.get("bars") or 0)
                if rec["mode"] == "day" and di.get("first"):
                    c["expected_slots"] = c.get("expected_slots", 0) + _slots(di)
            elif di.get("timeframe") == "volume":
                d["coverage"].setdefault(rec["symbol"], {"runs": 0, "issues": []})["issues"] += \
                    [f"volume: {x}" for x in di.get("issues") or []]
        lu = d["latest_used"]
        for t in rec["trades"]:
            lu["signal"] = max(lu["signal"], t["signal_time"])
            lu["entry"] = max(lu["entry"], t["entry_time"])
            lu["exit"] = max(lu["exit"], t["exit_time"])
        for x in rec["skipped"] + rec["not_live"]:
            lu["rejected_signal_seen"] = max(lu["rejected_signal_seen"], x.get("seen_at") or 0)
        d["symbols"].add(rec["symbol"])
        sbs = d.setdefault("signals_by_symbol", {})
        sbs[rec["symbol"]] = sbs.get(rec["symbol"], 0) + rec["signals_detected"]
        d["data_errors"] += [e | {"run": rec["run"]} for e in rec["data_errors"]]
        d["signals"] += rec["signals_detected"]
        d["not_live"] += len(rec["not_live"])
        d["skipped"] += len(rec["skipped"])
        d["runtime_s"] += rec["seconds"]
        d["rules_hash"].add(rec["rules_hash"])
        for t in rec["trades"]:
            f = features(t, rec["mode"])
            f["sector"] = sectors.get((rec["mode"], t["ticker"]), "?")
            f["sector_source"] = sector_src.get((rec["mode"], t["ticker"]), "?")
            f["strat_tf"] = f"{f['strategy']} {f['timeframe']}"
            rows.append(f)
    out = {"data": {}, "modes": {}}
    for m, d in data.items():
        want = proto[m]["symbols"]
        lu = d["latest_used"]
        out["data"][m] = d | {
            "symbols_processed": len(d["symbols"]), "symbols_expected": len(want),
            "missing_symbols": [s for s in want if s not in d["symbols"]],
            "rules_hash": sorted(d["rules_hash"]),
            "latest_used": {k: iso(v) for k, v in lu.items()},
            "sealed_period_used": any(v >= sealed for v in lu.values()),
            "period": proto[m]["period"],
            "sector_sources": {s: sector_src.get((m, s)) for s in want}}
        del out["data"][m]["symbols"]
        for c in d["coverage"].values():
            if c.get("expected_slots"):
                c["iex_5min_fill"] = round(c["bars"] / c["expected_slots"], 3)
        out["data"][m]["zero_signal_symbols"] = sorted(
            s for s in d["symbols"] if not any(r["ticker"] == s and r["mode"] == m for r in rows)
            and not d["signals_by_symbol"].get(s))
        del out["data"][m]["signals_by_symbol"]
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
