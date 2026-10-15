"""Point-in-time selection of the 50-ticker validation universe (market-data reads only).

Rule (fixed before any validation backtest was run; see research/validation_protocol.json):
  1. Candidates: S&P 500 members ON the selection date T0, from the public point-in-time
     membership file (github.com/fja05680/sp500, "S&P 500 Historical Components & Changes
     (Updated).csv"), so later deletions and acquisitions are included and later additions
     are not.
  2. Exclude the 9 development watchlist names (same company under any ticker: FB = META).
  3. Liquidity: median daily dollar volume (consolidated SIP close x volume) over the 126
     sessions ending at T0. Require a close >= $10 on T0 and candles on >= 95 % of those
     sessions. One share class per company (the more liquid one).
  4. Rank by liquidity; take names in order, at most SECTOR_CAP per GICS sector, until 50.
Sector: GICS sector from the current constituents file (github.com/datasets/
s-and-p-500-companies); names no longer in the index use FORMER_SECTORS below (their GICS
sector when they left the index), written before any result was seen.

Usage (from backend/): .venv/bin/python research/universe.py <T0 YYYY-MM-DD> > universe.json
"""

from __future__ import annotations

import csv
import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

MEMBERSHIP = Path("/home/user/fja05680/sp500/S&P 500 Historical Components & Changes (Updated).csv")
SECTORS = Path("/home/user/datasets/s-and-p-500-companies/data/constituents.csv")
WATCHLIST = {"SPY", "QQQ", "NVDA", "TSLA", "AAPL", "AMD", "MSFT", "META", "AMZN", "FB"}
N, SECTOR_CAP, SESSIONS, MIN_PRICE, MIN_COVERAGE = 50, 7, 126, 10.0, 0.95
SHARE_CLASSES = [{"GOOGL", "GOOG"}, {"FOXA", "FOX"}, {"NWSA", "NWS"}, {"UAA", "UA"},
                 {"DISCA", "DISCK"}, {"BRK.B", "BRK.A"}]
# GICS sector of former members that may rank in the top 50 (when they left the index).
FORMER_SECTORS = {
    "ATVI": "Communication Services", "TWTR": "Communication Services",
    "XLNX": "Information Technology", "SIVB": "Financials", "FRC": "Financials",
    "PXD": "Energy", "CTXS": "Information Technology", "NLOK": "Information Technology",
    "VIAC": "Communication Services", "PARA": "Communication Services",
    "DISCA": "Communication Services", "DISCK": "Communication Services",
    "PBCT": "Financials", "KSU": "Industrials", "MXIM": "Information Technology",
    "ALXN": "Health Care", "FLIR": "Information Technology", "VAR": "Health Care",
    "CERN": "Health Care", "ABMD": "Health Care", "DRE": "Real Estate",
    "FBHS": "Industrials", "WRK": "Materials", "MRO": "Energy", "HES": "Energy",
    "CTLT": "Health Care", "ANSS": "Information Technology", "JNPR": "Information Technology",
    "DFS": "Financials", "WBA": "Consumer Staples", "SEDG": "Information Technology",
    "ENPH": "Information Technology", "ETSY": "Consumer Discretionary",
    "ILMN": "Health Care", "MRNA": "Health Care", "WBD": "Communication Services",
    "PYPL": "Financials", "INTC": "Information Technology", "BA": "Industrials",
    "LUMN": "Communication Services", "CCL": "Consumer Discretionary",
    "NCLH": "Consumer Discretionary", "AAL": "Industrials", "PENN": "Consumer Discretionary",
    "DISH": "Communication Services", "GPS": "Consumer Discretionary",
    "SPLK": "Information Technology", "VMW": "Information Technology",
    "ANTM": "Health Care", "FISV": "Financials", "FLT": "Financials", "ABC": "Health Care",
    "RE": "Financials", "PKI": "Health Care", "WLTW": "Financials", "TWLO": "Information Technology",
}
RENAMED = {"ANTM": "ELV", "FISV": "FI", "FLT": "CPAY", "ABC": "COR", "RE": "EG", "PKI": "RVTY",
           "WLTW": "WTW", "FB": "META"}


