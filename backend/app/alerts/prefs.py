"""Alert types and user preferences (stored in SQLite, editable from the dashboard)."""

from __future__ import annotations

import copy

# type -> metadata. `actionable` types describe a live, confirmed trade plan and are only
# ever produced from fresh, complete data; the others are informational or warnings.
ALERT_TYPES: dict[str, dict] = {
    "approaching_support": {
        "label": "Approaching support", "severity": "info", "actionable": False,
        "cooldown_minutes": 60,
        "description": "Price moved within the proximity distance of the nearest important "
                       "support zone (key-level engine)."},
    "approaching_resistance": {
        "label": "Approaching resistance", "severity": "info", "actionable": False,
        "cooldown_minutes": 60,
        "description": "Price moved within the proximity distance of the nearest important "
                       "resistance zone."},
    "breakout_confirmed": {
        "label": "Breakout confirmed", "severity": "actionable", "actionable": True,
        "cooldown_minutes": 0,
        "description": "A breakout strategy (opening range, swing breakout, consolidation "
                       "breakout) confirmed its trigger on a completed candle and the plan "
                       "passed every planning check."},
    "retest_confirmed": {
        "label": "Breakout retest confirmed", "severity": "actionable", "actionable": True,
        "cooldown_minutes": 0,
        "description": "The breakout-retest strategy confirmed a hold of the broken level "
                       "and the plan passed every planning check."},
    "setup_triggered": {
        "label": "Setup triggered", "severity": "actionable", "actionable": True,
        "cooldown_minutes": 0,
        "description": "A setup's entry trigger confirmed (e.g. WATCHING -> TRIGGERED) and "
                       "the plan passed every planning check."},
    "setup_invalidated": {
        "label": "Setup invalidated", "severity": "warning", "actionable": False,
        "cooldown_minutes": 30,
        "description": "A previously WATCHING or TRIGGERED setup is no longer valid (its "
                       "invalidation condition hit, it expired, or it failed a check)."},
    "swing_candidate": {
        "label": "Strong swing candidate", "severity": "info", "actionable": False,
        "cooldown_minutes": 1440,
        "description": "A new swing setup is WATCHING or TRIGGERED with a quality score at or "
                       "above the configured minimum."},
    "target_reached": {
        "label": "Target reached", "severity": "info", "actionable": False,
        "cooldown_minutes": 0,
        "description": "A triggered setup's T1 or T2 was reached on a completed candle."},
    "stop_reached": {
        "label": "Stop reached", "severity": "warning", "actionable": False,
        "cooldown_minutes": 0,
        "description": "A triggered setup's stop-loss level was reached on a completed candle."},
    "price_level": {
        "label": "Price level reached", "severity": "info", "actionable": False,
        "cooldown_minutes": 0,
        "description": "Price reached a level you defined (one-shot)."},
    "data_stale": {
        "label": "Data stale / unavailable", "severity": "warning", "actionable": False,
        "cooldown_minutes": 60,
        "description": "Market data for a ticker became stale, invalid or unavailable; alerts "
                       "for it are suppressed until fresh data returns."},
}
TYPES = tuple(ALERT_TYPES)
MODES = ("day", "swing")

DEFAULT_PREFS: dict = {
    "enabled": {t: True for t in TYPES},
    "cooldown_minutes": {t: m["cooldown_minutes"] for t, m in ALERT_TYPES.items()},
    "modes": list(MODES),
    "muted_tickers": [],
    "proximity_atr": 0.5,           # "approaching" = within this many ATRs of the zone edge
    "swing_candidate_min_quality": 70,
    "min_quality": 0,               # actionable alerts below this quality score are skipped
    "min_risk_reward": None,        # optional extra R:R floor on actionable alerts
    "browser_types": ["breakout_confirmed", "retest_confirmed", "setup_triggered",
                      "setup_invalidated", "stop_reached", "target_reached", "price_level"],
    "webhook_types": ["breakout_confirmed", "retest_confirmed", "setup_triggered",
                      "setup_invalidated", "stop_reached", "target_reached", "price_level",
                      "swing_candidate"],
}


class PrefsError(ValueError):
    pass


def merge_prefs(saved: dict | None, update: dict | None = None) -> dict:
    """Defaults <- saved <- update, validated. Unknown keys and types are rejected."""
    out = copy.deepcopy(DEFAULT_PREFS)
    for layer in (saved or {}, update or {}):
        for k, v in layer.items():
            if k not in out:
                raise PrefsError(f"Unknown preference {k!r}")
            if k in ("enabled", "cooldown_minutes"):
                if not isinstance(v, dict):
                    raise PrefsError(f"{k} must be an object keyed by alert type")
                for t, x in v.items():
                    if t not in ALERT_TYPES:
                        raise PrefsError(f"Unknown alert type {t!r}")
                    if k == "enabled":
                        if not isinstance(x, bool):
                            raise PrefsError(f"enabled.{t} must be true or false")
                    elif not isinstance(x, (int, float)) or isinstance(x, bool) or not 0 <= x <= 10080:
                        raise PrefsError(f"cooldown_minutes.{t} must be 0-10080 minutes")
                    out[k][t] = x
            elif k == "modes":
                if not isinstance(v, list) or not set(v) <= set(MODES):
                    raise PrefsError("modes must be a list of 'day' and/or 'swing'")
                out[k] = [m for m in MODES if m in v]
            elif k == "muted_tickers":
                if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                    raise PrefsError("muted_tickers must be a list of symbols")
                out[k] = sorted({x.strip().upper() for x in v if x.strip()})
            elif k in ("browser_types", "webhook_types"):
                if not isinstance(v, list) or not set(v) <= set(TYPES):
                    raise PrefsError(f"{k} must be a list of alert types")
                out[k] = [t for t in TYPES if t in v]
            elif k == "proximity_atr":
                if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0.05 <= v <= 5:
                    raise PrefsError("proximity_atr must be between 0.05 and 5")
                out[k] = float(v)
            elif k in ("swing_candidate_min_quality", "min_quality"):
                if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v <= 100:
                    raise PrefsError(f"{k} must be between 0 and 100")
                out[k] = v
            elif k == "min_risk_reward":
                if v is not None and (not isinstance(v, (int, float)) or isinstance(v, bool)
                                      or not 0 < v <= 20):
                    raise PrefsError("min_risk_reward must be null or between 0 and 20")
                out[k] = v
    return out
