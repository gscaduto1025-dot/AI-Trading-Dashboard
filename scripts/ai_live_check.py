"""Live Claude check on a real, recent market setup (market-data reads + ONE Claude call).

  backend/.venv/bin/python scripts/ai_live_check.py [--mode day|swing]

1. Confirms the Anthropic credential is configured (never prints it).
2. Runs the real scanner on Alpaca data and picks the most recent detected setup
   (TRIGGERED first, then WATCHING, then a NO_TRADE with a trigger).
3. Asks Claude to explain it through the app's own path (fact sheet -> structured output
   -> validation), bypassing the cache, and prints the result, the validation report, the
   token usage and the estimated cost.
Exit code 0 = Claude fully operational; 1 = it is not (the reason is printed).
No orders of any kind are placed.
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.config import load_settings  # noqa: E402
from app.main import Service  # noqa: E402


def main() -> int:
    mode = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else None
    st = load_settings()
    print(f"Anthropic credential: {'configured' if st.anthropic_api_key_present else 'NOT SET'}"
          f" (value never shown) · model {st.claude_model} · effort {st.claude_effort}")
    print(f"Market data: {'Alpaca ' + st.alpaca_feed.upper() if st.alpaca_enabled else 'DEMO'}")
    if not st.anthropic_api_key_present:
        print("NOT OPERATIONAL: set ANTHROPIC_API_KEY in the environment (or backend/.env, "
              "chmod 600) and rerun.")
        return 1
    svc = Service(replace(st, database_path=Path(tempfile.mkdtemp()) / "ai.db",
                          scan_interval_seconds=0))
    snap = svc.scanner.run(force=True, trigger="ai live check")
    order = {"TRIGGERED": 0, "WATCHING": 1, "NO_TRADE": 2}
    rows = [r for r in snap["rows"] if mode in (None, r["mode"])]
    rows.sort(key=lambda r: (order[r["status"]], -(int(r["trigger_time"] or 0))))
    if not rows:
        print("NOT TESTED: the scanner found no setup to explain.")
        return 1
    r = rows[0]
    print(f"Setup: {r['ticker']} {r['mode']} {r['setup']} {r['direction']} {r['timeframe']} "
          f"status={r['status']} state={r['signal_state']} "
          f"evidence={r['validation']['label']}")
    out = svc.ai_explanation(r["ticker"], r["mode"], r["id"], refresh=True)
    if not out.get("available"):
        print(f"NOT OPERATIONAL: {out.get('reason')}")
        return 1
    ex, v = out["explanation"], out["validation"]
    print(f"Model: {out['model']} · tokens {json.dumps(out.get('usage'))} · estimated cost "
          f"${out.get('estimated_cost_usd')}")
    print(f"Validation: {v['numbers_verified']}/{v['numbers_checked']} numbers verified, "
          f"{len(v['removed'])} statement(s) removed, corrections: {v['corrections'] or 'none'}")
    for k in ("setup_status", "evidence_label", "setup_name", "technical_summary",
              "market_structure", "setup_explanation", "bullish_scenario", "bearish_scenario",
              "entry_conditions", "invalidation_conditions", "risk_reward",
              "historical_evidence", "risk_explanation"):
        print(f"- {k}: {ex[k]}")
    ok = (ex["setup_status"] == out["status"] == r["status"]
          and ex["evidence_label"] == r["validation"]["label"])
    print("FULLY OPERATIONAL" if ok else "OPERATIONAL WITH MISMATCH (see above)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
