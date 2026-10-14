"""Trade-planning and risk engine tests (app/planning).

Unit tests build PlanInputs directly so every number can be checked by hand; the
integration tests check that both strategy engines publish exactly the plan's numbers.
"""

from __future__ import annotations

import json

import pytest

from app.planning import (NO_TRADE, TRIGGERED, WATCHING, PlanInput, PlanningConfig, PriceRef,
                          QualityInputs, build_plan, select_targets)
from app.planning.config import DEFAULT_WEIGHTS, load_planning_config
from app.strategies.common import Condition

CFG = PlanningConfig()  # min R:R 2.0, day stop buffer 0.1 ATR, swing 0.25 ATR


def ok(name="confirmation close"):
    return Condition(name, True, True, "passed")


def make(direction="LONG", signal=TRIGGERED, trigger=100.0, entry=100.2, anchor=99.0,
         candidates=None, atr=1.0, mode="day", **kw) -> PlanInput:
    return PlanInput(
        ticker="TEST", mode=mode, strategy="s", strategy_name="Strategy", direction=direction,
        timeframe="5Min", signal=signal, atr=atr, atr_label="5m ATR(14)",
        entry_trigger=PriceRef(trigger, "close beyond the trigger") if trigger is not None else None,
        entry_price=PriceRef(entry, "confirmation close") if entry is not None else None,
        stop_anchor=PriceRef(anchor, "structural level") if anchor is not None else None,
        candidates=candidates if candidates is not None else [],
        confirmation=kw.pop("confirmation", [ok()]), **kw)


def lvl(price, label="level", strength=None):
    return PriceRef(price, f"{label} {price:.2f}", "zone", "1Day", strength)


def check(plan, prefix):
    return next(c for c in plan["checks"] if c["name"].startswith(prefix))


# ------------------------------------------------------------------ valid plans

def test_long_triggered_plan_numbers():
    plan = build_plan(make(candidates=[lvl(103.0, "PDH"), lvl(104.5, "zone")]), CFG)
    assert plan["status"] == TRIGGERED
    assert plan["entry"]["price"] == 100.2
    assert plan["stop"]["price"] == pytest.approx(99.0 - 0.1)  # anchor - 0.1 ATR
    assert plan["risk_per_share"] == pytest.approx(1.3)
    assert plan["target1"]["price"] == 103.0 and plan["target2"]["price"] == 104.5
    assert plan["reward_per_share_t1"] == pytest.approx(2.8)
    assert plan["reward_per_share_t2"] == pytest.approx(4.3)
    assert plan["risk_reward_t1"] == pytest.approx(2.8 / 1.3, abs=0.01)
    assert plan["risk_reward_t2"] == pytest.approx(4.3 / 1.3, abs=0.01)
    assert "PDH 103.00" in plan["target1"]["basis"]
    assert "structural level" in plan["stop"]["basis"] and "0.1 x" in plan["stop"]["basis"]
    assert any(c.startswith("R:R (T1) = 2.8000 / 1.3000") for c in plan["calculations"])
    assert all(c["passed"] in (True, None) for c in plan["checks"])


def test_short_triggered_plan_numbers():
    plan = build_plan(make("SHORT", trigger=100.0, entry=99.8, anchor=101.0,
                           candidates=[lvl(97.0, "PDL"), lvl(95.5)]), CFG)
    assert plan["status"] == TRIGGERED
    assert plan["stop"]["price"] == pytest.approx(101.1)  # anchor + 0.1 ATR
    assert plan["risk_per_share"] == pytest.approx(1.3)
    assert plan["target1"]["price"] == 97.0 and plan["target2"]["price"] == 95.5
    assert plan["reward_per_share_t1"] == pytest.approx(2.8)
    assert plan["risk_reward_t1"] == pytest.approx(2.15, abs=0.01)
    assert plan["entry_zone"]["low"] <= 100.0 == plan["entry_zone"]["high"]


def test_watching_plan_is_priced_at_the_trigger_and_waits():
    inp = make(signal=WATCHING, entry=None, trigger=100.0, anchor=99.0,
               candidates=[lvl(103.0)], confirmation=[Condition("close beyond", False, True)])
    plan = build_plan(inp, CFG)
    assert plan["status"] == WATCHING
    assert plan["entry"]["price"] == 100.0 and plan["entry"]["kind"].startswith("planned")
    assert plan["risk_per_share"] == pytest.approx(1.1)
    assert plan["risk_reward_t1"] == pytest.approx(3 / 1.1, abs=0.01)
    assert check(plan, "Entry confirmation")["passed"] is None
    assert "has not occurred" in plan["reasons"][-1]


