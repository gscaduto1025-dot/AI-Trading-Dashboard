"""Research harness: run the production backtest unchanged and save every trade and
untraded signal with the plan details that were live at entry.

It changes no strategy rule: it calls app.backtest.runner.run_backtest exactly as the app
does and only records, through a wrapper around eligibility.find_entry, the setup the live
engine reported at the entry decision (plan ATR, stop/target basis, quality components,
conflicts, confirmation conditions). Market data reads only; no orders.

The final test period is SEALED: any run touching it is refused unless --unseal is given.

Usage (from backend/): .venv/bin/python research/collect_trades.py <out.json> <segment>...
segments: day-dev, day-val, swing-dev, swing-val, bundled
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from dataclasses import replace
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest import runner as R  # noqa: E402
from app.backtest.data import BundledHistory, MarketHistory  # noqa: E402
from app.backtest.manager import engine_config  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.main import Service  # noqa: E402

SEALED_FROM = date(2026, 5, 1)  # final out-of-sample test: not run during development
WATCHLIST = ["SPY", "QQQ", "NVDA", "TSLA", "AAPL", "AMD", "MSFT", "META", "AMZN"]
SEGMENTS = {
    # name: (mode, provider, symbols, [(start, end), ...]) - runs split to the 190-day limit
    "day-dev": ("day", "alpaca", WATCHLIST,
                [(date(2024, 11, 1), date(2025, 4, 30)), (date(2025, 5, 1), date(2025, 10, 31))]),
    "day-val": ("day", "alpaca", WATCHLIST, [(date(2025, 11, 1), date(2026, 4, 30))]),
    "swing-dev": ("swing", "alpaca", WATCHLIST, [(date(2021, 7, 1), date(2024, 6, 30))]),
    "swing-val": ("swing", "alpaca", WATCHLIST, [(date(2024, 7, 1), date(2026, 4, 30))]),
    "bundled": ("swing", "bundled", ["AAPL", "GOOG"], [(date(2005, 1, 3), date(2017, 2, 10))]),
}


def plan_extract(setup: dict | None) -> dict:
    if not setup:
        return {}
    plan = setup.get("plan") or {}
    q = plan.get("quality") or {}
    pr = lambda k: (plan.get(k) or {}).get("price")  # noqa: E731
    return {
        "atr": (plan.get("atr") or {}).get("value"),
        "atr_label": (plan.get("atr") or {}).get("label"),
        "entry_trigger": pr("entry_trigger"),
        "stop_anchor": (plan.get("stop") or {}).get("anchor"),
        "stop_basis": (plan.get("stop") or {}).get("basis"),
        "target1_basis": (plan.get("target1") or {}).get("basis"),
        "target1_source": (plan.get("target1") or {}).get("source"),
        "quality_components": {c["name"]: c.get("points") for c in q.get("components", [])},
        "quality_details": {c["name"]: c.get("detail") for c in q.get("components", [])},
        "conflicts": q.get("conflicts", []),
        "confirmation": [{"name": c["name"], "passed": c.get("passed"),
                          "detail": c.get("detail"), "verification": c.get("verification")}
                         for c in setup.get("confirmation") or []],
        "htf_bias": (setup.get("htf_bias") or {}).get("label")
        if isinstance(setup.get("htf_bias"), dict) else setup.get("htf_bias"),
        "reason": (setup.get("reasons") or [""])[0],
    }


def main() -> None:
    out, names = sys.argv[1], [a for a in sys.argv[2:] if not a.startswith("--")]
    unseal = "--unseal" in sys.argv
    svc = Service(replace(load_settings(), database_path=Path(tempfile.mkdtemp()) / "r.db",
                          scan_interval_seconds=0))
    if svc.is_demo:
        raise SystemExit("real Alpaca data required (keys missing: demo mode)")
    captured: dict[str, dict] = {}
    original = R.find_entry

    def recording_find_entry(symbol, sig, mode, series, cal, ctx_cfg, cfg, sip=None):
        j, info = original(symbol, sig, mode, series, cal, ctx_cfg, cfg, sip)
        captured[sig["id"]] = plan_extract(info.get("setup"))
        return j, info

    R.find_entry = recording_find_entry
    result = {"sealed_from": SEALED_FROM.isoformat(), "segments": {}}
    for name in names:
        mode, source, syms, spans = SEGMENTS[name]
        if not unseal and any(e >= SEALED_FROM for _, e in spans):
            raise SystemExit(f"{name} touches the sealed test period")
        provider = BundledHistory() if source == "bundled" else MarketHistory(svc.market)
        seg = {"mode": mode, "source": source, "symbols": syms, "runs": []}
        for start, end in spans:
            t0 = time.time()
            captured.clear()
            run = R.run_backtest(provider, mode=mode, symbols=syms, start=start, end=end,
                                 cal=svc.market.calendar, engine_cfg=engine_config(svc, provider))
            for t in run["trades"]:
                t["plan_at_entry"] = captured.get(t["id"], {})
            seg["runs"].append({k: run[k] for k in (
                "start", "end", "start_ts", "end_ts", "trades", "skipped", "not_live",
                "signals_detected", "data_errors", "data_integrity", "settings", "volume",
                "regime_basis", "engine_evaluations")})
            print(f"{name} {start}..{end}: {len(run['trades'])} trades, "
                  f"{run['signals_detected']} signals, {time.time() - t0:.0f}s", flush=True)
        result["segments"][name] = seg
    Path(out).write_text(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
