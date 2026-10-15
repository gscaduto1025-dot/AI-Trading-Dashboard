"""Forward paper test on the 9-stock watchlist (market-data reads only; NO orders of any
kind, to any broker or paper account).

Every completed session from the forward start (the first session after the freeze) is
replayed with the frozen engine: the same replay, entry-eligibility and volume rules as
the live scanner (parity-tested in tests/test_backtest_parity.py). Closed trades are
appended to the ledger, one JSON line each, and never rewritten. Each run replays the whole
forward period again and checks that every trade already in the ledger is reproduced
exactly; a difference is appended as a "discrepancy" line, not silently fixed.

Kept strictly apart from the 50-ticker historical validation (different symbols, dates,
file and report).

Usage (from backend/): .venv/bin/python research/forward_test.py [ledger.jsonl]
(--start D --through D replay an old window into a scratch ledger to check the mechanics)
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import freeze  # noqa: E402

LEDGER = freeze.ROOT / "research" / "validation" / "forward_ledger.jsonl"
COMPARE = ("entry_time", "exit_time", "entry", "exit", "stop", "target1", "r", "gross_r",
           "exit_reason")
CHUNK_DAYS = 180  # day-mode runs are limited to 190 calendar days


def closed(t: dict) -> bool:
    """A swing trade still open at the end of the data is not final yet. A day trade is
    final once its session has completed (only completed sessions are replayed)."""
    return t["mode"] == "day" or t["exit_reason"] != "data end"


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    ledger = Path(args[0]) if args else LEDGER
    proto = json.loads(freeze.PROTOCOL.read_text())["forward_paper_test"]
    start = date.fromisoformat(proto["starts"][:10])
    if "--start" in sys.argv:  # only for checking the mechanics on old data
        start = date.fromisoformat(sys.argv[sys.argv.index("--start") + 1])
    through = None
    if "--through" in sys.argv:  # likewise: stop at this session
        through = date.fromisoformat(sys.argv[sys.argv.index("--through") + 1])
    rules = freeze.check()["rules_hash"]

    from app.backtest import runner as R
    from app.backtest.data import MarketHistory
    from app.backtest.manager import engine_config
    from app.config import load_settings
    from app.main import Service
    from app.market.calendar import NY
    svc = Service(replace(load_settings(), database_path=Path(tempfile.mkdtemp()) / "f.db",
                          scan_interval_seconds=0))
    if svc.is_demo:
        raise SystemExit("real Alpaca data required")
    cal, now = svc.market.calendar, svc.market.now()
    # sessions whose close (plus the 16-minute SIP embargo) has passed
    done = [s for s in cal.trading_days(start, now.date())
            if s.close + timedelta(minutes=16) <= now and (not through or s.day <= through)]
    if not done:
        print(f"no completed session since {start}; nothing to do")
        return
    last = done[-1].day
    provider = MarketHistory(svc.market)
    ecfg = engine_config(svc, provider)
    old = [json.loads(x) for x in ledger.open()] if ledger.exists() else []
    known = {r["trade"]["id"]: r["trade"] for r in old if r.get("kind") == "trade"}
    trades, errors = [], []
    for mode in ("day", "swing"):
        spans = [(start, last)]
        if mode == "day":
            spans, a = [], start
            while a <= last:
                b = min(last, a + timedelta(days=CHUNK_DAYS))
                spans.append((a, b))
                a = b + timedelta(days=1)
        for a, b in spans:
            if a == b:  # run_backtest needs start < end; extend back over a non-session
                a = a - timedelta(days=1)
            run = R.run_backtest(provider, mode=mode, symbols=proto["symbols"], start=a,
                                 end=b, cal=cal, engine_cfg=ecfg)
            trades += [t for t in run["trades"]
                       if datetime.fromtimestamp(t["entry_time"], NY).date() >= start]
            errors += run["data_errors"]
    stamp = now.isoformat()
    lines = []
    seen = set()
    for t in sorted(trades, key=lambda t: t["entry_time"]):
        if not closed(t) or t["id"] in seen:
            continue
        seen.add(t["id"])
        if t["id"] in known:
            prev = known[t["id"]]
            diff = {k: (prev.get(k), t.get(k)) for k in COMPARE if prev.get(k) != t.get(k)}
            if diff:
                lines.append({"kind": "discrepancy", "id": t["id"], "diff": diff,
                              "recorded_at": stamp})
            continue
        lines.append({"kind": "trade", "recorded_at": stamp, "through_session": last.isoformat(),
                      "rules_hash": rules, "trade": t})
    missing = [i for i in known if i not in seen]
    for i in missing:
        lines.append({"kind": "discrepancy", "id": i, "diff": "no longer produced",
                      "recorded_at": stamp})
    lines.append({"kind": "run", "recorded_at": stamp, "through_session": last.isoformat(),
                  "rules_hash": rules, "new_trades": sum(x["kind"] == "trade" for x in lines),
                  "discrepancies": sum(x["kind"] == "discrepancy" for x in lines),
                  "data_errors": errors})
    with ledger.open("a") as f:
        for x in lines:
            f.write(json.dumps(x, default=str) + "\n")
    print(json.dumps(lines[-1], default=str))


if __name__ == "__main__":
    main()
