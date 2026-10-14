"""Scanner rows are actionable only after a TRIGGERED plan is revalidated against the live
price, and the liquidity gate uses consolidated volume when the feed is IEX-only."""

from __future__ import annotations

from datetime import datetime

from app.market.calendar import NY
from app.scanner.engine import live_check, live_quote, setup_row
from app.strategies.config import StrategyConfig
from app.swing.config import SwingConfig
from app.swing.context import build_swing_context
from app.strategies.common import build_context

from .conftest import FIXED_NOW, demo_market
from .test_market_data import OPEN_NOW, make

FRESH = {"freshness": "fresh", "reason": None, "as_of": None, "last_candle": "x"}


def setup(direction="LONG", entry=100.0, stop=95.0, t1=120.0, inv=None, tf="1Day"):
    ref = lambda p: {"price": p}
    return {"id": "x", "ticker": "SPY", "strategy": "swing_breakout",
            "strategy_name": "Daily breakout", "timeframe": tf, "direction": direction,
            "status": "TRIGGERED", "invalidation_price": inv, "reasons": ["triggered"],
            "plan": {"entry": ref(entry), "stop": ref(stop), "target1": ref(t1),
                     "entry_trigger": ref(entry), "target2": None, "risk_reward_t1": 2.4,
                     "min_rr": 2.0, "quality": {"score": 70}}}


def live(price, low=None, high=None):
    return {"price": price, "time": "t", "day_low": low, "day_high": high}


def test_triggered_plan_still_valid_at_live_price_is_actionable():
    s = setup()
    assert live_check(s, "swing", live(101, low=99, high=102)) == []
    row = setup_row(s, "swing", 101, "bullish", FRESH, live(101, low=99, high=102))
    assert row["actionable"] and row["live_checks_failed"] == []
    assert row["live_risk_reward"] == round(19 / 6, 2)


def test_live_price_through_stop_blocks():
    out = live_check(setup(), "swing", live(94.5))
    assert out and "at or beyond the stop" in out[0]
    row = setup_row(setup(), "swing", 94.5, None, FRESH, live(94.5))
    assert not row["actionable"] and row["live_checks_failed"] == out
    assert out[0] in row["requirement_issues"]


def test_swing_stop_hit_intraday_and_recovered_blocks():
    out = live_check(setup(), "swing", live(100.5, low=94.0, high=101))
    assert any("reached intraday today" in x for x in out)


def test_day_mode_ignores_session_range_before_the_trigger():
    # The session low may predate an intraday trigger, so only the live price counts.
    assert live_check(setup(tf="5Min"), "day", live(100.5, low=94.0)) == []


def test_live_price_beyond_invalidation_level_blocks():
    out = live_check(setup(inv=99.0), "swing", live(98.5))
    assert any("invalidation level 99.00" in x for x in out)


def test_t1_already_reached_blocks():
    out = live_check(setup(), "swing", live(120.5))
    assert any("already reached T1" in x for x in out)


def test_chased_price_with_poor_live_reward_risk_blocks():
    # From 108: risk 13, reward 12 -> 0.92:1 < 2:1, although the plan said 4:1.
    out = live_check(setup(), "swing", live(108))
    assert len(out) == 1 and "0.92:1 < 2:1" in out[0]


def test_short_mirror():
    s = setup("SHORT", entry=100, stop=105, t1=80)
    assert live_check(s, "swing", live(99, low=98, high=104)) == []
    assert any("reached intraday" in x for x in live_check(s, "swing", live(99, high=105.2)))
    assert any("at or beyond the stop" in x for x in live_check(s, "swing", live(105)))


def test_no_live_price_is_not_actionable():
    row = setup_row(setup(), "swing", 100.0, None, FRESH, None)
    assert not row["actionable"]
    assert "No fresh live price" in row["live_checks_failed"][0]
    assert live_quote({"available": True, "stale": True, "price": 1.0}) is None
    assert live_quote({"available": False, "price": None}) is None


def test_non_triggered_rows_are_not_revalidated():
    s = setup() | {"status": "WATCHING"}
    row = setup_row(s, "swing", 50.0, None, FRESH, live(50.0))
    assert row["live_checks_failed"] == [] and not row["actionable"]


def test_scanner_rows_carry_live_price_from_quotes(tmp_path):
    from app.config import Settings
    from app.db import Database
    from app.main import Service
    st = Settings(database_path=tmp_path / "t.db", scan_interval_seconds=0)
    svc = Service(st, market=demo_market(), db=Database(st.database_path))
    snap = svc.scanner.run()
    quotes = {q["symbol"]: q for q in svc.market.get_quotes()}
    for r in snap["rows"]:
        assert r["live_price"] == quotes[r["ticker"]]["price"]
        if r["actionable"]:
            assert r["status"] == "TRIGGERED" and r["live_checks_failed"] == []


# ------------------------------------------------------------ consolidated liquidity

def test_iex_feed_measures_liquidity_on_consolidated_sip_history():
    fake, market, _ = make(feed="iex", sip_allowed=False)  # free plan: old SIP only
    liq = market.consolidated_liquidity("SPY")
    assert liq and liq["basis"] == "consolidated volume (SIP)" and liq["sessions"] == 20
    sip = [r for r in fake.requests if r.url.params.get("feed") == "sip"]
    assert sip, "liquidity must be read from the SIP feed"
    today = datetime(2026, 3, 10, tzinfo=NY)
    for r in sip:  # never inside the free plan's 15-minute SIP embargo
        assert datetime.fromisoformat(r.url.params["end"].replace("Z", "+00:00")) <= today
    assert market.consolidated_liquidity("SPY") == liq  # cached
    assert len([r for r in fake.requests if r.url.params.get("feed") == "sip"]) == len(sip)


def test_consolidated_feeds_and_demo_use_their_own_volume():
    _, market, _ = make(feed="delayed_sip")
    assert market.consolidated_liquidity("SPY") is None
    assert demo_market().consolidated_liquidity("SPY") is None


def test_liquidity_gate_uses_consolidated_value_when_given():
    market = demo_market()
    cal = market.calendar
    frames = {tf: market.get_candles("SPY", tf, "regular", 500).bars
              for tf in ("1Day", "1Week", "1Hour", "4Hour")}
    cfg = SwingConfig()
    low = {"adv": 1e6, "basis": "consolidated volume (SIP)", "sessions": 20}
    ctx = build_swing_context("SPY", frames, FIXED_NOW, cal, cfg, liquidity=low)
    assert any("$1.0M < $20M on consolidated volume (SIP)" in g for g in ctx.gate_reasons)
    high = low | {"adv": 5e8}
    ctx = build_swing_context("SPY", frames, FIXED_NOW, cal, cfg, liquidity=high)
    assert not any("Insufficient liquidity" in g for g in ctx.gate_reasons)
    dframes = frames | {tf: market.get_candles("SPY", tf, "regular", 600).bars
                        for tf in ("5Min", "15Min")}
    ctx = build_context("SPY", dframes, FIXED_NOW, cal, StrategyConfig(), liquidity=low)
    assert any("on consolidated volume (SIP)" in g for g in ctx.gate_reasons)
