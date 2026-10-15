"""Strategy validation registry: what the frozen historical validation says about each
strategy, shown next to every setup so a technical setup is never presented as a proven
trade.

The registry (status.json) is generated from the frozen validation report by
research/build_validation_status.py and committed; the app only reads it. It changes no
strategy rule. Statuses:

  VALIDATED_POSITIVE            positive after costs in validation AND confirmed by a
                                pre-registered confirmatory test (none so far)
  NEGATIVE_HISTORICAL_EVIDENCE  lost money after costs, 95% CI below zero
  INCONCLUSIVE                  enough trades, but the 95% CI includes zero
  UNVALIDATED                   too few trades, or never tested
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

STATUS_FILE = Path(__file__).with_name("status.json")
STATUSES = ("VALIDATED_POSITIVE", "NEGATIVE_HISTORICAL_EVIDENCE", "INCONCLUSIVE",
            "UNVALIDATED")
NOTICE = {
    "VALIDATED_POSITIVE": "Positive after costs in out-of-sample validation and a "
                          "confirmatory test. Past results do not guarantee future results.",
    "NEGATIVE_HISTORICAL_EVIDENCE": "Technical setup only. This strategy lost money after "
                                    "costs in historical validation; it is not a "
                                    "recommended trade.",
    "INCONCLUSIVE": "Technical setup only. Historical validation could not show a positive "
                    "edge after costs.",
    "UNVALIDATED": "Technical setup only. This strategy has no adequate historical "
                   "validation.",
}


@lru_cache(maxsize=1)
def registry() -> dict:
    """The committed registry; an unvalidated stub if the file is missing."""
    try:
        return json.loads(STATUS_FILE.read_text())
    except (OSError, ValueError):
        return {"baseline": None, "strategies": {}, "modes": {}, "criteria": {},
                "source": "registry unavailable"}


def for_strategy(strategy: str, timeframe: str | None = None) -> dict:
    """The compact validation block attached to a scanner row or alert."""
    s = registry().get("strategies", {}).get(strategy)
    if not s:
        status = "UNVALIDATED"
        return {"status": status, "label": "Unvalidated", "notice": NOTICE[status],
                "summary": "Not part of the historical validation.", "n": 0,
                "net_r": None, "net_ci": None, "profit_factor": None, "gross_r": None,
                "timeframe": None, "baseline": registry().get("baseline")}
    ev = s.get("evidence") or {}
    tf = (s.get("by_timeframe") or {}).get(timeframe or "") or None
    return {
        "status": s["status"], "label": s["label"], "notice": NOTICE[s["status"]],
        "summary": s["summary"], "n": ev.get("n", 0), "net_r": ev.get("exp_r"),
        "net_ci": ev.get("exp_ci"), "profit_factor": ev.get("pf"),
        "gross_r": ev.get("gross_exp_r"),
        "timeframe": ({"timeframe": timeframe, "n": tf.get("n"), "net_r": tf.get("exp_r"),
                       "net_ci": tf.get("exp_ci")} if tf else None),
        "baseline": registry().get("baseline"),
    }
