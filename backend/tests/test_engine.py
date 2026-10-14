"""Engine behaviour on hand-built price series."""

import pytest

from app.analysis.engine import ACTIONABLE, FORMING, NO_TRADE, analyze, market_bias
from app.analysis.levels import compute_levels, find_pivots
from app.data.base import Bar
from app.data.demo import DemoDataProvider
from tests.conftest import FIXED_DAY

DAY0 = 1767450600  # 2026-01-05 09:30 ET (Monday)


def make_daily(closes, volumes=None, wick=0.5):
    bars, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        v = volumes[i] if volumes else 1_000_000
        bars.append(Bar(DAY0 + i * 86400, o, max(o, c) + wick, min(o, c) - wick, c, v))
        prev = c
    return bars


def test_uptrend_is_bullish_downtrend_is_bearish():
    up = make_daily([100 + i * 0.5 + (1.5 if i % 6 < 3 else -1.5) for i in range(260)])
    down = make_daily([300 - i * 0.5 + (1.5 if i % 6 < 3 else -1.5) for i in range(260)])
    assert market_bias(up, "swing", 390)["label"] == "Bullish"
    assert market_bias(down, "swing", 390)["label"] == "Bearish"


def test_pivots_and_levels_found_on_range():
    closes = [100 + 5 * ((i // 10) % 2) for i in range(120)]  # square wave 100 <-> 105
    bars = make_daily(closes)
    highs, lows = find_pivots(bars, 3)
    assert highs and lows
    levels = compute_levels(bars, atr_value=1.0, mode="swing", bar_minutes=390)
    prices = [l["price"] if isinstance(l, dict) else l.price for l in levels]
    assert any(abs(p - 105.5) < 1.0 for p in prices)
    assert all(l.kind in ("support", "resistance") for l in levels)


def test_breakout_on_volume_is_actionable():
    # Uptrend into a flat ceiling at ~110, then a high-volume close through it.
    closes = [90 + i * 0.25 for i in range(80)]  # 90 -> 109.75
    closes += [108.5, 109.5, 110.0, 108.8, 109.6, 110.0, 108.9, 109.4, 110.0, 109.0,
               109.5, 110.0, 108.9, 109.6, 110.0, 109.2, 109.7, 110.0, 109.3, 109.8]
    closes += [111.2]
    volumes = [1_000_000] * (len(closes) - 1) + [3_000_000]
    bars = make_daily(closes, volumes, wick=0.3)
    result = analyze("TEST", bars, "swing", "1Day")
    setup = result["setup"]
    assert setup["setup_type"] == "breakout"
    assert setup["direction"] == "long"
    assert setup["triggered"] is True
    assert setup["stop"] < setup["entry"] < setup["targets"][0]
    assert setup["risk_reward"] == pytest.approx(
        (setup["targets"][0] - setup["entry"]) / (setup["entry"] - setup["stop"]), abs=0.02)
    assert setup["status"] in (ACTIONABLE, NO_TRADE)  # NO_TRADE only if R:R is too low
    if setup["status"] == ACTIONABLE:
        assert all(c["passed"] for c in setup["checklist"] if c["required"])


def test_choppy_market_is_no_trade():
    closes = [100 + (0.4 if i % 2 else -0.4) for i in range(150)]
    bars = make_daily(closes, wick=0.2)
    setup = analyze("CHOP", bars, "swing", "1Day")["setup"]
    assert setup["status"] == NO_TRADE
    assert setup["entry"] is None
    assert setup["reasons"]


def test_statuses_are_valid_across_demo_universe():
    p = DemoDataProvider(today=FIXED_DAY)
    seen = set()
    for sym in ["SPY", "QQQ", "NVDA", "TSLA", "AAPL", "AMD", "MSFT", "META", "AMZN"]:
        for mode, tf in [("day", "5Min"), ("day", "1Min"), ("swing", "1Day"), ("swing", "1Hour")]:
            r = analyze(sym, p.get_bars(sym, tf, 300), mode, tf)
            s = r["setup"]
            seen.add(s["status"])
            assert s["status"] in (ACTIONABLE, FORMING, NO_TRADE)
            if s["status"] != NO_TRADE:
                long = s["direction"] == "long"
                assert (s["stop"] < s["entry"]) if long else (s["stop"] > s["entry"])
                assert len(s["targets"]) == 2
                t1 = s["targets"][0]
                assert (t1 > s["entry"]) if long else (t1 < s["entry"])
            if s["status"] == ACTIONABLE:
                assert s["triggered"]
                assert all(c["passed"] for c in s["checklist"] if c["required"])
    assert NO_TRADE in seen


def test_too_few_bars_rejected():
    with pytest.raises(ValueError):
        analyze("X", make_daily([100.0] * 20), "swing", "1Day")
