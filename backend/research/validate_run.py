"""Run the frozen-protocol 50-ticker historical validation (market-data reads only).

Runs the production backtest unchanged, one symbol at a time so a long run can resume:
each finished (mode, run, symbol) is appended as one JSON line to <out.jsonl> and skipped
on restart. Refuses to start unless the rules match the frozen protocol.

Usage (from backend/): .venv/bin/python research/validate_run.py <out.jsonl> day|swing
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import freeze  # noqa: E402
from collect_trades import plan_extract  # noqa: E402


def main() -> None:
    out, mode = Path(sys.argv[1]), sys.argv[2]
    frozen = freeze.check()["rules_hash"]
    proto = json.loads(freeze.PROTOCOL.read_text())["historical_validation"][mode]
    import tempfile
    from dataclasses import replace

    from app.backtest import runner as R
    from app.backtest.data import MarketHistory
    from app.backtest.manager import engine_config
    from app.config import load_settings
    from app.main import Service
    svc = Service(replace(load_settings(), database_path=Path(tempfile.mkdtemp()) / "v.db",
                          scan_interval_seconds=0))
    if svc.is_demo:
        raise SystemExit("real Alpaca data required")
    provider = MarketHistory(svc.market)
    captured: dict[str, dict] = {}
    original = R.find_entry

    def recording_find_entry(symbol, sig, mode_, series, cal, ctx_cfg, cfg, sip=None):
        j, info = original(symbol, sig, mode_, series, cal, ctx_cfg, cfg, sip)
        captured[sig["id"]] = plan_extract(info.get("setup"))
        return j, info

    R.find_entry = recording_find_entry
    done = set()
    if out.exists():
        done = {(r["run"], r["symbol"]) for r in map(json.loads, out.open())}
    for a, b in proto["runs"]:
        for sym in proto["symbols"]:
            if (a, sym) in done:
                continue
            t0 = time.time()
            captured.clear()
            run = R.run_backtest(provider, mode=mode, symbols=[sym],
                                 start=date.fromisoformat(a), end=date.fromisoformat(b),
                                 cal=svc.market.calendar,
                                 engine_cfg=engine_config(svc, provider))
            for t in run["trades"]:
                t["plan_at_entry"] = captured.get(t["id"], {})
            rec = {"run": a, "end": b, "symbol": sym, "mode": mode, "rules_hash": frozen,
                   "seconds": round(time.time() - t0)} | {k: run[k] for k in (
                       "trades", "skipped", "not_live", "signals_detected", "data_errors",
                       "data_integrity", "volume", "regime_basis")}
            with out.open("a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
            print(f"{mode} {a} {sym}: {len(run['trades'])} trades, "
                  f"{run['signals_detected']} signals, {rec['seconds']}s", flush=True)


if __name__ == "__main__":
    main()
