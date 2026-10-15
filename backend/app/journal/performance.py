"""Performance analytics for the user's logged trades (ACTUAL or PAPER, never mixed).

Only CLOSED trades count toward realized results; open trades are listed separately.
Saved setups and backtests are never included (they are not trades). Every figure is
computed from the recorded fills; nothing is estimated.
"""

from __future__ import annotations

from datetime import date, datetime

from ..market.calendar import NY
from .pnl import stats

MIN_GROUP = 5  # groups smaller than this are flagged as anecdotal


def _day(ts: int) -> date:
    return datetime.fromtimestamp(ts, NY).date()


def matches(t: dict, f: dict) -> bool:
    if f.get("ticker") and t["ticker"] != f["ticker"].upper():
        return False
    if f.get("strategy") and (t.get("strategy") or "") != f["strategy"]:
        return False
    if f.get("mode") and t.get("mode") != f["mode"]:
        return False
    if f.get("asset_type") and (t.get("asset_type") or "stock") != f["asset_type"]:
        return False
    ref = t["pnl"].get("closed_at") or t["pnl"].get("opened_at")
    if ref is None:
        return False
    d = _day(ref)
    if f.get("start") and d < date.fromisoformat(f["start"]):
        return False
    if f.get("end") and d > date.fromisoformat(f["end"]):
        return False
    return True


def _group(closed: list[dict], key) -> list[dict]:
    g: dict = {}
    for t in closed:
        g.setdefault(key(t) or "(none)", []).append(t)
    out = []
    for k, ts in g.items():
        s = stats(ts)
        out.append({"key": k, "trades": s["closed_trades"], "net_pnl": s["net_pnl"],
                    "win_rate": s["win_rate"], "avg_r": s["avg_r"],
                    "profit_factor": s["profit_factor"], "anecdotal": len(ts) < MIN_GROUP})
    return sorted(out, key=lambda x: -x["net_pnl"])


def performance(trades: list[dict], account: str, filters: dict) -> dict:
    sel = [t for t in trades if t["pnl"]["status"] != "invalid" and matches(t, filters)]
    closed = sorted((t for t in sel if t["pnl"]["status"] == "closed"),
                    key=lambda t: t["pnl"]["closed_at"])
    open_ = [t for t in sel if t["pnl"]["status"] == "open"]
    s = stats(closed)
    cum = peak = dd = 0.0
    curve = []
    for t in closed:
        cum += t["pnl"]["realized_net"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        curve.append({"time": t["pnl"]["closed_at"], "date": _day(t["pnl"]["closed_at"]).isoformat(),
                      "ticker": t["ticker"], "net": t["pnl"]["realized_net"],
                      "cumulative": round(cum, 2)})
    monthly: dict[str, float] = {}
    for t in closed:
        m = _day(t["pnl"]["closed_at"]).strftime("%Y-%m")
        monthly[m] = round(monthly.get(m, 0.0) + t["pnl"]["realized_net"], 2)
    by_strategy = _group(closed, lambda t: t.get("strategy"))
    eligible = [g for g in by_strategy if not g["anecdotal"]]
    durations = [t["pnl"]["duration_seconds"] for t in closed
                 if t["pnl"].get("duration_seconds") is not None]
    return {
        "account": account, "filters": {k: v for k, v in filters.items() if v},
        "summary": s | {
            "total_trades": len(sel), "gross_pnl": round(sum(t["pnl"]["realized_gross"]
                                                             for t in closed), 2),
            "max_drawdown": round(dd, 2),
            "breakeven": sum(t["pnl"]["result"] == "breakeven" for t in closed),
            "avg_duration_seconds": round(sum(durations) / len(durations)) if durations
            else None,
            "open_realized_partials": round(sum(t["pnl"]["realized_net"] for t in open_), 2)},
        "best_strategy": eligible[0] if eligible else None,
        "worst_strategy": eligible[-1] if len(eligible) > 1 else None,
        "strategy_note": (f"Best/worst need at least {MIN_GROUP} closed trades per strategy."
                          if not eligible else None),
        "by_strategy": by_strategy,
        "by_mode": _group(closed, lambda t: t.get("mode")),
        "by_ticker": _group(closed, lambda t: t["ticker"]),
        "by_asset_type": _group(closed, lambda t: t.get("asset_type") or "stock"),
        "monthly": [{"month": k, "net_pnl": v} for k, v in sorted(monthly.items())],
        "cumulative": curve,
        "open_trades": len(open_),
        "simulated": account == "paper",
        "note": ("Paper trades: simulated, kept apart from actual trades." if account == "paper"
                 else "Actual trades you logged. Detected setups, saved setups and backtests "
                      "are never included."),
    }
