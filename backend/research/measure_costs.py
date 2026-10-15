"""Measure real entry costs for sampled day trades from historical consolidated quotes.

For each sampled trade: the first NBBO quote at or after (entry candle open + 5 s, the live
completion grace), giving
  half_spread_bps  = (ask - bid) / 2 / mid
  entry_cost_bps   = (ask - open) / open for a long, (open - bid) / open for a short:
                     what a marketable order would pay versus the candle open the backtest
                     uses as its reference (spread + drift in the first seconds).
Market-data reads only. Usage: .venv/bin/python research/measure_costs.py <seg.json>...
"""

from __future__ import annotations

import json
import os
import random
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

H = {"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY_ID"],
     "APCA-API-SECRET-KEY": os.environ["ALPACA_API_SECRET_KEY"]}


def first_quote(sym: str, ts: int):
    start = datetime.fromtimestamp(ts + 5, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = datetime.fromtimestamp(ts + 65, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = {"start": start, "end": end, "limit": 20, "feed": "sip"}
    url = f"https://data.alpaca.markets/v2/stocks/{sym}/quotes?" + urllib.parse.urlencode(q)
    j = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=30).read())
    for x in j.get("quotes") or []:
        if x["bp"] > 0 and x["ap"] >= x["bp"]:
            return x
    return None


def main() -> None:
    trades = []
    for path in sys.argv[1:]:
        for seg, s in json.load(open(path))["segments"].items():
            for run in s["runs"]:
                trades += [t | {"seg": seg} for t in run["trades"] if t["mode"] == "day"]
    rng = random.Random(11)
    sample = rng.sample(trades, min(300, len(trades)))
    rows = []
    for t in sample:
        q = first_quote(t["ticker"], t["entry_time"])
        if not q:
            continue
        mid = (q["ap"] + q["bp"]) / 2
        s = 1 if t["direction"] == "LONG" else -1
        px = q["ap"] if s > 0 else q["bp"]
        rows.append({"id": t["id"], "seg": t["seg"], "ticker": t["ticker"],
                     "half_spread_bps": round((q["ap"] - q["bp"]) / 2 / mid * 1e4, 3),
                     "entry_cost_bps": round((px - t["open"]) * s / t["open"] * 1e4, 3),
                     "risk_bps": round(t["risk_per_share"] / t["entry"] * 1e4, 2)})
    json.dump(rows, sys.stdout, indent=1)


if __name__ == "__main__":
    main()
