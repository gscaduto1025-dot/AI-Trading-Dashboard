"""Performance metrics, in-sample / out-of-sample split, walk-forward validation,
quality-score analysis and the evidence verdict.

All statistics are computed from closed hypothetical trades (net of the modelled costs).
"R" is profit or loss divided by the initial risk at the fill (entry to stop).
"""

from __future__ import annotations

import math
from datetime import datetime

from ..market.calendar import NY


def _r(x, nd=4):
    return None if x is None else round(x, nd)


def summarize(trades: list[dict], risk_pct: float = 1.0) -> dict:
    n = len(trades)
    if not n:
        return {"trades": 0}
    rs = [t["r"] for t in trades]
    rets = [t["return_pct"] for t in trades]
    wins = [t for t in trades if t["r"] > 0]
    losses = [t for t in trades if t["r"] <= 0]
    gw = sum(t["r"] for t in wins)
    gl = -sum(t["r"] for t in losses)
    mean = sum(rs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / (n - 1)) if n > 1 else None
    se = sd / math.sqrt(n) if sd else None
    curve = equity_curve(trades, risk_pct)
    planned = [t["planned_rr"] for t in trades if t.get("planned_rr") is not None]
    exits: dict[str, int] = {}
    for t in trades:
        for leg in t["legs"]:
            key = leg["reason"].split(" (")[0]
            exits[key] = exits.get(key, 0) + 1
    return {
        "trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate": _r(100 * len(wins) / n, 2),
        "avg_win_r": _r(gw / len(wins)) if wins else None,
        "avg_loss_r": _r(-gl / len(losses)) if losses else None,
        "avg_win_pct": _r(sum(t["return_pct"] for t in wins) / len(wins)) if wins else None,
        "avg_loss_pct": _r(sum(t["return_pct"] for t in losses) / len(losses)) if losses else None,
        "avg_planned_rr": _r(sum(planned) / len(planned), 3) if planned else None,
        "realized_win_loss_ratio": _r((gw / len(wins)) / (gl / len(losses)), 3)
        if wins and losses and gl > 0 else None,
        "profit_factor": _r(gw / gl, 3) if gl > 0 else None,
        "expectancy_r": _r(mean),
        "expectancy_pct": _r(sum(rets) / n),
        "expectancy_ci95_r": [_r(mean - 1.96 * se), _r(mean + 1.96 * se)] if se else None,
        "std_r": _r(sd),
        "total_r": _r(sum(rs), 3),
        "max_drawdown_r": _r(max_drawdown([0.0] + _cumsum(rs)), 3),
        "max_drawdown_pct": curve["max_drawdown_pct"],
        "final_equity_pct": curve["final_return_pct"],
        "avg_bars_held": _r(sum(t["bars_held"] for t in trades) / n, 2),
        "avg_cost_r": _r(sum(t["gross_r"] - t["r"] for t in trades
                             if t.get("gross_r") is not None) / n),
        "exits": exits,
    }


def _cumsum(xs):
    out, s = [], 0.0
    for x in xs:
        s += x
        out.append(s)
    return out


def max_drawdown(curve: list[float]) -> float:
    """Largest peak-to-trough decline of an additive curve (same units as the curve)."""
    peak, dd = -math.inf, 0.0
    for v in curve:
        peak = max(peak, v)
        dd = max(dd, peak - v)
    return dd


def equity_curve(trades: list[dict], risk_pct: float = 1.0) -> dict:
    """Fixed-fractional equity: each trade risks `risk_pct` % of equity at the stop, applied
    in exit order (overlapping trades are not netted)."""
    eq, peak, mdd = 100.0, 100.0, 0.0
    points = []
    for t in sorted(trades, key=lambda x: (x["exit_time"], x["id"])):
        eq *= 1 + risk_pct / 100 * t["r"]
        peak = max(peak, eq)
        mdd = max(mdd, 100 * (peak - eq) / peak)
        points.append({"time": t["exit_time"], "equity": round(eq, 4),
                       "drawdown_pct": round(100 * (peak - eq) / peak, 4)})
    return {"points": points, "max_drawdown_pct": round(mdd, 3),
            "final_return_pct": round(eq - 100, 3)}


def group(trades: list[dict], key: str, risk_pct: float) -> dict:
    groups: dict[str, list] = {}
    for t in trades:
        groups.setdefault(str(t[key]), []).append(t)
    return {k: summarize(v, risk_pct) for k, v in sorted(groups.items())}


def _rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(xs, ys) -> float | None:
    if len(xs) < 3:
        return None
    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else None


QUALITY_BUCKETS = [(0, 50), (50, 60), (60, 70), (70, 80), (80, 101)]


