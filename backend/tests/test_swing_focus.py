"""Focused tests for the three swing strategies with the thinnest coverage (Phase 2 audit):
swing_consolidation_breakout, swing_sr_reversal and swing_trend_continuation.

Each strategy is checked for TRIGGERED, WATCHING (where the strategy has that state) and
NO_TRADE outcomes; plan arithmetic is recomputed independently from the candles; and
missing/stale data, forming candles, invalid plans (reward/risk, chasing) and exact
boundaries are exercised. Real cases use the bundled GOOG/AAPL daily history; the rest
use small hand-built daily paths. No strategy rule is changed here.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from app.data.base import Bar
from app.planning.config import PlanningConfig

from .test_key_levels import AAPL, CAL, GOOG, day_of, midnight
from .test_swing import CFG, PLAN, at, idx, path, pick, run, ts

CB, SR, TC = "swing_consolidation_breakout", "swing_sr_reversal", "swing_trend_continuation"
STALE = ["Daily data: stale"]
MISSING = ["Daily data: 3 sessions missing"]


def check_plan_math(s: dict) -> None:
    """Independent recomputation of a complete plan's arithmetic."""
    p = s["plan"]
    sgn = 1 if s["direction"] == "LONG" else -1
    entry, stop = p["entry"]["price"], p["stop"]["price"]
    t1, t2 = p["target1"]["price"], p["target2"]["price"]
    assert sgn * (entry - stop) > 0, "stop on the wrong side of the entry"
    assert sgn * (t1 - entry) > 0, "target 1 on the wrong side of the entry"
    assert sgn * (t2 - t1) >= 0, "target 2 before target 1"
    risk, reward = sgn * (entry - stop), sgn * (t1 - entry)
    assert p["risk_per_share"] == pytest.approx(risk, abs=1e-3)
    assert p["reward_per_share_t1"] == pytest.approx(reward, abs=1e-3)
    assert p["risk_reward_t1"] == pytest.approx(reward / risk, abs=0.006)
    assert p["risk_reward_t2"] == pytest.approx(sgn * (t2 - entry) / risk, abs=0.006)
    assert p["risk_reward_worst_fill"] <= p["risk_reward_t1"]
    # The setup's summary fields are the plan's numbers.
    for k in ("entry", "stop", "target1", "target2"):
        assert s[k] == pytest.approx(p[k]["price"])
    if s["status"] == "TRIGGERED":
        assert p["risk_reward_t1"] >= p["min_rr"]
    # Stop buffer: anchor -/+ buffer_atr x ATR.
    st = p["stop"]
    assert st["price"] == pytest.approx(st["anchor"] - sgn * st["buffer_atr"] * p["atr"]["value"],
                                        abs=1e-3)


def wild_future(bars: list[Bar], n: int = 15) -> list[Bar]:
    """Absurd candles after the last one, to prove nothing reads the future."""
    out, last = [], bars[-1]
    d = day_of(last)
    for _ in range(n):
        d = CAL.next_trading_day(d)
        out.append(Bar(midnight(d), last.close * 3, last.close * 3.5, last.close * 0.2,
                       last.close * 0.3, last.volume * 50))
    return out


# ------------------------------------------------------------ consolidation breakout

def consolidation(up: bool = True) -> list[Bar]:
    """A 40-candle range between 100 and 103 (wicks 99.7-103.3) after a trend."""
    lo, hi = 100, 103
    pts = [(0, 80 if up else 125), (70, lo if up else hi)]
    k = 70
    for _ in range(4):
        pts += [(k + 6, hi if up else lo), (k + 12, lo if up else hi)]
        k += 12
    pts.append((k + 6, hi if up else lo))
    return path(pts, wick=0.3)


def next_day(bars):
    return CAL.next_trading_day(day_of(bars[-1]))


def when(d: date, hh: int = 17):
    return at(d.year, d.month, d.day, hh)


BASE = consolidation()
BREAK_DAY = next_day(BASE)
BREAKOUT = Bar(midnight(BREAK_DAY), 103.0, 104.0, 102.8, 103.9, 5_000_000)


def test_consolidation_watching_then_triggered_with_independent_math():
    last = day_of(BASE[-1])
    w = pick(run(BASE, when(last)), CB, "1Day", "LONG")
    assert w["status"] == "WATCHING"
    assert "waiting for a completed close above 103.30" in w["reasons"][0]
    assert w["entry_trigger"] == pytest.approx(103.3)

    s = pick(run(BASE + [BREAKOUT], when(BREAK_DAY)), CB, "1Day", "LONG")
    assert s["status"] == "TRIGGERED" and s["trigger_time"] == ts(BREAK_DAY)
    hi, lo = 103.3, 99.7  # range edges including wicks
    assert s["entry"] == pytest.approx(103.9)  # confirmation close
    assert s["plan"]["stop"]["anchor"] == pytest.approx(102.8)  # breakout candle low
    assert s["target1"] == pytest.approx(hi + (hi - lo))  # 1x measured move = 106.9
    check_plan_math(s)


