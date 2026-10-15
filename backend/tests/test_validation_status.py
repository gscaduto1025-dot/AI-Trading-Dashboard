"""Strategy validation labels, scanner filters and presets, and the research endpoint.

The labels come from the frozen validation report; these tests check that every setup
carries one, that the registry follows its stated criteria, that nothing is labelled
VALIDATED_POSITIVE without a confirmatory test, and that the rules themselves are still
the frozen ones."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import Settings
from app.db import Database
from app.main import Service
from app.scanner.engine import annotate_row, filter_rows
from app.validation import NOTICE, STATUSES, for_strategy, registry

from .conftest import demo_market

BACKEND = Path(__file__).resolve().parents[1]
ENGINE_STRATEGIES = {"liquidity_sweep_reversal", "trend_pullback", "sr_rejection",
                     "breakout_retest", "opening_range_breakout", "swing_breakout",
                     "swing_sr_reversal", "swing_consolidation_breakout",
                     "swing_trend_continuation", "swing_trend_reversal"}


@pytest.fixture(scope="module")
def scanned(tmp_path_factory):
    path = tmp_path_factory.mktemp("val") / "t.db"
    svc = Service(Settings(database_path=path, scan_interval_seconds=0),
                  market=demo_market(), db=Database(path))
    return svc, svc.scanner.run()


# --------------------------------------------------------------------------- registry

def test_registry_covers_every_strategy_and_follows_its_criteria():
    reg = registry()
    assert reg["baseline"] == "rejected-v1"
    assert set(reg["strategies"]) == ENGINE_STRATEGIES
    for sid, s in reg["strategies"].items():
        assert s["status"] in STATUSES
        ev = s["evidence"]
        if s["status"] == "NEGATIVE_HISTORICAL_EVIDENCE":
            assert ev["n"] >= 30 and ev["exp_ci"][1] < 0, sid
        elif s["status"] == "INCONCLUSIVE":
            assert ev["n"] >= 30 and ev["exp_ci"][0] <= 0 <= ev["exp_ci"][1], sid
        elif s["status"] == "UNVALIDATED":
            assert ev is None or ev["n"] < 30, sid
        # No confirmatory test has been run (the sealed period is unopened).
        assert s["status"] != "VALIDATED_POSITIVE", sid


def test_registry_matches_the_frozen_report():
    """The committed registry is exactly what the build script derives from the frozen
    report (no hand edits)."""
    out = subprocess.run([sys.executable, "research/build_validation_status.py"],
                         cwd=BACKEND, capture_output=True, text=True, check=True)
    assert "NEGATIVE_HISTORICAL_EVIDENCE" in out.stdout
    rep = json.loads((BACKEND / "research/validation/results/report.json").read_text())
    s = registry()["strategies"]["liquidity_sweep_reversal"]["evidence"]
    r = rep["modes"]["day"]["by_strategy"]["liquidity_sweep_reversal"]
    assert (s["n"], s["exp_r"], s["exp_ci"]) == (r["n"], r["exp_r"], r["exp_ci"])
    subprocess.run(["git", "diff", "--exit-code", "--", "app/validation/status.json"],
                   cwd=BACKEND, check=True)


def test_unknown_strategy_is_unvalidated():
    v = for_strategy("not_a_strategy")
    assert v["status"] == "UNVALIDATED" and v["n"] == 0 and v["notice"] == NOTICE["UNVALIDATED"]


def test_frozen_rule_surface_is_unchanged():
    out = subprocess.run([sys.executable, "research/freeze.py", "surface"], cwd=BACKEND,
                         capture_output=True, text=True, check=True).stdout.strip()
    proto = json.loads((BACKEND / "research/validation/protocol.json").read_text())
    assert out == proto["protocol_amendments_data"]["rule_surface_sha256"]


# ---------------------------------------------------------------------------- scanner

def test_every_row_carries_its_validation_label_and_context(scanned):
    _, snap = scanned
    assert snap["rows"]
    for r in snap["rows"] + snap["setup_states"]:
        v = r["validation"]
        assert v == for_strategy(r["strategy"], r["timeframe"])
        assert v["notice"].startswith(("Technical setup only", "Positive after costs"))
        assert "nearest_level" in r and "liquidity_adv" in r
        # The quality score is the engine's rules score, untouched by the label.
        assert r["quality"] is None or 0 <= r["quality"] <= 100


def test_annotate_row_picks_the_nearest_level():
    info = {"levels": {"day": {
        "support": {"label": "S", "low": 9, "high": 9.5, "timeframe": "5Min",
                    "distance": 0.5, "distance_pct": 5.0, "distance_atr": 1.0,
                    "near": False, "inside": False},
        "resistance": {"label": "R", "low": 10.2, "high": 10.4, "timeframe": "5Min",
                       "distance": 0.2, "distance_pct": 2.0, "distance_atr": 0.4,
                       "near": True, "inside": False}}},
        "liquidity": {"adv": 5e8}}
    row = annotate_row({"strategy": "trend_pullback", "timeframe": "5Min"}, info, "day")
    assert row["nearest_level"]["side"] == "resistance"
    assert row["nearest_level"]["distance_atr"] == 0.4
    assert row["liquidity_adv"] == 5e8
    assert row["validation"]["status"] == "NEGATIVE_HISTORICAL_EVIDENCE"
    assert row["validation"]["timeframe"]["timeframe"] == "5Min"


def test_new_filters(scanned):
    _, snap = scanned
    rows = snap["rows"]
    neg = filter_rows(rows, validation="NEGATIVE_HISTORICAL_EVIDENCE")
    assert neg and all(r["validation"]["status"] == "NEGATIVE_HISTORICAL_EVIDENCE" for r in neg)
    other = filter_rows(rows, validation="INCONCLUSIVE,UNVALIDATED")
    assert len(neg) + len(other) == len(rows)
    for state in ("WATCHING", "NO_TRADE", "CURRENTLY_ACTIONABLE,HISTORICALLY_TRIGGERED"):
        got = filter_rows(rows, signal_state=state)
        assert all(r["signal_state"] in state.split(",") for r in got)
    vol = filter_rows(rows, volume="NOT_REQUIRED")
    assert all(r["volume_confirmation"] == "NOT_REQUIRED" for r in vol)
    near = filter_rows(rows, max_level_atr=0.5)
    assert all(r["nearest_level"]["distance_atr"] <= 0.5 for r in near)
    pct = filter_rows(rows, max_level_pct=1.0)
    assert all(r["nearest_level"]["distance_pct"] <= 1.0 for r in pct)
    # Rows without a liquidity figure are excluded by a liquidity minimum.
    liq = filter_rows(rows, min_liquidity=1)
    assert all(r["liquidity_adv"] is not None and r["liquidity_adv"] >= 1 for r in liq)


def test_scanner_api_filters_and_validation(client):
    j = client.get("/api/scanner", params={"validation": "NEGATIVE_HISTORICAL_EVIDENCE",
                                           "signal_state": "WATCHING,NO_TRADE"}).json()
    assert all(r["validation"]["status"] == "NEGATIVE_HISTORICAL_EVIDENCE" for r in j["rows"])
    assert all(r["signal_state"] in ("WATCHING", "NO_TRADE") for r in j["rows"])
    for bad in ({"validation": "GOOD"}, {"signal_state": "LIVE"}, {"volume": "HUGE"}):
        assert client.get("/api/scanner", params=bad).status_code == 400


# ---------------------------------------------------------------------------- presets

def test_presets_crud(client):
    assert client.get("/api/scanner/presets").json()["presets"] == []
    body = {"name": "Day longs near levels",
            "filters": {"mode": "day", "side": "bullish", "max_level_atr": 0.5,
                        "validation": "INCONCLUSIVE,UNVALIDATED", "min_rr": 2}}
    p = client.post("/api/scanner/presets", json=body).json()
    assert p["name"] == body["name"] and p["filters"] == body["filters"]
    # Same name overwrites, not duplicates.
    body["filters"]["min_rr"] = 3
    p2 = client.post("/api/scanner/presets", json=body).json()
    assert p2["id"] == p["id"] and p2["filters"]["min_rr"] == 3
    assert len(client.get("/api/scanner/presets").json()["presets"]) == 1
    # Saved filters are valid /api/scanner parameters.
    assert client.get("/api/scanner", params=p2["filters"]).status_code == 200
    for bad in ({"name": "", "filters": {}}, {"name": "x", "filters": {"bogus": 1}},
                {"name": "x", "filters": {"mode": "scalp"}},
                {"name": "x", "filters": {"validation": "GOOD"}},
                {"name": "x", "filters": {"mode": ["day"]}}):
        assert client.post("/api/scanner/presets", json=bad).status_code == 400
    assert client.delete(f"/api/scanner/presets/{p['id']}").status_code == 200
    assert client.delete(f"/api/scanner/presets/{p['id']}").status_code == 404


def test_presets_persist_across_restart(tmp_path):
    db = Database(tmp_path / "p.db")
    db.save_preset("Swing", {"mode": "swing"})
    db.close()
    assert Database(tmp_path / "p.db").presets()[0]["filters"] == {"mode": "swing"}


# ---------------------------------------------------------------------------- research

def test_research_endpoint_serves_the_frozen_evidence(client):
    j = client.get("/api/research/validation").json()
    assert j == registry()
    day = j["modes"]["day"]["overall"]
    assert day["n"] > 10_000 and day["exp_ci"][1] < 0
    assert set(day["cost_sensitivity"]) == {"x0", "x0.2", "x0.25", "x0.5", "x1", "x2"}


def test_alerts_say_the_setup_is_not_a_validated_trade(scanned):
    svc, _ = scanned
    trig = [a for a in svc.db.alerts(limit=500) if a.get("setup_id")]
    assert trig
    for a in trig:
        assert a["validation"]["status"] in STATUSES
        assert "not a validated trade" in a["message"]
