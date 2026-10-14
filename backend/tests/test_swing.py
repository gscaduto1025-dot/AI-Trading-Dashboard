"""Swing-trading engine tests.

Positive cases are pinned to real, well-known daily events in the AAPL 2015-2017 and
GOOG 2004-2013 fixtures (see fixtures/README.md) and re-checked against values computed
independently from the raw CSV rows. Negative and edge cases mutate those real candles or
use small hand-built daily paths. No-lookahead / no-repaint tests walk forward through the
real history.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime

import pytest

from app.data.base import Bar
from app.market.calendar import NY
from app.planning.config import PlanningConfig
from app.strategies.config import StrategyConfig
from app.swing.config import SwingConfig, load_swing_config
from app.swing.context import fmt_day
from app.swing.engine import SORT_KEYS, evaluate_swing_symbol, sort_setups

from .test_key_levels import AAPL, CAL, GOOG, day_of, midnight, ref_atr, weekly

CFG = SwingConfig()
PLAN = PlanningConfig()  # default 2:1 minimum reward/risk


def at(y, m, d, hh=17, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=NY)


def frames(bars: list[Bar]) -> dict:
    return {"1Day": bars, "1Week": weekly(bars)}


def run(bars, as_of, cfg=CFG, full=False, problems=None, ticker="TEST", planning=None):
    return evaluate_swing_symbol(ticker, frames(bars), as_of, CAL, cfg, problems,
                                 full_history=full, planning=planning or PLAN)[1]


def pick(setups, strategy, tf, direction):
    return next(s for s in setups if s["strategy"] == strategy and s["timeframe"] == tf
                and s["direction"] == direction)


def ts(d: date) -> int:
    return midnight(d)


def idx(bars, d: date) -> int:
    return next(i for i, b in enumerate(bars) if day_of(b) == d)


def path(points, start=date(2019, 1, 2), vol=2_000_000, wick=0.6) -> list[Bar]:
    """Hand-built daily candles walking linearly between (bar index, close) waypoints;
    each candle opens at the previous close with `wick` above/below the body."""
    closes = [points[0][1]]
    for (n0, p0), (n1, p1) in zip(points, points[1:]):
        closes += [p0 + (p1 - p0) * j / (n1 - n0) for j in range(1, n1 - n0 + 1)]
    days = CAL.trading_days(start, date(2030, 1, 1))[:len(closes)]
    out, prev = [], closes[0]
    for d, c in zip(days, closes):
        out.append(Bar(midnight(d.day), prev, max(prev, c) + wick, min(prev, c) - wick, c, vol))
        prev = c
    return out


# Downtrend (lower highs/lows) -> break of structure -> higher low -> higher high.
REVERSAL_POINTS = [(0, 200), (10, 206), (20, 198), (30, 206), (40, 198), (50, 206), (60, 198),
                   (70, 206), (80, 200), (90, 186), (98, 194), (108, 176), (116, 190),
                   (126, 166), (134, 174), (144, 158),  # LH 174 / LL 158
                   (154, 178),  # break of structure: close above the 174 lower high
                   (162, 175),  # higher low (above 158)
                   (168, 181)]  # higher high: close above the post-break high


# ------------------------------------------------------- positives on real history

def test_daily_breakout_goog_jan_2008_triggers_with_a_level_target():
    s = pick(run(GOOG, at(2008, 1, 31)), "swing_breakout", "1Day", "LONG")
    assert s["status"] == "TRIGGERED"
    assert s["trigger_time"] == ts(date(2008, 1, 31))
    i = idx(GOOG, date(2008, 1, 31))
    b, p = GOOG[i], GOOG[i - 1]
    atr = ref_atr(GOOG[:i])  # ATR known before the breakout candle
    trig = s["entry_trigger"]
    assert p.close <= trig < b.close
    assert b.close > trig + CFG.daily_breakout.breakout_buffer_atr * atr - 1e-6
    rel_vol = b.volume / (sum(x.volume for x in GOOG[i - 20:i]) / 20)
    assert rel_vol >= CFG.daily_breakout.min_rel_volume
    assert s["entry"] == pytest.approx(b.close)
    assert s["stop"] < trig < s["entry"] < s["target1"] < s["target2"]
    assert s["risk_reward"] >= PLAN.min_rr
    # T1 is a confirmed daily zone (the nearest level above), never an R multiple.
    assert s["plan"]["target1"]["source"] == "zone"
    assert s["entry_zone"][0] == pytest.approx(trig)
    assert all(c["passed"] for c in s["confirmation"] if c["required"])
    assert s["market_structure"]["structure"] and s["market_structure"]["ema_stack"]


def test_aapl_feb_2017_breakout_is_detected_but_resistance_overhead_makes_it_no_trade():
    """2017-02-01: AAPL closed through resistance on heavy post-earnings volume - the pattern
    is detected - but a daily resistance zone sits ~0.8 above the entry. T1 stays at that
    zone (reward/risk ~0.2) instead of being pushed out to satisfy 2:1."""
    s = pick(run(AAPL, at(2017, 2, 1)), "swing_breakout", "1Day", "LONG")
    [t] = [t for t in s["triggers"] if t["time"] == ts(date(2017, 2, 1))]
    assert not t["accepted"] and "Poor risk/reward" in t["blocked"]
    assert s["status"] == "NO_TRADE"
    assert s["plan"]["target1"]["source"] == "zone"
    assert s["plan"]["target1"]["price"] - s["plan"]["entry"]["price"] < 1.0
    assert s["plan"]["risk_reward_t1"] < 0.5


def test_consolidation_breakdown_aapl_brexit_day_detected():
    """2016-06-24 (Brexit referendum result): AAPL closed below a multi-week range. The
    pattern is detected and verified against the raw candles; the plan is NO_TRADE because
    a confirmed support zone lies just below."""
    s = pick(run(AAPL, at(2016, 6, 24)), "swing_consolidation_breakout", "1Day", "SHORT")
    [t] = [t for t in s["triggers"] if t["time"] == ts(date(2016, 6, 24))]
    assert "Poor risk/reward" in t["blocked"]
    lo = next(l for l in s["supporting_levels"] if l["type"] == "RANGE_LOW")
    hi = next(l for l in s["supporting_levels"] if l["type"] == "RANGE_HIGH")
    rng = next(c for c in s["confirmation"] if c["name"].startswith("Consolidation of"))
    n = int(rng["detail"].split()[0])
    i = idx(AAPL, date(2016, 6, 24))
    window = AAPL[i - n:i]
    assert lo["low"] == pytest.approx(min(b.low for b in window), abs=1e-3)
    assert hi["high"] == pytest.approx(max(b.high for b in window), abs=1e-3)
    assert hi["high"] - lo["low"] <= CFG.consolidation_breakout.max_range_atr * ref_atr(AAPL[:i])
    assert AAPL[i - 1].close >= lo["low"] > AAPL[i].close


def test_consolidation_breakout_hand_built_triggers_with_measured_move():
    pts = [(0, 80), (70, 100)]
    k = 70
    for _ in range(4):  # four swings between 100 and 103
        pts += [(k + 6, 103), (k + 12, 100)]
        k += 12
    pts.append((k + 6, 103))
    bars = path(pts, wick=0.3)
    d = CAL.next_trading_day(day_of(bars[-1]))
    breakout = Bar(midnight(d), 103.0, 104.0, 102.8, 103.9, 5_000_000)
    s = pick(run(bars + [breakout], at(d.year, d.month, d.day)),
             "swing_consolidation_breakout", "1Day", "LONG")
    assert s["status"] == "TRIGGERED"
    plan = s["plan"]
    hi = next(l for l in s["supporting_levels"] if l["type"] == "RANGE_HIGH")["high"]
    lo = next(l for l in s["supporting_levels"] if l["type"] == "RANGE_LOW")["low"]
    assert plan["target1"]["price"] == pytest.approx(hi + (hi - lo))  # 1x measured move
    assert plan["stop"]["anchor"] == pytest.approx(102.8)  # breakout candle low
    atr = plan["atr"]["value"]
    assert plan["stop"]["price"] == pytest.approx(102.8 - 0.25 * atr, abs=1e-3)
    assert plan["risk_reward_t1"] == pytest.approx(
        (plan["target1"]["price"] - 103.9) / (103.9 - plan["stop"]["price"]), abs=0.01)


def test_sr_reversal_goog_march_2006_needs_a_confirming_close():
    s = pick(run(GOOG, at(2006, 3, 20)), "swing_sr_reversal", "1Day", "LONG")
    assert s["status"] == "TRIGGERED" and s["trigger_time"] == ts(date(2006, 3, 20))
    rev = next(e for e in s["evidence"] if e["kind"].startswith("reversal candle"))
    assert rev["time"] < s["trigger_time"]  # the reversal candle alone did not trigger
    r = GOOG[next(i for i, b in enumerate(GOOG) if b.time == rev["time"])]
    assert GOOG[idx(GOOG, date(2006, 3, 20))].close > r.high == pytest.approx(s["entry_trigger"])
    assert s["plan"]["stop"]["anchor"] == pytest.approx(r.low)  # beyond the rejection wick
    # On the reversal candle's own close the setup was only pending confirmation.
    before = pick(run(GOOG, at(*map(int, fmt_day(rev["time"]).split("-")))),
                  "swing_sr_reversal", "1Day", "LONG")
    assert before["status"] != "TRIGGERED"
    assert before["trigger_time"] != s["trigger_time"]


def test_trend_continuation_aapl_july_2016_downtrend():
    s = pick(run(AAPL, at(2016, 7, 13)), "swing_trend_continuation", "1Day", "SHORT")
    assert s["status"] == "TRIGGERED" and s["trigger_time"] == ts(date(2016, 7, 13))
    names = [c["name"] for c in s["confirmation"] if c["passed"]]
    assert any("EMA20 < EMA50 < EMA200" in n for n in names)
    assert any("lower highs and lower lows" in n for n in names)
    i = idx(AAPL, date(2016, 7, 13))
    assert AAPL[i].close < AAPL[i].open and AAPL[i].close < AAPL[i - 1].low


def test_weekly_breakout_goog_april_2008():
    s = pick(run(GOOG, at(2008, 4, 18)), "swing_breakout", "1Week", "LONG")
    assert s["status"] == "TRIGGERED"
    assert s["trigger_time"] == ts(date(2008, 4, 14))  # week of Apr 14, closed Fri Apr 18
    assert s["confirmed_at"] == int(at(2008, 4, 18, 16).timestamp())
    assert "1-year high" in s["plan"]["target1"]["basis"]


def test_trend_reversal_goog_august_2005():
    s = pick(run(GOOG, at(2005, 8, 10)), "swing_trend_reversal", "1Day", "SHORT")
    assert s["status"] == "TRIGGERED"
    kinds = [e["kind"] for e in s["evidence"]]
    assert kinds == ["last uptrend swing low", "break of structure", "lower high",
                     "lower low close"]
    times = [e["time"] for e in s["evidence"]]
    assert times == sorted(times) and times[-1] == s["trigger_time"]
    lh = next(e for e in s["evidence"] if e["kind"] == "lower high")
    assert s["plan"]["stop"]["anchor"] == pytest.approx(lh["price"])


def test_every_strategy_signals_in_real_history():
    seen: dict = {}
    for bars, end in ((AAPL, at(2017, 3, 1)), (GOOG, at(2013, 12, 31))):
        for s in run(bars, end, full=True):
            seen.setdefault(s["strategy"], [0, 0])
            seen[s["strategy"]][0] += len(s["triggers"])
            seen[s["strategy"]][1] += sum(t["accepted"] for t in s["triggers"])
    assert set(seen) == {"swing_breakout", "swing_trend_continuation",
                         "swing_consolidation_breakout", "swing_sr_reversal",
                         "swing_trend_reversal"}
    assert all(v[0] >= 1 for v in seen.values()), seen  # every pattern is detected
    # With honest targets most raw signals fail 2:1; consolidation breakouts with a 1x
    # measured move rarely clear it (covered by the hand-built test above).
    assert all(v[1] >= 1 for k, v in seen.items() if k != "swing_consolidation_breakout"), seen


def test_accepted_triggers_obey_every_rule():
    for s in run(GOOG, at(2013, 12, 31), full=True):
        sg = 1 if s["direction"] == "LONG" else -1
        for t in s["triggers"]:
            if not t["accepted"]:
                assert t["blocked"]
                continue
            assert sg * (t["entry"] - t["stop"]) > 0
            assert sg * (t["target1"] - t["entry"]) > 0 and sg * (t["target2"] - t["target1"]) > 0
            assert t["risk_reward"] >= PLAN.min_rr - 0.01
            assert t["confirmed_at"] > t["time"]


# ----------------------------------------------------------- negatives and edges

def test_forming_daily_candle_never_triggers():
    s = pick(run(GOOG, at(2008, 1, 31, 15, 0)), "swing_breakout", "1Day", "LONG")
    assert s["trigger_time"] != ts(date(2008, 1, 31))
    assert s["last_candle"] == "2008-01-30"


def test_breakout_without_volume_is_rejected():
    i = idx(GOOG, date(2008, 1, 31))
    avg = sum(b.volume for b in GOOG[i - 20:i]) / 20
    bars = GOOG[:i] + [replace(GOOG[i], volume=avg)] + GOOG[i + 1:]
    s = pick(run(bars, at(2008, 1, 31)), "swing_breakout", "1Day", "LONG")
    assert s["status"] != "TRIGGERED"
    assert any("relative volume" in r for r in s["reasons"])


def test_chasing_and_reward_risk_rules_block_the_trigger():
    tight = CFG.with_overrides({"gates": {"max_extension_atr": 0.01}})
    s = pick(run(GOOG, at(2008, 1, 31), tight), "swing_breakout", "1Day", "LONG")
    assert s["status"] != "TRIGGERED"
    assert any("chasing" in r for r in s["reasons"])
    s = pick(run(GOOG, at(2008, 1, 31), planning=PlanningConfig(min_rr=50)),
             "swing_breakout", "1Day", "LONG")
    assert s["status"] == "NO_TRADE"
    assert any("Poor risk/reward" in r for r in s["reasons"])


def test_trigger_goes_stale_after_max_age():
    s = pick(run(GOOG, at(2008, 2, 5)), "swing_breakout", "1Day", "LONG")  # 3 candles later
    assert not (s["status"] == "TRIGGERED" and s["trigger_time"] == ts(date(2008, 1, 31)))
    assert any("2008-01-31" in r for r in s["reasons"])


def test_trend_reversal_full_sequence_triggers_on_hand_built_path():
    bars = path(REVERSAL_POINTS)
    s = pick(run(bars, at(2019, 9, 3)), "swing_trend_reversal", "1Day", "LONG")
    assert s["status"] == "TRIGGERED"
    by = {c["name"]: c for c in s["confirmation"]}
    assert by["Prior downtrend (confirmed swings)"]["passed"]
    assert by["Confirmed higher low"]["passed"]
    assert by["Moving-average shift (close beyond EMA20, EMA20 turning)"]["passed"]
    hl = next(e for e in s["evidence"] if e["kind"] == "higher low")
    assert s["stop"] < hl["price"] < s["entry_trigger"] < s["entry"]


def test_one_break_of_structure_is_not_a_reversal():
    bars = path(REVERSAL_POINTS[:-2])  # ends right after the break, no higher low yet
    s = pick(run(bars, at(2030, 1, 1)), "swing_trend_reversal", "1Day", "LONG")
    assert s["status"] == "NO_TRADE"
    assert not any(t["accepted"] for t in s["triggers"])
    assert any("not a reversal" in r for r in s["reasons"])


def test_moving_average_shift_is_required_for_reversal():
    bars = path(REVERSAL_POINTS)
    cfg = CFG.with_overrides({"trend_reversal": {"ema_slope_bars": 60}})
    s = pick(run(bars, at(2019, 9, 3), cfg), "swing_trend_reversal", "1Day", "LONG")
    assert s["status"] != "TRIGGERED"
    assert any("missing confirmation: moving-average shift" in r.lower() for r in s["reasons"])


def test_gates_force_no_trade():
    thin = [replace(b, volume=b.volume * 1e-4) for b in AAPL]
    assert all(s["status"] == "NO_TRADE" and "Insufficient liquidity" in s["reasons"][0]
               for s in run(thin, at(2017, 2, 1)))
    assert all("Fewer than 60" in s["reasons"][0] for s in run(AAPL[:50], at(2030, 1, 1)))
    problems = ["Daily data: stale"]
    assert all(s["status"] == "NO_TRADE" and s["reasons"] == problems
               for s in run(AAPL, at(2017, 2, 1), problems=problems))


# --------------------------------------------------- lookahead, repainting, determinism

@pytest.mark.parametrize("bars,as_of", [(AAPL, at(2016, 6, 24)), (AAPL, at(2017, 2, 1)),
                                        (GOOG, at(2008, 10, 10)), (GOOG, at(2012, 11, 28))])
def test_future_candles_change_nothing(bars, as_of):
    past = [b for b in bars if day_of(b) <= as_of.date()]
    future = bars[len(past):]
    wild = [replace(b, open=b.open * 3, high=b.high * 3, low=b.low * 0.2, close=b.close * 0.3,
                    volume=b.volume * 50) for b in future]
    base = run(past, as_of)
    assert run(bars, as_of) == base
    assert run(past + wild, as_of) == base


def test_triggers_never_repaint_walking_forward():
    seen: dict = {}
    for k in range(300, len(AAPL), 3):
        d = day_of(AAPL[k])
        for s in run(AAPL[:k + 1], at(d.year, d.month, d.day)):
            for t in s["triggers"]:
                key = (s["strategy"], s["timeframe"], s["direction"], t["time"])
                assert seen.setdefault(key, t) == t, key


def test_deterministic_and_independent_strategies():
    a = run(GOOG, at(2012, 11, 28))
    b = run(GOOG, at(2012, 11, 28))
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    # Disabling one strategy leaves every other strategy's output untouched.
    off = CFG.with_overrides({"sr_reversal": {"enabled": False}})
    c = run(GOOG, at(2012, 11, 28), off)
    assert not any(s["strategy"] == "swing_sr_reversal" for s in c)
    key = lambda s: (s["strategy"], s["timeframe"], s["direction"])
    assert sorted(c, key=key) == sorted((s for s in a if s["strategy"] != "swing_sr_reversal"),
                                        key=key)


# ------------------------------------------------------------- sorting and config

def test_sorting_by_every_key():
    setups = run(GOOG, at(2012, 11, 28)) + run(AAPL, at(2017, 2, 1), ticker="AAPL")
    for key in SORT_KEYS:
        out = sort_setups(setups, key)
        active = [s for s in out if s["status"] != "NO_TRADE"]
        assert out[:len(active)] == active  # active setups always first
        if key == "quality":
            assert [s["quality"]["score"] for s in active] == sorted(
                (s["quality"]["score"] for s in active), reverse=True)
        if key == "distance":
            d = [abs(s["distance_to_trigger_pct"]) for s in active]
            assert d == sorted(d)
        if key == "ticker":
            assert [s["ticker"] for s in active] == sorted(s["ticker"] for s in active)
    with pytest.raises(ValueError):
        sort_setups(setups, "probability")


def test_swing_config_is_separate_and_validated(tmp_path, monkeypatch):
    cfg = CFG.with_overrides({"gates": {"max_trigger_age": {"1Day": 5}}})
    assert cfg.gates.max_trigger_age == {"1Day": 5, "1Week": 1}  # per-timeframe merge
    with pytest.raises(ValueError):
        CFG.with_overrides({"orb": {}})
    with pytest.raises(ValueError):
        CFG.with_overrides({"daily_breakout": {"min_rel_vol": 2}})
    p = tmp_path / "swing.json"
    p.write_text(json.dumps({"daily_breakout": {"min_rel_volume": 3.0}}))
    monkeypatch.setenv("SWING_STRATEGY_CONFIG_PATH", str(p))
    assert load_swing_config().daily_breakout.min_rel_volume == 3.0
    with pytest.raises(ValueError):  # planning rules are not swing-strategy settings
        CFG.with_overrides({"targets": {"min_rr": 3.0}})
    assert StrategyConfig().to_dict() != load_swing_config().to_dict()
    assert "breakout_retest" in StrategyConfig().to_dict()
    assert "breakout_retest" not in load_swing_config().to_dict()


# ------------------------------------------------------------------------- API

def test_swing_api_scanner_sorting_and_config(client):
    r = client.get("/api/swing/setups", params={"symbol": "SPY", "sort": "distance"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "swing" and body["is_demo"] is True and body["sort"] == "distance"
    assert body["timeframes"] == ["1Day", "1Week"]
    assert {s["strategy"] for s in body["setups"]} == {
        "swing_breakout", "swing_trend_continuation", "swing_consolidation_breakout",
        "swing_sr_reversal", "swing_trend_reversal"}
    assert len(body["setups"]) == 5 * 2 * 2  # strategies x timeframes x directions
    for s in body["setups"]:
        assert s["ticker"] == "SPY" and s["is_demo"] is True
        assert s["status"] in ("TRIGGERED", "WATCHING", "NO_TRADE")
        assert s["reasons"]
        if s["status"] != "NO_TRADE":
            assert None not in (s["entry_trigger"], s["stop"], s["target1"], s["target2"],
                                s["risk_reward"], s["entry_zone"])
    assert client.get("/api/swing/setups", params={"sort": "probability"}).status_code == 400
    cfg = client.get("/api/swing/config").json()
    assert set(cfg["config"]) == {"gates", "daily_breakout", "trend_continuation",
                                  "consolidation_breakout", "sr_reversal", "trend_reversal"}
    assert len(cfg["definitions"]) == 5 and all(d["rules"] for d in cfg["definitions"])
    # The day-trading scanner and its configuration are untouched.
    day = client.get("/api/strategies/config").json()
    assert "breakout_retest" in day["config"] and "daily_breakout" not in day["config"]


def test_swing_replay_is_point_in_time(client):
    r = client.get("/api/swing/setups", params={"symbol": "AAPL", "as_of": "2026-02-20T17:00"})
    assert r.status_code == 200
    body = r.json()
    assert body["replay"] is True and body["new_signals"] == []
    assert all(s["last_candle"] <= "2026-02-20" for s in body["setups"])