def test_consolidation_breakdown_short_is_the_mirror_image():
    base = consolidation(up=False)
    d = next_day(base)
    brk = Bar(midnight(d), 100.0, 100.2, 99.0, 99.1, 5_000_000)
    s = pick(run(base + [brk], when(d)), CB, "1Day", "SHORT")
    assert s["status"] == "TRIGGERED"
    assert s["target1"] < s["entry"] < s["stop"]
    assert s["plan"]["stop"]["anchor"] == pytest.approx(100.2)  # breakdown candle high
    check_plan_math(s)


def test_consolidation_no_trade_and_boundaries():
    avg = sum(b.volume for b in BASE[-20:]) / 20
    weak = pick(run(BASE + [replace(BREAKOUT, volume=avg)], when(BREAK_DAY)), CB, "1Day", "LONG")
    assert weak["status"] == "NO_TRADE"  # relative volume 1.0 does not confirm a breakout
    trend = path([(0, 80), (150, 130)], wick=0.3)
    s = pick(run(trend, at(2030, 1, 1)), CB, "1Day", "LONG")
    assert s["status"] == "NO_TRADE" and "No qualifying consolidation range" in s["reasons"][0]
    # A close exactly at the range high is not a breakout.
    edge = replace(BREAKOUT, close=103.3, high=103.4)
    assert pick(run(BASE + [edge], when(BREAK_DAY)), CB, "1Day", "LONG")["status"] != "TRIGGERED"
    # The breakout candle has not closed at 15:00: no trigger from a forming candle.
    forming = pick(run(BASE + [BREAKOUT], when(BREAK_DAY, 15)), CB, "1Day", "LONG")
    assert forming["status"] == "WATCHING" and forming["trigger_time"] is None


def test_consolidation_invalid_plans_and_bad_data_never_trigger():
    bars, t = BASE + [BREAKOUT], when(BREAK_DAY)
    s = pick(run(bars, t, planning=PlanningConfig(min_rr=50)), CB, "1Day", "LONG")
    assert s["status"] == "NO_TRADE"
    assert s["target1"] == pytest.approx(106.9)  # target is not moved to satisfy the minimum
    tight = CFG.with_overrides({"gates": {"max_extension_atr": 0.01}})
    assert pick(run(bars, t, tight), CB, "1Day", "LONG")["status"] != "TRIGGERED"  # chasing
    for problems in (STALE, MISSING):
        s = pick(run(bars, t, problems=problems), CB, "1Day", "LONG")
        assert s["status"] == "NO_TRADE" and s["reasons"] == problems
        assert s["entry"] is None and s["stop"] is None


def test_consolidation_ignores_future_candles():
    bars, t = BASE + [BREAKOUT], when(BREAK_DAY)
    assert run(bars + wild_future(bars), t) == run(bars, t)


# -------------------------------------------------------------- support/resistance reversal

SR_DAY, SR_REV_DAY = date(2006, 3, 20), date(2006, 3, 17)


def test_sr_reversal_watching_on_the_reversal_candle_then_triggered_on_confirmation():
    w = pick(run(GOOG, at(2006, 3, 17)), SR, "1Day", "LONG")
    assert w["status"] == "WATCHING" and w["trigger_time"] is None
    assert "waiting for a confirming close beyond 341.78" in w["reasons"][0]

    s = pick(run(GOOG, at(2006, 3, 20)), SR, "1Day", "LONG")
    assert s["status"] == "TRIGGERED" and s["trigger_time"] == ts(SR_DAY)
    rev, conf = GOOG[idx(GOOG, SR_REV_DAY)], GOOG[idx(GOOG, SR_DAY)]
    assert s["entry_trigger"] == pytest.approx(rev.high)  # 341.78
    assert s["entry"] == pytest.approx(conf.close)  # 348.19
    assert s["plan"]["stop"]["anchor"] == pytest.approx(rev.low)  # wick low 334.93
    check_plan_math(s)
    assert s["risk_reward"] == pytest.approx(2.68, abs=0.01)