def members_on(t0: date) -> list[str]:
    rows = list(csv.DictReader(MEMBERSHIP.open()))
    best = max((r for r in rows if date.fromisoformat(r["date"]) <= t0),
               key=lambda r: r["date"])
    return sorted(set(best["tickers"].split(",")))


def sectors() -> dict[str, str]:
    return {r["Symbol"]: r["GICS Sector"] for r in csv.DictReader(SECTORS.open())}


def sip_daily(symbols: list[str], start: date, end: date, asof: date) -> dict[str, list]:
    h = {"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY_ID"],
         "APCA-API-SECRET-KEY": os.environ["ALPACA_API_SECRET_KEY"]}
    out: dict[str, list] = {}
    for i in range(0, len(symbols), 100):
        token = None
        while True:
            q = {"symbols": ",".join(symbols[i:i + 100]), "timeframe": "1Day",
                 "start": start.isoformat(), "end": end.isoformat(), "feed": "sip",
                 "adjustment": "raw", "limit": 10000, "asof": asof.isoformat()}
            if token:
                q["page_token"] = token
            url = "https://data.alpaca.markets/v2/stocks/bars?" + urllib.parse.urlencode(q)
            j = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                                  timeout=60).read())
            for s, bars in (j.get("bars") or {}).items():
                out.setdefault(s, []).extend(bars)
            token = j.get("next_page_token")
            time.sleep(0.35)
            if not token:
                break
    return out


def main() -> None:
    t0 = date.fromisoformat(sys.argv[1])
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.market.calendar import MarketCalendar
    cal = MarketCalendar()
    days = [s.day for s in cal.trading_days(t0 - timedelta(days=220), t0)][-SESSIONS:]
    cands = [s for s in members_on(t0) if s not in WATCHLIST]
    bars = sip_daily(cands, days[0], t0, t0)
    sec = sectors()
    rows, excluded = [], []
    for s in cands:
        b = [x for x in bars.get(s, []) if days[0].isoformat() <= x["t"][:10] <= t0.isoformat()]
        cov = len(b) / len(days)
        if not b or cov < MIN_COVERAGE:
            excluded.append({"symbol": s, "reason": f"coverage {cov:.0%}"})
            continue
        if b[-1]["t"][:10] != t0.isoformat() or b[-1]["c"] < MIN_PRICE:
            excluded.append({"symbol": s, "reason": f"close {b[-1]['c']} on {b[-1]['t'][:10]}"})
            continue
        dv = statistics.median(x["c"] * x["v"] for x in b)
        sector = sec.get(s) or sec.get(RENAMED.get(s, "")) or FORMER_SECTORS.get(s)
        rows.append({"symbol": s, "median_dollar_volume": round(dv), "close_t0": b[-1]["c"],
                     "coverage": round(cov, 3), "sector": sector,
                     "sector_source": "current GICS" if (s in sec or RENAMED.get(s) in sec)
                     else ("former GICS (manual)" if sector else None)})
    rows.sort(key=lambda r: -r["median_dollar_volume"])
    for i, r in enumerate(rows, 1):
        r["liquidity_rank"] = i
    chosen, per, skipped = [], {}, []
    for r in rows:
        if len(chosen) == N:
            break
        twin = next((g for g in SHARE_CLASSES if r["symbol"] in g), set())
        if any(c["symbol"] in twin for c in chosen):
            skipped.append({"symbol": r["symbol"], "reason": "second share class"})
            continue
        if r["sector"] is None:
            raise SystemExit(f"no sector for {r['symbol']}: add it to FORMER_SECTORS first")
        if per.get(r["sector"], 0) >= SECTOR_CAP:
            skipped.append({"symbol": r["symbol"], "reason": f"sector cap ({r['sector']})"})
            continue
        per[r["sector"]] = per.get(r["sector"], 0) + 1
        chosen.append(r)
    json.dump({"t0": t0.isoformat(), "window": [days[0].isoformat(), t0.isoformat()],
               "sessions": len(days), "candidates": len(cands), "rule": {
                   "n": N, "sector_cap": SECTOR_CAP, "min_price": MIN_PRICE,
                   "min_coverage": MIN_COVERAGE, "excluded_watchlist": sorted(WATCHLIST)},
               "sector_counts": per, "selected": chosen, "skipped_in_order": skipped,
               "excluded": excluded}, sys.stdout, indent=1)


if __name__ == "__main__":
    main()
