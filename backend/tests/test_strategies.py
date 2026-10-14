"""Day-trading strategy engine: positive, negative and edge cases, plus no-repaint,
determinism, independence and gate tests. Fixtures are described in strategy_fixtures.py
(previous day high PDH = 101, low PDL = 99, 5-minute ATR ~0.42, daily ATR ~0.84)."""

import json
from datetime import datetime

import pytest

from app.market.calendar import NY
from app.planning.config import PlanningConfig
from app.strategies.common import NO_TRADE, TRIGGERED, WATCHING
from app.strategies.config import StrategyConfig
from tests.strategy_fixtures import (DAY, after_bars, bar_time, frames, pick, run, t)

FLAT = (100.0, 100.2, 99.8, 100.0, 1000.0)
LEAD = [(100, 100.4, 99.9, 100.3, 1000), (100.3, 100.7, 100.2, 100.6, 1000)]


def required_ok(setup) -> bool:
    return all(c.passed in (True, None) for c in setup.confirmation if c.required)


# ------------------------------------------------------------- 1. breakout & retest

BR_BREAK = LEAD + [(100.6, 100.95, 100.5, 100.9, 1000),          # 2 approach (close <= 101.04)
                   (100.9, 101.6, 100.85, 101.5, 2500)]           # 3 breakout, rel vol ~2.4
BR_FULL = BR_BREAK + [(101.5, 101.7, 101.3, 101.4, 1200),         # 4
                      (101.4, 101.45, 101.05, 101.2, 1000),       # 5 retest of 101.04
                      (101.2, 101.8, 101.15, 101.7, 1500)]        # 6 close > 101.45: trigger


def test_breakout_retest_triggers_on_confirmation_close():
    _, setups = run(BR_FULL)
    s = pick(setups, "breakout_retest", "LONG")
    assert s.status == TRIGGERED
    assert s.trigger_time == bar_time(6) and s.confirmed_at == bar_time(7)
    assert s.entry == pytest.approx(101.7)
    assert 100.8 < s.stop < 101.0  # below the zone (PDH 101 - 0.1 ATR) by a 0.1 ATR buffer
    # No level lies above this session, so T1 is an ATR projection (stated in its basis).
    assert s.target1 > s.entry and s.risk_reward >= 1.0
    assert "ATR projection" in s.plan["target1"]["basis"]
    assert required_ok(s)
    names = [c.name for c in s.confirmation]
    for needle in ("Confirmed zone", "closed beyond the zone", "relative volume", "Retest",
                   "Entry confirmation"):
        assert any(needle in n for n in names), needle
    assert s.supporting_levels[0]["type"] == "PDH"
    assert any("invalidates" in x for x in s.invalidation)
    assert [e["kind"] for e in s.evidence] == ["breakout", "retest", "confirmation"]


def test_breakout_without_retest_is_watching_then_expires():
    _, setups = run(BR_BREAK)
    s = pick(setups, "breakout_retest", "LONG")
    assert s.status == WATCHING and "waiting for retest" in s.reasons[0]
    # 13 more candles holding well above the zone: the retest window (12) lapses.
    running = BR_BREAK + [(101.5 + 0.05 * i, 101.7 + 0.05 * i, 101.45 + 0.05 * i,
                           101.6 + 0.05 * i, 1000) for i in range(13)]
    s = pick(run(running)[1], "breakout_retest", "LONG")
    assert s.status == NO_TRADE


def test_failed_breakout_is_no_trade():
    rows = BR_BREAK + [(101.5, 101.5, 100.7, 100.8, 1500)]  # closes back below the zone
    s = pick(run(rows)[1], "breakout_retest", "LONG")
    assert s.status == NO_TRADE


def test_breakout_on_weak_volume_is_rejected():
    rows = BR_FULL[:3] + [(100.9, 101.6, 100.85, 101.5, 1100)] + BR_FULL[4:]
    s = pick(run(rows)[1], "breakout_retest", "LONG")
    assert s.status == NO_TRADE
    assert any("relative volume" in r for r in s.reasons)


def test_breakout_volume_condition_is_na_when_feed_has_no_volume():
    rows = [r[:4] + (0.0,) for r in BR_FULL]
    s = pick(run(rows)[1], "breakout_retest", "LONG")
    assert s.status == TRIGGERED
    vol = next(c for c in s.confirmation if "relative volume" in c.name)
    assert vol.passed is None and "unavailable" in vol.detail


# ---------------------------------------------------------- 2. opening range breakout