def test_sr_reversal_requires_a_close_strictly_beyond_the_reversal_high():
    i = idx(GOOG, SR_DAY)
    rev, conf = GOOG[idx(GOOG, SR_REV_DAY)], GOOG[i]
    for close in (rev.high - 0.5, rev.high):  # below, and exactly at, the trigger
        bars = GOOG[:i] + [replace(conf, close=close)] + GOOG[i + 1:]
        s = pick(run(bars, at(2006, 3, 20)), SR, "1Day", "LONG")
        assert s["status"] != "TRIGGERED", close
    forming = pick(run(GOOG, at(2006, 3, 20, 15)), SR, "1Day", "LONG")
    assert forming["status"] == "WATCHING" and forming["trigger_time"] is None


def test_sr_reversal_invalid_plans_and_bad_data_never_trigger():
    t = at(2006, 3, 20)
    s = pick(run(GOOG, t, planning=PlanningConfig(min_rr=50)), SR, "1Day", "LONG")
    assert s["status"] == "NO_TRADE" and s["risk_reward"] == pytest.approx(2.68, abs=0.01)
    tight = CFG.with_overrides({"gates": {"max_extension_atr": 0.01}})
    assert pick(run(GOOG, t, tight), SR, "1Day", "LONG")["status"] != "TRIGGERED"
    for problems in (STALE, MISSING):
        s = pick(run(GOOG, t, problems=problems), SR, "1Day", "LONG")
        assert s["status"] == "NO_TRADE" and s["reasons"] == problems and s["entry"] is None


def test_sr_reversal_ignores_future_candles():
    past = [b for b in GOOG if day_of(b) <= SR_DAY]
    assert run(past + wild_future(past), at(2006, 3, 20)) == run(past, at(2006, 3, 20))
    assert run(GOOG, at(2006, 3, 20)) == run(past, at(2006, 3, 20))


# --------------------------------------------------------------------- trend continuation

TC_DAY = date(2016, 7, 13)


def test_trend_continuation_triggers_short_with_independent_math():
    before = pick(run(AAPL, at(2016, 7, 12)), TC, "1Day", "SHORT")
    assert before["status"] == "NO_TRADE"  # this strategy has no WATCHING state
    s = pick(run(AAPL, at(2016, 7, 13)), TC, "1Day", "SHORT")
    assert s["status"] == "TRIGGERED" and s["trigger_time"] == ts(TC_DAY)
    i = idx(AAPL, TC_DAY)
    assert s["entry_trigger"] == pytest.approx(AAPL[i - 1].low)  # close below the prior low
    assert s["entry"] == pytest.approx(AAPL[i].close)
    assert s["plan"]["stop"]["anchor"] == pytest.approx(97.70, abs=0.01)  # pullback high
    check_plan_math(s)
    assert s["risk_reward"] == pytest.approx(2.78, abs=0.01)


def uptrend(pullback: int) -> list[Bar]:
    """12 higher-high/higher-low legs, a pullback, then a bullish close above the prior high."""
    pts, k, p = [(0, 60)], 0, 60
    for _ in range(12):
        k += 14
        p += 9
        pts.append((k, p))
        k += 6
        p -= 4
        pts.append((k, p))
    pts.append((k + 5 + pullback, p - 3 - pullback))
    bars = path(pts, wick=0.4)
    d, last = next_day(bars), bars[-1]
    return bars + [Bar(midnight(d), last.close, last.high + 1.5, last.close - 0.2,
                       last.high + 1.2, 4_000_000)]


def test_trend_continuation_long_reward_risk_boundary():
    """The same pattern just under (1.76) and just over (2.02) the 2:1 minimum."""
    under = pick(run(uptrend(0), at(2030, 1, 1)), TC, "1Day", "LONG")
    assert under["status"] == "NO_TRADE"
    assert under["risk_reward"] == pytest.approx(1.76, abs=0.01)
    assert under["risk_reward"] < PLAN.min_rr
    over = pick(run(uptrend(1), at(2030, 1, 1)), TC, "1Day", "LONG")
    assert over["status"] == "TRIGGERED"
    assert over["risk_reward"] == pytest.approx(2.02, abs=0.01)
    assert over["risk_reward"] >= PLAN.min_rr
    assert over["stop"] < over["entry_trigger"] < over["entry"] < over["target1"]
    check_plan_math(over)