def test_trigger_requires_every_required_confirmation():
    inp = make(candidates=[lvl(103.0)],
               confirmation=[ok(), Condition("volume", False, True, "0.8x")])
    plan = build_plan(inp, CFG)
    assert plan["status"] == NO_TRADE
    assert plan["reasons"][0].startswith("Missing confirmation: volume")
    # Optional (supporting) conditions never block.
    inp = make(candidates=[lvl(103.0)], confirmation=[ok(), Condition("4H", False, False)])
    assert build_plan(inp, CFG)["status"] == TRIGGERED


# ------------------------------------------------------------ stop placement / risk

@pytest.mark.parametrize("direction,entry,anchor", [
    ("LONG", 100.2, 100.5),  # long stop anchor above the entry
    ("SHORT", 99.8, 99.5),  # short stop anchor below the entry
])
def test_invalid_stop_placement_is_no_trade(direction, entry, anchor):
    plan = build_plan(make(direction, entry=entry, anchor=anchor, candidates=[lvl(90), lvl(110)]),
                      CFG)
    assert plan["status"] == NO_TRADE
    assert plan["reasons"][0].startswith("Invalid stop placement")
    assert check(plan, "Stop")["passed"] is False
    assert plan["risk_reward_t1"] is None and plan["target1"] is None


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_zero_risk_is_no_trade(direction):
    """Anchor at the entry with no buffer: stop == entry, risk 0 - never divided by."""
    cfg = PlanningConfig().with_overrides({"stop_buffer_atr": {"day": 0.0}})
    plan = build_plan(make(direction, entry=100.0, anchor=100.0,
                           candidates=[lvl(90), lvl(110)]), cfg)
    assert plan["status"] == NO_TRADE
    assert plan["risk_reward_t1"] is None and plan["risk_per_share"] == 0.0
    assert plan["reasons"][0].startswith("Zero risk")
    assert check(plan, "Non-zero risk")["passed"] is False


def test_negligible_risk_is_no_trade():
    cfg = PlanningConfig().with_overrides({"stop_buffer_atr": {"day": 0.0}})
    plan = build_plan(make(entry=100.02, anchor=100.0, candidates=[lvl(103)]), cfg)
    assert plan["status"] == NO_TRADE
    assert plan["reasons"][0].startswith("Zero or negligible risk")
    assert check(plan, "Risk >=")["passed"] is False


def test_stop_wider_than_max_risk_is_no_trade():
    plan = build_plan(make(entry=100.2, anchor=96.0, candidates=[lvl(120)]), CFG)  # 4.3 ATR
    assert plan["status"] == NO_TRADE
    assert any(r.startswith("Stop too wide") for r in plan["reasons"])


def test_missing_stop_anchor_is_no_trade():
    plan = build_plan(make(anchor=None, candidates=[lvl(103)]), CFG)
    assert plan["status"] == NO_TRADE and "arbitrary" in plan["reasons"][0]


# --------------------------------------------------------------- targets / R:R

def test_poor_reward_risk_is_no_trade_and_target_is_not_moved():
    """A level 0.5 above the entry caps the trade even though a level at 106 would give
    4:1 - the plan reports the nearest level instead of skipping it."""
    plan = build_plan(make(candidates=[lvl(106.0, "far zone"), lvl(100.7, "near PDH")]), CFG)
    assert plan["status"] == NO_TRADE
    assert plan["target1"]["price"] == 100.7
    assert plan["risk_reward_t1"] == pytest.approx(0.5 / 1.3, abs=0.01)
    assert plan["reasons"][0].startswith("Poor risk/reward") and "not moved" in plan["reasons"][0]
    assert plan["target2"]["price"] == 106.0


def test_min_rr_is_configurable_globally_and_per_mode():
    inp = make(candidates=[lvl(102.2)])  # 2.0 / 1.3 = 1.54
    assert build_plan(inp, CFG)["status"] == NO_TRADE
    assert build_plan(inp, PlanningConfig(min_rr=1.5))["status"] == TRIGGERED
    per_mode = PlanningConfig().with_overrides({"min_rr_by_mode": {"day": 1.5}})
    assert build_plan(inp, per_mode)["status"] == TRIGGERED
    assert per_mode.min_rr_for("swing") == 2.0