ORB = [(100, 100.4, 100, 100.3, 3000), (100.3, 100.8, 100.2, 100.6, 2000),   # range 100-100.8
       (100.6, 100.7, 100.3, 100.5, 1500), (100.5, 100.75, 100.4, 100.7, 1000),
       (100.7, 100.95, 100.65, 100.86, 2500)]                                # 4 breakout


ORB_DOWN = [(100.5, 100.8, 100.4, 100.6, 3000), (100.6, 100.7, 100.0, 100.2, 2000),  # 100-100.8
            (100.2, 100.5, 100.1, 100.4, 1500), (100.4, 100.5, 100.1, 100.2, 1000),
            (100.2, 100.25, 99.85, 99.9, 2500)]                                # 4 breakdown


def test_orb_short_triggers_with_measured_targets():
    s = pick(run(ORB_DOWN, trend="down")[1], "opening_range_breakout", "SHORT")
    assert s.status == TRIGGERED and s.trigger_time == bar_time(4)
    assert s.entry == pytest.approx(99.9)
    assert s.target1 == pytest.approx(99.9 - 0.8)  # 1x range measured move (nearest ahead)
    assert s.target2 == pytest.approx(99.9 - 1.6)
    assert 100.4 < s.stop < 100.5  # range midpoint 100.4 + 0.1 ATR
    assert s.supporting_levels[0]["low"] == 100 and s.supporting_levels[0]["high"] == 100.8
    assert required_ok(s)


def test_orb_long_into_pdh_is_no_trade_target_not_moved():
    """The breakout closes 0.14 below PDH 101: PDH is the first target, so reward/risk is
    poor. The plan reports that instead of skipping PDH for a farther measured move."""
    s = pick(run(ORB)[1], "opening_range_breakout", "LONG")
    assert s.status == NO_TRADE
    assert s.plan["target1"]["price"] == pytest.approx(101.0)
    assert "Previous day high" in s.plan["target1"]["basis"]
    assert any("Poor risk/reward" in r and "not moved" in r for r in s.reasons)


def test_orb_opposite_direction_is_no_trade():
    s = pick(run(ORB)[1], "opening_range_breakout", "SHORT")
    assert s.status == NO_TRADE and "first confirmed breakout was up" in s.reasons[0]


def test_orb_no_trade_while_range_forms():
    # The range edge (the entry trigger) is not defined yet: insufficient information.
    s = pick(run(ORB[:2])[1], "opening_range_breakout", "LONG")
    assert s.status == NO_TRADE and "forming" in s.reasons[0]
    assert "no objective entry trigger" in s.reasons[0]


def test_orb_30_minute_range_is_configurable():
    cfg = StrategyConfig().with_overrides({"opening_range_breakout": {"range_minutes": 30}})
    s = pick(run(ORB, cfg=cfg)[1], "opening_range_breakout", "LONG")
    assert s.status == NO_TRADE  # 5 of 6 range candles so far: range still forming
    assert "30 minutes" in s.reasons[0]


def test_orb_breakout_after_cutoff_is_no_trade():
    inside = [(100.4, 100.6, 100.2, 100.4, 1000)] * 22  # 09:45 .. 11:35 inside the range
    rows = ORB[:3] + inside + [(100.5, 101.0, 100.45, 100.9, 3000)]  # 11:35 breakout
    s = pick(run(rows)[1], "opening_range_breakout", "LONG")
    assert s.status == NO_TRADE and "before 11:30" in s.reasons[0]


def test_orb_abnormally_wide_range_is_no_trade():
    rows = [(100, 102.2, 100, 101, 3000)] + ORB[1:]
    s = pick(run(rows)[1], "opening_range_breakout", "LONG")
    assert s.status == NO_TRADE and "abnormal" in s.reasons[0]


def test_orb_weak_volume_breakout_does_not_trigger():
    rows = ORB[:4] + [(100.7, 100.95, 100.65, 100.86, 500)]
    s = pick(run(rows)[1], "opening_range_breakout", "LONG")
    assert s.status != TRIGGERED
    assert any("relative volume" in r for r in s.reasons)


# --------------------------------------------------------- 3. liquidity sweep reversal

SWEEP = LEAD + [(100.6, 100.95, 100.5, 100.9, 1000),
                (100.9, 101.15, 100.6, 100.65, 2000)]  # 3: high 101.15 > PDH, bearish close back


def test_sweep_of_pdh_reverses_short():
    s = pick(run(SWEEP)[1], "liquidity_sweep_reversal", "SHORT")
    assert s.status == TRIGGERED and s.trigger_time == bar_time(3)
    assert s.entry == pytest.approx(100.65)
    assert s.stop > 101.15  # beyond the sweep extreme
    assert s.target1 < s.entry and required_ok(s)
    assert "Previous day high" in s.supporting_levels[0]["label"]
    assert pick(run(SWEEP)[1], "liquidity_sweep_reversal", "LONG").status != TRIGGERED