def quality_analysis(trades: list[dict]) -> dict:
    """Do higher quality scores go with better realized outcomes?"""
    rows = [t for t in trades if t.get("quality") is not None]
    rho = spearman([t["quality"] for t in rows], [t["r"] for t in rows])
    n = len(rows)
    # t-statistic of a correlation; |t| > ~2 is roughly significant at 5 % for n > 30
    t_stat = rho * math.sqrt((n - 2) / max(1e-12, 1 - rho * rho)) if rho is not None and n > 3 \
        else None
    buckets = []
    for lo, hi in QUALITY_BUCKETS:
        b = [t for t in rows if lo <= t["quality"] < hi]
        buckets.append({"range": f"{lo}-{min(hi, 100)}", "trades": len(b),
                        "win_rate": _r(100 * sum(t["r"] > 0 for t in b) / len(b), 2) if b else None,
                        "expectancy_r": _r(sum(t["r"] for t in b) / len(b)) if b else None})
    if n < 30:
        verdict = f"Too few trades ({n}) to judge whether quality scores relate to outcomes."
    elif t_stat is not None and abs(t_stat) >= 2:
        verdict = (f"Quality scores are {'positively' if rho > 0 else 'NEGATIVELY'} related to "
                   f"realized R (Spearman {rho:.2f}, t {t_stat:.1f}, n {n}).")
    else:
        verdict = (f"No statistically meaningful relation between quality score and realized R "
                   f"(Spearman {rho if rho is None else round(rho, 2)}, n {n}).")
    return {"trades": n, "spearman": _r(rho, 3), "t_stat": _r(t_stat, 2), "buckets": buckets,
            "verdict": verdict}


def split_in_out(trades: list[dict], start_ts: int, end_ts: int, fraction: float):
    cut = start_ts + fraction * (end_ts - start_ts)
    ins = [t for t in trades if t["signal_time"] < cut]
    outs = [t for t in trades if t["signal_time"] >= cut]
    return ins, outs, int(cut)


def walk_forward(trades: list[dict], start_ts: int, end_ts: int, folds: int,
                 thresholds: list[float], risk_pct: float) -> dict:
    """Expanding-window walk-forward. The period is cut into folds+1 equal segments; for each
    fold the minimum-quality filter with the best expectancy on all EARLIER trades (ties:
    lowest threshold) is applied, unchanged, to the next segment. Only the out-of-sample
    test segments are aggregated. The engines themselves are not re-tuned."""
    seg = (end_ts - start_ts) / (folds + 1)
    rows, oos = [], []
    for i in range(1, folds + 1):
        train_end = start_ts + i * seg
        test_end = start_ts + (i + 1) * seg
        train = [t for t in trades if t["signal_time"] < train_end]
        test = [t for t in trades if train_end <= t["signal_time"] < test_end]
        best, best_e = thresholds[0], None
        for th in thresholds:
            sel = [t for t in train if (t["quality"] or 0) >= th]
            if len(sel) < 5:
                continue
            e = sum(t["r"] for t in sel) / len(sel)
            if best_e is None or e > best_e + 1e-12:
                best, best_e = th, e
        chosen = [t for t in test if (t["quality"] or 0) >= best]
        oos += chosen
        rows.append({"fold": i,
                     "train": [_iso(start_ts), _iso(train_end)],
                     "test_period": [_iso(train_end), _iso(test_end)],
                     "train_trades": len(train), "min_quality": best,
                     "train_expectancy_r": _r(best_e),
                     "test": summarize(chosen, risk_pct)})
    return {"folds": rows, "oos": summarize(oos, risk_pct),
            "method": "expanding window; min-quality filter chosen in-sample, applied "
                      "out-of-sample; strategy rules unchanged"}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, NY).date().isoformat()


def verdict(overall: dict, oos: dict, wf: dict, is_demo: bool, survivorship: bool,
            min_trades: int, min_oos: int) -> dict:
    """Never claims profitability without sufficient evidence."""
    reasons = []
    if is_demo:
        return {"label": "NOT EVIDENCE", "positive_evidence": False,
                "reasons": ["Run on DEMO DATA (simulated prices). It tests the machinery, not "
                            "the strategies."]}
    n, n_oos = overall.get("trades", 0), oos.get("trades", 0)
    if n < min_trades:
        reasons.append(f"Only {n} trades (need >= {min_trades}).")
    if n_oos < min_oos:
        reasons.append(f"Only {n_oos} out-of-sample trades (need >= {min_oos}).")
    ci = overall.get("expectancy_ci95_r")
    if not ci or ci[0] is None or ci[0] <= 0:
        reasons.append("The 95% confidence interval of expectancy includes zero or less.")
    if (oos.get("expectancy_r") or 0) <= 0:
        reasons.append("Out-of-sample expectancy is not positive.")
    if (wf.get("oos", {}).get("expectancy_r") or 0) <= 0:
        reasons.append("Walk-forward out-of-sample expectancy is not positive.")
    if (oos.get("profit_factor") or 0) <= 1:
        reasons.append("Out-of-sample profit factor is not above 1.")
    if survivorship:
        reasons.append("Universe is survivorship-biased (today's tickers chosen in "
                       "hindsight), which inflates results.")
    positive = not reasons
    if positive:
        label = "POSITIVE EVIDENCE (hypothetical)"
    elif n < min_trades or n_oos < min_oos:
        label = "INSUFFICIENT EVIDENCE"
    elif (overall.get("expectancy_r") or 0) <= 0:
        label = "NO EDGE FOUND"
    else:
        label = "NOT DEMONSTRATED"
    return {"label": label, "positive_evidence": positive, "reasons": reasons or [
        "Enough trades, positive out-of-sample and walk-forward expectancy with a confidence "
        "interval above zero. Still hypothetical: past results do not guarantee future ones."]}