def test_exact_threshold_passes():
    plan = build_plan(make(entry=100.0, anchor=99.1, candidates=[lvl(102.0)]), CFG)  # 2.0/1.0
    assert plan["risk_reward_t1"] == pytest.approx(2.0)
    assert plan["status"] == TRIGGERED


def test_atr_projection_only_when_nothing_lies_ahead():
    t1, t2 = select_targets("LONG", 100.0, 1.0, [lvl(95.0)], CFG, "day")
    assert t1.price == pytest.approx(102.0) and t1.source == "atr_projection"
    assert "No confirmed level" in t1.basis
    assert t2.price == pytest.approx(103.0) and t2.source == "atr_extension"
    t1, t2 = select_targets("SHORT", 100.0, 2.0, [], CFG, "swing")
    assert t1.price == pytest.approx(94.0) and t2.price == pytest.approx(91.0)
    # A level ahead always wins over the projection.
    t1, _ = select_targets("LONG", 100.0, 1.0, [lvl(108.0)], CFG, "day")
    assert t1.price == 108.0


def test_target_selection_ignores_levels_at_the_entry_and_needs_a_t2_gap():
    cands = [lvl(100.01), lvl(101.0), lvl(101.1), lvl(102.0)]
    t1, t2 = select_targets("LONG", 100.0, 1.0, cands, CFG, "day")
    assert t1.price == 101.0  # 100.01 is within 0.05 ATR of the entry
    assert t2.price == 102.0  # 101.1 is closer than 0.25 ATR to T1
    # Targets do not depend on the stop: same candidates, same targets.
    a = build_plan(make(entry=100.0, anchor=99.5, candidates=cands), CFG)
    b = build_plan(make(entry=100.0, anchor=98.0, candidates=cands), CFG)
    assert a["target1"] == b["target1"] and a["target2"] == b["target2"]


# ------------------------------------------------------------ no-trade conditions

def test_stale_or_unreliable_data_is_no_trade():
    plan = build_plan(make(candidates=[lvl(103)], data_issues=["5m data: stale (12 min)"]), CFG)
    assert plan["status"] == NO_TRADE
    assert plan["reasons"][0].startswith("Unreliable or insufficient market data")
    assert plan["entry"] is None


def test_insufficient_history_without_atr_is_no_trade():
    plan = build_plan(make(atr=None, candidates=[lvl(103)]), CFG)
    assert plan["status"] == NO_TRADE and "Insufficient candle history" in plan["reasons"][0]


def test_no_valid_setup_is_no_trade_with_explanation():
    plan = build_plan(make(signal="NONE", trigger=None, entry=None,
                           notes=["No completed close beyond a zone."]), CFG)
    assert plan["status"] == NO_TRADE
    assert plan["reasons"][0] == "No valid technical setup: No completed close beyond a zone."
    assert plan["entry"] is None and plan["stop"] is None


def test_contradictory_signals_are_no_trade():
    inp = make(candidates=[lvl(104)], conflicts=["daily trend is bearish", "1-hour bearish"])
    plan = build_plan(inp, CFG)
    assert plan["status"] == NO_TRADE and plan["reasons"][0].startswith("Contradictory signals")
    one = build_plan(make(candidates=[lvl(104)], conflicts=["daily trend is bearish"]), CFG)
    assert one["status"] == TRIGGERED
    assert one["quality"]["conflict_penalty"] == 10.0


def test_wide_spread_is_no_trade_when_quotes_exist():
    plan = build_plan(make(candidates=[lvl(104)], spread_pct=0.6), CFG)
    assert plan["status"] == NO_TRADE and "spread" in plan["reasons"][0]
    plan = build_plan(make(candidates=[lvl(104)], spread_pct=0.02), CFG)
    assert plan["status"] == TRIGGERED
    plan = build_plan(make(candidates=[lvl(104)]), CFG)
    assert check(plan, "Quoted spread")["passed"] is None  # unknown is reported, not assumed


def test_strategy_entry_rule_rejection_is_reported_first():
    plan = build_plan(make(candidates=[lvl(100.5)], blocked="within 30 minutes of the close"),
                      CFG)
    assert plan["status"] == NO_TRADE
    assert plan["reasons"][0] == ("Entry rule rejected the trigger candle: within 30 minutes "
                                  "of the close.")