def test_sweep_confirmed_on_the_next_candle():
    rows = SWEEP[:3] + [(100.9, 101.15, 100.85, 101.05, 2000),   # sweep, closes above
                        (101.05, 101.1, 100.7, 100.75, 1800)]    # back below, bearish
    s = pick(run(rows)[1], "liquidity_sweep_reversal", "SHORT")
    assert s.status == TRIGGERED and s.trigger_time == bar_time(4)


def test_breakout_that_holds_is_not_a_sweep():
    rows = SWEEP[:3] + [(100.9, 101.15, 100.85, 101.1, 2000), (101.1, 101.2, 101.05, 101.15, 1500),
                        (101.15, 101.25, 101.1, 101.2, 1500), (101.2, 101.3, 101.15, 101.25, 1500)]
    s = pick(run(rows)[1], "liquidity_sweep_reversal", "SHORT")
    assert s.status != TRIGGERED
    assert any("no close back inside" in r for r in s.reasons)


def test_too_deep_penetration_is_a_breakout_not_a_sweep():
    rows = SWEEP[:3] + [(100.9, 101.8, 100.6, 100.65, 3000)]  # 0.8 beyond, > 1 ATR (~0.42)
    s = pick(run(rows)[1], "liquidity_sweep_reversal", "SHORT")
    assert s.status != TRIGGERED
    assert any("not a sweep" in r for r in s.reasons)


# ------------------------------------------------------ 4. trend continuation pullback

RALLY = [(100 + 0.15 * k, 100 + 0.15 * k + 0.2, 100 + 0.15 * k - 0.03, 100 + 0.15 * k + 0.15, 1500)
         for k in range(10)]
PULLBACK = [(101.5, 101.52, 101.25, 101.3, 900), (101.3, 101.35, 101.05, 101.1, 900),
            (101.1, 101.15, 100.78, 100.95, 900)]  # three lower highs into VWAP (~100.84)
CONFIRM = [(100.95, 101.4, 100.92, 101.35, 1400)]  # bullish close above prior high


LOW_RR = PlanningConfig(min_rr=0.3)  # isolates the pattern rules from the session-high target


def test_trend_pullback_triggers_with_htf_alignment():
    s = pick(run(RALLY + PULLBACK + CONFIRM, planning=LOW_RR)[1], "trend_pullback", "LONG")
    assert s.status == TRIGGERED and s.trigger_time == bar_time(13)
    assert s.htf_bias["label"] == "bullish"
    assert any("touched session VWAP" in c.name for c in s.confirmation)
    assert s.stop < 100.78 and required_ok(s)


def test_trend_pullback_below_session_high_is_poor_reward_risk():
    """Valid pattern, but the session high 101.55 sits 0.20 above the 101.35 entry with
    ~0.60 of risk: T1 is that swing level and the plan is NO_TRADE."""
    s = pick(run(RALLY + PULLBACK + CONFIRM)[1], "trend_pullback", "LONG")
    assert s.status != TRIGGERED
    [t] = s.triggers_today
    assert not t["accepted"] and t["target1"] == pytest.approx(101.55)
    assert "Poor risk/reward" in t["blocked"] and "session high" in t["blocked"]


def test_trend_pullback_blocked_when_htf_is_bearish():
    s = pick(run(RALLY + PULLBACK + CONFIRM, trend="down")[1], "trend_pullback", "LONG")
    assert s.status == NO_TRADE
    assert any("bias is bearish" in r for r in s.reasons)


def test_bullish_indicators_alone_do_not_trigger():
    # Straight rally: EMA9 > EMA20 and price > VWAP on every candle, but no pullback.
    s = pick(run(RALLY + [(101.5, 101.75, 101.48, 101.7, 1500)])[1], "trend_pullback", "LONG")
    assert s.status != TRIGGERED


def test_htf_requirement_is_configurable():
    cfg = StrategyConfig().with_overrides({"trend_pullback": {"require_htf_alignment": False}})
    s = pick(run(RALLY + PULLBACK + CONFIRM, trend="down", cfg=cfg, planning=LOW_RR)[1],
             "trend_pullback", "LONG")
    assert s.status == TRIGGERED
    # Still recorded as a conflicting signal and scored lower.
    assert any("daily trend is bearish" in c for c in s.plan["conflicts"])


# --------------------------------------------------------- 5. support/resistance rejection