def test_trend_continuation_no_trade_and_boundaries():
    i = idx(AAPL, TC_DAY)
    c, prior = AAPL[i], AAPL[i - 1]
    for close in (prior.low + 0.3, prior.low):  # above, and exactly at, the prior low
        bars = AAPL[:i] + [replace(c, close=close, low=min(c.low, prior.low - 0.1))] + AAPL[i + 1:]
        assert pick(run(bars, at(2016, 7, 13)), TC, "1Day", "SHORT")["status"] == "NO_TRADE"
    assert pick(run(AAPL, at(2016, 7, 13, 15)), TC, "1Day", "SHORT")["status"] == "NO_TRADE"
    flat = path([(0, 100)] + [(20 * j, 104 if j % 2 else 100) for j in range(1, 13)], wick=0.4)
    assert len(flat) >= 220  # enough history that only the trend rule decides
    for d in ("LONG", "SHORT"):
        s = pick(run(flat, at(2030, 1, 1)), TC, "1Day", d)
        assert s["status"] == "NO_TRADE" and "No established trend" in s["reasons"][0]
    # Missing history: EMA200 needs 220 daily candles.
    long = path([(0, 60), (260, 140)], wick=0.4)
    s219 = pick(run(long[:219], at(2030, 1, 1)), TC, "1Day", "LONG")
    assert s219["status"] == "NO_TRADE" and "Only 219 daily candles; 220 needed" in s219["reasons"][0]
    s220 = pick(run(long[:220], at(2030, 1, 1)), TC, "1Day", "LONG")
    assert "220 needed" not in s220["reasons"][0]


def test_trend_continuation_invalid_plans_and_bad_data_never_trigger():
    t = at(2016, 7, 13)
    s = pick(run(AAPL, t, planning=PlanningConfig(min_rr=50)), TC, "1Day", "SHORT")
    assert s["status"] == "NO_TRADE" and s["risk_reward"] == pytest.approx(2.78, abs=0.01)
    tight = CFG.with_overrides({"gates": {"max_extension_atr": 0.01}})
    assert pick(run(AAPL, t, tight), TC, "1Day", "SHORT")["status"] != "TRIGGERED"
    for problems in (STALE, MISSING):
        s = pick(run(AAPL, t, problems=problems), TC, "1Day", "SHORT")
        assert s["status"] == "NO_TRADE" and s["reasons"] == problems and s["entry"] is None


def test_trend_continuation_ignores_future_candles():
    past = [b for b in AAPL if day_of(b) <= TC_DAY]
    assert run(past + wild_future(past), at(2016, 7, 13)) == run(past, at(2016, 7, 13))
    up = uptrend(1)
    t = when(day_of(up[-1]))  # evaluated on the trigger day; the wild candles come after it
    assert run(up + wild_future(up), t) == run(up, t)


def downtrend(pullback: int) -> list[Bar]:
    """Mirror of `uptrend`: lower highs/lows, a bounce, then a bearish close below the prior low."""
    pts, k, p = [(0, 200)], 0, 200
    for _ in range(12):
        k += 14
        p -= 9
        pts.append((k, p))
        k += 6
        p += 4
        pts.append((k, p))
    pts.append((k + 5 + pullback, p + 3 + pullback))
    bars = path(pts, wick=0.4)
    d, last = next_day(bars), bars[-1]
    return bars + [Bar(midnight(d), last.close, last.close + 0.2, last.low - 1.5,
                       last.low - 1.2, 4_000_000)]


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_trend_continuation_needs_a_same_colour_close_strictly_beyond_the_prior_extreme(direction):
    """Isolates the confirmation rule on hand-built trends with ample reward/risk (2.86), so
    no other check (reward/risk, targets) decides the outcome."""
    bars = uptrend(4) if direction == "LONG" else downtrend(4)
    base, trig = bars[:-1], bars[-1]
    prev, t = base[-1], when(day_of(trig))
    ok = pick(run(bars, t), TC, "1Day", direction)
    assert ok["status"] == "TRIGGERED" and ok["risk_reward"] == pytest.approx(2.86, abs=0.01)
    check_plan_math(ok)
    if direction == "LONG":
        at_edge = replace(trig, close=prev.high, high=prev.high + 0.3)
        wrong_colour = replace(trig, open=prev.high + 3, high=prev.high + 3.2, close=prev.high + 1.2)
    else:
        at_edge = replace(trig, close=prev.low, low=prev.low - 0.3)
        wrong_colour = replace(trig, open=prev.low - 3, low=prev.low - 3.2, close=prev.low - 1.2)
    for c in (at_edge, wrong_colour):
        s = pick(run(base + [c], t), TC, "1Day", direction)
        assert s["status"] == "NO_TRADE" and s["risk_reward"] is None


def test_trend_continuation_short_reward_risk_boundary():
    under = pick(run(downtrend(0), at(2030, 1, 1)), TC, "1Day", "SHORT")
    over = pick(run(downtrend(1), at(2030, 1, 1)), TC, "1Day", "SHORT")
    assert under["status"] == "NO_TRADE" and under["risk_reward"] == pytest.approx(1.76, abs=0.01)
    assert over["status"] == "TRIGGERED" and over["risk_reward"] == pytest.approx(2.02, abs=0.01)
    assert over["target1"] < over["entry"] < over["entry_trigger"] < over["stop"]
    check_plan_math(over)