# ------------------------------------------------------------------ quality score

def test_quality_score_is_transparent_and_bounded():
    q = QualityInputs("aligned", "", 80, "", 1.5, "", "aligned", "")
    plan = build_plan(make(candidates=[lvl(106)], quality=q), CFG)  # R:R 4.46
    comps = {c["name"]: c for c in plan["quality"]["components"]}
    assert set(comps) == set(DEFAULT_WEIGHTS)
    assert sum(c["weight"] for c in comps.values()) == 100
    assert comps["higher_timeframe"]["points"] == 15
    assert comps["level_strength"]["points"] == pytest.approx(12.0)
    assert comps["relative_volume"]["points"] == 10
    assert comps["risk_reward"]["points"] == 25  # >= 2 x min R:R earns full points
    assert plan["quality"]["score"] == pytest.approx(sum(c["points"] for c in comps.values()))
    assert "not a predicted win probability" in plan["quality"]["note"]
    weak = build_plan(make(candidates=[lvl(106)], quality=QualityInputs(
        "opposed", "", 10, "", 0.4, "", "opposed", ""), conflicts=["x"]), CFG)
    assert weak["quality"]["score"] < plan["quality"]["score"]
    assert 0 <= weak["quality"]["score"] <= 100


def test_planning_config_validation(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        CFG.with_overrides({"min_r": 2})
    with pytest.raises(ValueError):
        CFG.with_overrides({"stop_buffer_atr": {"weekly": 1}})
    with pytest.raises(ValueError):
        CFG.with_overrides({"weights": {**DEFAULT_WEIGHTS, "risk_reward": 50}})  # sums to 125
    with pytest.raises(ValueError):
        CFG.with_overrides({"min_rr": 0})
    p = tmp_path / "planning.json"
    p.write_text(json.dumps({"min_rr": 3.0, "max_risk_atr": {"day": 2.0}}))
    monkeypatch.setenv("PLANNING_CONFIG_PATH", str(p))
    cfg = load_planning_config()
    assert cfg.min_rr == 3.0 and cfg.max_risk_atr == {"day": 2.0, "swing": 3.0}


# ------------------------------------------------------------ engine integration

def test_day_and_swing_setups_publish_exactly_their_plan(service):
    day = service.strategy_setups(["SPY", "TSLA"], record=False)["setups"]
    swing = service.swing_setups(["SPY"], record=False)["setups"]
    assert day and swing
    for s in day + swing:
        p = s["plan"]
        assert p is not None and s["status"] == p["status"]
        price = lambda k: p[k]["price"] if p.get(k) else None
        assert s["stop"] == price("stop") and s["target1"] == price("target1")
        assert s["entry"] == price("entry") and s["risk_reward"] == p["risk_reward_t1"]
        if p["status"] != NO_TRADE:
            assert p["risk_reward_t1"] >= service.planning_config.min_rr - 1e-9
            assert all(c["passed"] in (True, None) for c in p["checks"])
        if p["status"] == TRIGGERED:
            assert all(c["passed"] is not False for c in p["confirmation"] if c["required"])


def test_plans_api_and_config(client):
    r = client.get("/api/plans/SPY", params={"mode": "day"})
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "SPY" and body["mode"] == "day" and body["is_demo"] is True
    order = {"TRIGGERED": 0, "WATCHING": 1, "NO_TRADE": 2}
    ranks = [order[p["status"]] for p in body["plans"]]
    assert ranks == sorted(ranks)
    for p in body["plans"]:
        for k in ("ticker", "direction", "strategy_name", "status", "entry_trigger", "entry",
                  "stop", "target1", "target2", "risk_per_share", "reward_per_share_t1",
                  "risk_reward_t1", "confirmation", "invalidation", "checks", "quality"):
            assert k in p
    meta = client.get("/api/plans/META", params={"mode": "day"}).json()
    assert meta["symbol"] == "META" and meta["plans"]  # whole watchlist is scanned
    assert client.get("/api/plans/ZZZZ", params={"mode": "day"}).status_code == 400
    assert client.get("/api/plans/SPY", params={"mode": "weekly"}).status_code == 400
    cfg = client.get("/api/planning/config").json()
    assert cfg["config"]["min_rr"] == 2.0
    assert sum(cfg["config"]["weights"].values()) == 100
    assert "never moves a target" in cfg["rules"]