REJECT = LEAD + [(100.6, 100.9, 100.5, 100.85, 1000),
                 (100.88, 101.02, 100.84, 100.86, 1500),   # 3 pin bar into PDH zone
                 (100.85, 100.87, 100.6, 100.65, 1500)]    # 4 close < 100.84: confirmation


def test_rejection_at_resistance_triggers_short():
    s = pick(run(REJECT)[1], "sr_rejection", "SHORT")
    assert s.status == TRIGGERED and s.trigger_time == bar_time(4)
    assert s.entry == pytest.approx(100.65) and s.stop > 101.02
    rej = next(c for c in s.confirmation if "rejection candle" in c.name)
    assert "pin bar" in rej.detail and required_ok(s)


def test_rejection_waits_for_confirmation():
    s = pick(run(REJECT[:4])[1], "sr_rejection", "SHORT")
    assert s.status == WATCHING and "waiting" in s.reasons[0]


def test_no_objective_rejection_candle():
    rows = REJECT[:3] + [(100.88, 101.02, 100.84, 100.98, 1500), REJECT[4]]  # closes near high
    assert pick(run(rows)[1], "sr_rejection", "SHORT").status != TRIGGERED


def test_rejection_invalidated_by_close_above_zone():
    rows = REJECT[:4] + [(100.9, 101.3, 100.88, 101.2, 1500), REJECT[4]]
    assert pick(run(rows)[1], "sr_rejection", "SHORT").status != TRIGGERED


# ---------------------------------------------------------------------------- gates

def test_stale_or_invalid_data_blocks_every_strategy():
    _, setups = run(BR_FULL, problems=["5-minute data: Market data is stale."])
    assert setups and all(s.status == NO_TRADE for s in setups)
    assert all("stale" in s.reasons[0] for s in setups)


def test_insufficient_liquidity_blocks_every_strategy():
    _, setups = run(BR_FULL, daily_volume=1000)
    assert all(s.status == NO_TRADE for s in setups)
    assert "Insufficient liquidity" in setups[0].reasons[0]


def test_abnormal_candle_cannot_trigger():
    rows = BR_FULL[:6] + [(101.2, 103.5, 101.15, 101.7, 9000)]  # range 2.35 > 4 x ATR
    s = pick(run(rows)[1], "breakout_retest", "LONG")
    assert s.status == NO_TRADE
    assert any("abnormal candle" in r for r in s.reasons)
    assert s.triggers_today and s.triggers_today[0]["accepted"] is False


def test_no_new_entries_near_the_close():
    quiet = [(100.4, 100.6, 100.3, 100.5, 1000)] * 72  # 09:30 .. 15:25 (72 candles)
    rows = quiet + [(100.5, 100.95, 100.45, 100.9, 1000), (100.9, 101.15, 100.6, 100.65, 2000)]
    s = pick(run(rows)[1], "liquidity_sweep_reversal", "SHORT")  # trigger candle ends 15:40
    assert s.status == NO_TRADE
    assert any("within 30 minutes" in r for r in s.reasons)


def test_abnormal_opening_gap_blocks_every_strategy():
    rows = [(104, 104.4, 103.9, 104.3, 3000)] + [(104.3, 104.5, 104.1, 104.4, 1000)] * 4
    _, setups = run(rows)
    assert all(s.status == NO_TRADE for s in setups)
    assert "Abnormal opening gap" in setups[0].reasons[0]


def test_watching_becomes_no_trade_after_the_session_closes():
    _, setups = run(BR_BREAK, as_of=datetime(2026, 3, 10, 16, 30, tzinfo=NY))
    s = pick(setups, "breakout_retest", "LONG")
    assert s.status == NO_TRADE and s.reasons[0].startswith("Session closed before confirmation")


# --------------------------------------------- no lookahead, no repainting, determinism

LONG_DAY = BR_FULL + [(101.7, 101.9, 101.5, 101.8, 1000)] * 8 + REJECT  # several patterns


def _accepted(setups):
    return {(s.strategy, s.timeframe, s.direction): [x for x in s.triggers_today if x["accepted"]]
            for s in setups}


def test_no_repainting_triggers_only_accumulate():
    previous: dict = {}
    first_seen: dict = {}
    for n in range(1, len(LONG_DAY) + 1):
        _, setups = run(LONG_DAY, as_of=after_bars(n))
        now = _accepted(setups)
        for key, trig in now.items():
            before = previous.get(key, [])
            assert trig[:len(before)] == before, f"{key} repainted at bar {n}"
            for x in trig:
                first_seen.setdefault((key, x["time"]), n)
                assert x["confirmed_at"] <= after_bars(n).timestamp()  # never before its close
        previous = now
    # The breakout-retest long first appears exactly when its confirmation candle closes.
    assert first_seen[(("breakout_retest", "5Min", "LONG"), bar_time(6))] == 7


