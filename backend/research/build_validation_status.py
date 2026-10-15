"""Build app/validation/status.json, the strategy validation registry the app shows, from
the FROZEN validation report (research/validation/results/report.json).

The registry only labels and summarises evidence; it changes no strategy rule. Labels:

  VALIDATED_POSITIVE            net expectancy (frozen costs) 95% CI above zero in the
                                validation AND confirmed by a pre-registered confirmatory
                                test (CONFIRMED below; none yet).
  NEGATIVE_HISTORICAL_EVIDENCE  >= 30 validation trades and the net 95% CI is below zero.
  INCONCLUSIVE                  >= 30 validation trades and the net 95% CI includes zero
                                (or is above zero without a confirmatory test).
  UNVALIDATED                   fewer than 30 validation trades, or never tested.

Usage (from backend/): .venv/bin/python research/build_validation_status.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "research" / "validation" / "results" / "report.json"
PROTOCOL = ROOT / "research" / "validation" / "protocol.json"
OUT = ROOT / "app" / "validation" / "status.json"
MIN_N = 30
CONFIRMED: set[str] = set()  # strategies that passed a pre-registered confirmatory test
STRATEGIES = {  # every strategy the engines can produce (id -> mode)
    "liquidity_sweep_reversal": "day", "trend_pullback": "day", "sr_rejection": "day",
    "breakout_retest": "day", "opening_range_breakout": "day",
    "swing_breakout": "swing", "swing_sr_reversal": "swing",
    "swing_consolidation_breakout": "swing", "swing_trend_continuation": "swing",
    "swing_trend_reversal": "swing",
}
NOTES = {  # observed facts about a strategy's sample, shown with its status
    "opening_range_breakout": "Zero trades and zero entry signals in development (9 "
    "tickers, 2024-11 to 2026-04) and validation (50 tickers, 2023-11 to 2024-10). Live "
    "evaluation shows the setups form but are rejected by the planning engine's 2:1 minimum "
    "reward/risk, because target 1 is the nearest key level. A design property, not a code "
    "fault; the rule is frozen.",
}
LABELS = {"VALIDATED_POSITIVE": "Validated positive",
          "NEGATIVE_HISTORICAL_EVIDENCE": "Negative historical evidence",
          "INCONCLUSIVE": "Inconclusive", "UNVALIDATED": "Unvalidated"}
KEEP = ("n", "win_rate", "win_ci", "exp_r", "exp_ci", "pf", "max_dd_r", "gross_exp_r",
        "mean_cost_r", "verdict")


def classify(ev: dict | None, sid: str) -> tuple[str, str]:
    if not ev or ev.get("n", 0) == 0:
        return "UNVALIDATED", "No trades in the validation sample, so there is no evidence."
    n, ci = ev["n"], ev.get("exp_ci")
    if n < MIN_N or not ci:
        return "UNVALIDATED", f"Only {n} validation trades: too few to judge."
    if ci[1] < 0:
        return ("NEGATIVE_HISTORICAL_EVIDENCE",
                f"Lost {ev['exp_r']:+.2f}R per trade after costs over {n} validation trades "
                f"(95% CI {ci[0]:+.2f} to {ci[1]:+.2f}R).")
    if ci[0] > 0 and sid in CONFIRMED:
        return ("VALIDATED_POSITIVE",
                f"{ev['exp_r']:+.2f}R per trade after costs over {n} trades "
                f"(95% CI {ci[0]:+.2f} to {ci[1]:+.2f}R), confirmed out of sample.")
    if ci[0] > 0:
        return ("INCONCLUSIVE",
                f"Positive in validation ({ev['exp_r']:+.2f}R, CI {ci[0]:+.2f} to "
                f"{ci[1]:+.2f}R) but not yet confirmed by a pre-registered test.")
    return ("INCONCLUSIVE",
            f"{ev['exp_r']:+.2f}R per trade after costs over {n} validation trades; the "
            f"95% CI ({ci[0]:+.2f} to {ci[1]:+.2f}R) includes zero.")


def main() -> None:
    rep = json.loads(REPORT.read_text())
    proto = json.loads(PROTOCOL.read_text())
    hv = proto["historical_validation"]
    out = {
        "baseline": "rejected-v1",
        "frozen_on": proto["frozen_on"],
        "rules_hash": proto["frozen"]["rules_hash"],
        "source": "research/validation/results/report.json (frozen 50-ticker validation)",
        "criteria": {k: v for k, v in {
            "VALIDATED_POSITIVE": "Net 95% CI above zero in validation and confirmed by a "
                                  "pre-registered confirmatory test.",
            "NEGATIVE_HISTORICAL_EVIDENCE": f">= {MIN_N} validation trades and the net 95% "
                                            "CI is below zero.",
            "INCONCLUSIVE": f">= {MIN_N} validation trades and the net 95% CI includes zero "
                            "(or is positive but unconfirmed).",
            "UNVALIDATED": f"Fewer than {MIN_N} validation trades, or never tested."}.items()},
        "costs": proto["frozen_cost_assumptions"],
        "modes": {}, "strategies": {},
    }
    for mode in ("day", "swing"):
        m = rep["modes"][mode]
        out["modes"][mode] = {
            "sample": f"50 unseen S&P 500 stocks, {hv[mode]['period'][0]} to "
                      f"{hv[mode]['period'][1]}",
            "independence": ("unseen tickers and unseen period" if mode == "day" else
                             "unseen tickers only; same dates as swing development"),
            "overall": {k: m["overall"].get(k) for k in KEEP}
            | {"cost_sensitivity": m["overall"]["cost_sensitivity"]},
            "preregistered": m["preregistered"],
        }
    for sid, mode in STRATEGIES.items():
        m = rep["modes"][mode]
        ev = m["by_strategy"].get(sid)
        status, summary = classify(ev, sid)
        tfs = {k.split(" ", 1)[1]: {f: v.get(f) for f in KEEP}
               for k, v in m["by_strategy_timeframe"].items() if k.split(" ", 1)[0] == sid}
        out["strategies"][sid] = {
            "mode": mode, "status": status, "label": LABELS[status], "summary": summary,
            "note": NOTES.get(sid),
            "evidence": ({k: ev.get(k) for k in KEEP} | {
                "cost_sensitivity": ev.get("cost_sensitivity")}) if ev else None,
            "by_timeframe": tfs,
        }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1) + "\n")
    for sid, s in out["strategies"].items():
        print(f"{sid:30s} {s['status']:30s} {s['summary']}")


if __name__ == "__main__":
    main()
