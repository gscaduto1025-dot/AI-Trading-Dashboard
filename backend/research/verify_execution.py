"""Execution-model verification on real data (market-data reads only).

1. Independent re-simulation: rebuilds each sampled trade's exit from raw execution
   candles with separately written code (same documented rules: entry at the decision
   candle's open + half spread + slippage; stop first when stop and target share a candle;
   gap through the stop fills at the open; target fills only when traded through by one
   tick; day trades flat at the session's last candle; swing time exit after 20 days),
   and compares exit reason and R with the backtest.
2. Intrabar ambiguity: for every trade whose stop and target were inside one execution
   candle (counted as a stop), finer candles (1-minute for 5-minute candles, 5-minute for
   daily candles) show which level traded first.
Usage: .venv/bin/python research/verify_execution.py <segments.json>... > verify.json
"""

from __future__ import annotations

import json
import random
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.backtest.config import BacktestConfig  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.main import Service  # noqa: E402

NY = ZoneInfo("America/New_York")
CFG = BacktestConfig()


def resim(t: dict, bars: list) -> tuple[str, float]:
    """Independent implementation of the documented exit rules (exit_policy t1)."""
    mode = t["mode"]
    long_ = t["direction"] == "LONG"
    s = 1 if long_ else -1
    cost = (CFG.half_spread_bps[mode] + CFG.slippage_bps[mode]) / 1e4
    stop, t1, tick = t["stop"], t["target1"], CFG.tick
    eb = next(b for b in bars if b.time == t["entry_time"])
    fill = eb.open * (1 + s * cost)
    risk = (fill - stop) * s
    held = 0
    exit_px = reason = None
    after = [b for b in bars if b.time >= eb.time]
    day = datetime.fromtimestamp(eb.time, NY).date()
    last = eb
    for k, b in enumerate(after):
        if mode == "day" and datetime.fromtimestamp(b.time, NY).date() != day:
            break
        last = b
        held += 1
        lo, hi = (b.low, b.high) if long_ else (-b.high, -b.low)
        st, tg, op = s * stop, s * t1, s * b.open
        if k > 0 and op <= st:
            exit_px, reason = b.open * (1 - s * cost), "stop (gap)"
            break
        if lo <= st:
            exit_px, reason = stop * (1 - s * cost), "stop"
            break
        if hi >= tg + tick or (k > 0 and op >= tg):
            exit_px, reason = t1, "target1"
            break
        if mode == "swing" and held >= CFG.max_hold_bars["swing"]:
            exit_px, reason = b.close * (1 - s * cost), "time exit"
            break
    if reason is None:
        exit_px, reason = last.close * (1 - s * cost), "session close / data end"
    long_sale = exit_px if long_ else fill
    fees = 2 * CFG.commission_per_share + CFG.sec_fee_rate * long_sale + CFG.finra_taf_per_share
    days = max(1.0, (last.time - eb.time) / 86400) if not long_ else 0.0
    borrow = fill * CFG.short_borrow_bps_per_year / 1e4 * days / 360 if not long_ else 0.0
    pnl = (exit_px - fill) * s - fees - borrow
    return reason, round(pnl / risk, 4)


def main() -> None:
    svc = Service(replace(load_settings(), database_path=Path(tempfile.mkdtemp()) / "v.db",
                          scan_interval_seconds=0))
    m = svc.market
    trades = []
    for path in sys.argv[1:]:
        d = json.load(open(path))
        for seg, s in d["segments"].items():
            if s["source"] != "alpaca":
                continue
            for run in s["runs"]:
                trades += [t | {"seg": seg} for t in run["trades"]]
    rng = random.Random(7)
    day = [t for t in trades if t["mode"] == "day"]
    sample = rng.sample(day, min(120, len(day))) + [t for t in trades if t["mode"] == "swing"]
    out = {"resim": [], "ambiguous": []}
    cache: dict = {}

    def bars(tk, tf, start, end):
        key = (tk, tf, start.date(), end.date())
        if key not in cache:
            cache[key] = m.get_history(tk, tf, start, end, any_symbol=True).bars
        return cache[key]

    for t in sample:
        et = datetime.fromtimestamp(t["entry_time"], NY)
        if t["mode"] == "day":
            b = bars(t["ticker"], "5Min", et.replace(hour=0, minute=0),
                     et.replace(hour=23, minute=59))
        else:
            b = bars(t["ticker"], "1Day", et - timedelta(days=3), et + timedelta(days=40))
        reason, r = resim(t, b)
        out["resim"].append({"id": t["id"], "seg": t["seg"], "mode": t["mode"],
                             "bt_exit": t["exit_reason"], "bt_r": t["r"],
                             "re_exit": reason, "re_r": r,
                             "match": abs(r - t["r"]) < 1e-3
                             and reason.split(" ")[0] == t["exit_reason"].split(" ")[0]})
    for t in trades:
        if "same candle" not in t["exit_reason"]:
            continue
        s = 1 if t["direction"] == "LONG" else -1
        xt = datetime.fromtimestamp(t["exit_time"], NY)
        if t["mode"] == "day":
            fine = [b for b in bars(t["ticker"], "1Min", xt, xt + timedelta(minutes=5))
                    if t["exit_time"] <= b.time < t["exit_time"] + 300]
        else:
            fine = bars(t["ticker"], "5Min", xt.replace(hour=0, minute=0),
                        xt.replace(hour=23, minute=59))
            fine = [b for b in fine if datetime.fromtimestamp(b.time, NY).date() == xt.date()]
        first = "unresolved"
        for b in fine:
            hit_stop = (b.low if s > 0 else -b.high) <= s * t["stop"]
            hit_tgt = (b.high if s > 0 else -b.low) >= s * t["target1"] + CFG.tick
            if hit_stop and hit_tgt:
                first = "both in one finer candle"
                break
            if hit_stop or hit_tgt:
                first = "stop first" if hit_stop else "target first"
                break
        out["ambiguous"].append({"id": t["id"], "seg": t["seg"], "mode": t["mode"],
                                 "ticker": t["ticker"], "time": xt.isoformat(),
                                 "first": first, "bt_r": t["r"],
                                 "r_if_target": round((t["target1"] - t["entry"]) * s
                                                      / t["risk_per_share"], 3),
                                 "fine_candles": len(fine)})
    json.dump(out, sys.stdout, indent=1)


if __name__ == "__main__":
    main()