def test_future_candles_do_not_change_results():
    as_of = after_bars(7)
    past = run(BR_FULL, as_of=as_of)[1]
    with_future = run(LONG_DAY, as_of=as_of)[1]
    dump = lambda xs: json.dumps([s.to_dict() for s in xs], sort_keys=True)  # noqa: E731
    assert dump(past) == dump(with_future)


def test_deterministic_output():
    a = json.dumps([s.to_dict() for s in run(LONG_DAY)[1]], sort_keys=True)
    b = json.dumps([s.to_dict() for s in run(LONG_DAY)[1]], sort_keys=True)
    assert a == b


def test_strategies_are_independent():
    full = {s.id: s.to_dict() for s in run(LONG_DAY)[1] if s.strategy == "breakout_retest"}
    cfg = StrategyConfig()
    only = cfg.with_overrides({k: {"enabled": False} for k in (
        "opening_range_breakout", "liquidity_sweep", "trend_pullback", "sr_rejection")})
    alone = run(LONG_DAY, cfg=only)[1]
    assert {s.strategy for s in alone} == {"breakout_retest"}
    # Detection is independent; only the shared plan layer sees other strategies' signals
    # (an opposite-direction trigger on the same timeframe counts as a conflicting signal).
    strip = lambda d: {k: v for k, v in d.items() if k != "plan"}
    assert {s.id: strip(s.to_dict()) for s in alone} == {k: strip(v) for k, v in full.items()}


def test_every_setup_has_the_required_fields():
    _, setups = run(LONG_DAY)
    assert len(setups) == 5 * 2 * 2  # strategies x timeframes x directions
    for s in setups:
        d = s.to_dict()
        for k in ("ticker", "strategy", "direction", "timeframe", "htf_bias", "status", "entry",
                  "stop", "target1", "target2", "risk_reward", "supporting_levels",
                  "confirmation", "invalidation", "evidence", "reasons", "id"):
            assert k in d
        assert d["direction"] in ("LONG", "SHORT")
        assert d["status"] in (TRIGGERED, WATCHING, NO_TRADE)
        if d["status"] == TRIGGERED:
            assert None not in (d["entry"], d["stop"], d["target1"], d["target2"], d["risk_reward"])
        assert d["reasons"]


def test_config_rejects_unknown_keys():
    with pytest.raises(ValueError):
        StrategyConfig().with_overrides({"opening_range_breakout": {"range_mins": 15}})
    with pytest.raises(ValueError):
        StrategyConfig().with_overrides({"nonsense": {}})
    assert "targets" not in StrategyConfig().to_dict()  # planning rules live in PlanningConfig
    assert PlanningConfig().min_rr == 2.0


# ------------------------------------------------------------------------------ API

def test_api_strategy_setups_demo(client):
    j = client.get("/api/strategies/setups").json()
    assert j["universe"] == ["SPY", "QQQ", "NVDA", "TSLA", "AAPL", "AMD", "MSFT"]
    assert len(j["setups"]) == 7 * 5 * 2 * 2
    assert all(s["is_demo"] for s in j["setups"])
    cfg = client.get("/api/strategies/config").json()
    assert [d["id"] for d in cfg["definitions"]] == [
        "breakout_retest", "opening_range_breakout", "liquidity_sweep_reversal",
        "trend_pullback", "sr_rejection"]
    assert "Entry confirmation" in cfg["definitions"][0]["rules"]
    trig = [s for s in j["setups"] if s["status"] == "TRIGGERED"]
    signals = client.get("/api/strategies/signals").json()["signals"]
    assert len(signals) >= len(trig)  # every accepted trigger is logged (earlier ones too)
    again = client.get("/api/strategies/setups").json()
    assert again["setups"] == j["setups"]  # cached + deterministic


def test_api_strategy_replay(client):
    j = client.get("/api/strategies/setups",
                   params={"symbol": "SPY", "as_of": "2026-03-10T11:00:00"}).json()
    assert j["replay"] and j["evaluated_at"].startswith("2026-03-10T11:00")
    assert all(s["session_date"] == "2026-03-10" for s in j["setups"])
    assert all((s["confirmed_at"] or 0) <= datetime(2026, 3, 10, 11, tzinfo=NY).timestamp()
               for s in j["setups"])


def test_fixture_sanity():
    f = frames(BR_FULL)
    assert f["5Min"][-1].time == bar_time(6) and t(DAY, 9, 30) == bar_time(0)
