"""Signal-quality rules on top of the engines:

1. TRIGGERED is what happened; `signal_state` says whether it is a fresh entry *now*
   (CURRENTLY_ACTIONABLE) or not (HISTORICALLY_TRIGGERED). A favourable reward/risk at the
   current price is never enough: the strategy's entry conditions are rechecked.
2. Relative volume is FULL only from consolidated volume; on the IEX feed the proxy is
   LIMITED or UNVERIFIED (configurable, conservative default) and SIP volume is overlaid
   wherever the free plan provides it (candles older than 15 minutes).
3. A volume-dependent signal is never labelled fully confirmed without its volume source.
4. The liquidity gate uses consolidated SIP daily volume with its sessions and feed label.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime

import pytest

from app.config import Settings
from app.db import Database
from app.main import Service
from app.market.calendar import NY
from app.planning.config import PlanningConfig
from app.scanner.engine import (CURRENTLY_ACTIONABLE, HISTORICALLY_TRIGGERED, entry_checks,
                                live_quote, setup_row)
from app.strategies.common import TRIGGERED, build_context
from app.strategies.config import StrategyConfig
from app.strategies.volume import (FULL, LIMITED, NOT_REQUIRED, UNAVAILABLE, UNVERIFIED,
                                   VolumeOverlay, VolumeSource, read_volume,
                                   volume_confirmation)
from app.swing.config import SwingConfig
from app.swing.context import build_swing_context
from tests import strategy_fixtures as SF
from tests.test_alerts import row as alert_row, snap as alert_snap
from tests.test_swing import GOOG, at, idx, pick as swing_pick, run as swing_run
from tests.test_strategies import BR_FULL

from .conftest import FIXED_NOW, demo_market
from .test_market_data import make

FRESH = {"freshness": "fresh", "reason": None, "as_of": None, "last_candle": "x"}
NOW = 1_800_000_000
ENV_OPEN = {"now": NOW, "day_window": True, "day_window_detail": "open",
            "max_age": PlanningConfig().max_entry_age_candles}


# ============================================================ 1. current entry checks

def setup(direction="LONG", *, mode="swing", tf="1Day", trigger=98.0, entry=100.0,
          stop=95.0, t1=120.0, inv=None, bars_after=0, confirmed=NOW - 60,
          vc=NOT_REQUIRED):
    ref = lambda p: {"price": p}
    lo, hi = sorted((trigger, entry))
    return {"id": "x", "ticker": "SPY", "strategy": "swing_breakout",
            "strategy_name": "Daily breakout", "timeframe": tf, "direction": direction,
            "status": TRIGGERED, "invalidation_price": inv, "reasons": ["triggered"],
            "confirmed_at": confirmed, "outcome": {"state": "open",
                                                   "bars_after_trigger": bars_after},
            "volume_confirmation": vc,
            "plan": {"entry": ref(entry), "stop": ref(stop), "target1": ref(t1),
                     "entry_trigger": ref(trigger), "target2": None, "risk_reward_t1": 4.0,
                     "entry_zone": {"low": lo, "high": hi}, "min_rr": 2.0,
                     "quality": {"score": 70}}}


def live(price, low=None, high=None):
    return {"price": price, "time": "t", "day_low": low, "day_high": high}


def state(s, price, mode="swing", env=ENV_OPEN, **kw):
    return setup_row(s, mode, price, None, FRESH, live(price, **kw), env)


def failed(r):
    return [c["name"] for c in r["entry_checks"] if not c["passed"]]


def test_inside_the_entry_zone_with_everything_valid_is_currently_actionable():
    r = state(setup(), 99.0, low=97.5, high=99.5)
    assert r["signal_state"] == CURRENTLY_ACTIONABLE and r["actionable"]
    assert failed(r) == [] and r["live_checks_failed"] == []


def test_favourable_reward_risk_alone_is_not_a_fresh_entry():
    # 97.0 is back below the 98.0 trigger: R:R from here is 23/2 = 11.5:1 - better than the
    # plan - but the breakout's entry condition (price beyond the trigger) no longer holds.
    r = state(setup(), 97.0)
    assert r["live_risk_reward"] == 11.5
    assert r["signal_state"] == HISTORICALLY_TRIGGERED and not r["actionable"]
    assert failed(r) == ["Live price inside the entry zone"]
    assert "back through the trigger" in r["live_checks_failed"][0]


def test_chasing_beyond_the_entry_zone_is_historical():
    r = state(setup(t1=140), 103.0)  # R:R 37/8 still > 2:1, but beyond the zone
    assert r["signal_state"] == HISTORICALLY_TRIGGERED
    assert "chasing" in r["live_checks_failed"][0]


def test_old_triggers_are_historical_even_at_a_good_price():
    assert state(setup(bars_after=2), 99.0)["signal_state"] == CURRENTLY_ACTIONABLE
    r = state(setup(bars_after=3), 99.0)
    assert r["signal_state"] == HISTORICALLY_TRIGGERED
    assert failed(r) == ["Trigger at most 3 completed 1Day candles old"]
    weekly = state(setup(tf="1Week", bars_after=1), 99.0)
    assert weekly["signal_state"] == HISTORICALLY_TRIGGERED


def test_intraday_trigger_age_and_entry_window():
    day = lambda **k: setup(mode="day", tf="5Min", **k)
    ok = state(day(confirmed=NOW - 5 * 60), 99.0, mode="day")
    assert ok["signal_state"] == CURRENTLY_ACTIONABLE
    old = state(day(confirmed=NOW - 15 * 60), 99.0, mode="day")  # 3 candles: too old
    assert failed(old) == ["Trigger at most 3 5Min candles old"]
    closed = {**ENV_OPEN, "day_window": False, "day_window_detail": "closed"}
    r = state(day(confirmed=NOW - 60), 99.0, mode="day", env=closed)
    assert r["signal_state"] == HISTORICALLY_TRIGGERED
    assert failed(r) == ["Entry window open"]
    # Swing setups are not bound to the day-trading window.
    assert state(setup(), 99.0, env=closed)["signal_state"] == CURRENTLY_ACTIONABLE


def test_stop_invalidation_and_target_rules():
    assert "Stop not reached" in failed(state(setup(), 99.0, low=94.0))  # intraday low
    assert "Stop not reached" not in failed(
        state(setup(mode="day", tf="5Min"), 99.0, mode="day", low=94.0))  # pre-trigger range
    assert "Not beyond the invalidation level" in failed(state(setup(inv=99.5), 99.0))
    t1_hit = state(setup(entry=100, t1=100.5), 100.6)
    assert "T1 not reached" in failed(t1_hit)
    poor = state(setup(trigger=98, entry=100, stop=97.9, t1=103), 99.9)  # 3.1/2.0 = 1.55:1
    assert failed(poor) == ["Reward/risk from the live price"]


def test_short_mirror():
    s = setup("SHORT", trigger=102, entry=100, stop=105, t1=80)
    assert state(s, 101.0, low=99, high=103)["signal_state"] == CURRENTLY_ACTIONABLE
    assert "back through the trigger" in state(s, 102.5)["live_checks_failed"][0]
    assert "Stop not reached" in failed(state(s, 101.0, high=105.5))


@pytest.mark.parametrize("vc,ok", [(FULL, True), (NOT_REQUIRED, True), (LIMITED, True),
                                   (UNVERIFIED, False), (UNAVAILABLE, False)])
def test_volume_confirmation_gates_actionability(vc, ok):
    r = state(setup(vc=vc), 99.0)
    assert r["actionable"] is ok and r["volume_confirmation"] == vc
    if not ok:
        assert failed(r) == ["Volume confirmation verified"]
        assert r["signal_state"] == HISTORICALLY_TRIGGERED


def test_no_live_price_or_stale_data_is_not_actionable():
    r = setup_row(setup(), "swing", 99.0, None, FRESH, None, ENV_OPEN)
    assert not r["actionable"] and "Fresh live price" in failed(r)
    stale = setup_row(setup(), "swing", 99.0, None, {**FRESH, "freshness": "stale"},
                      live(99.0), ENV_OPEN)
    assert not stale["actionable"] and stale["signal_state"] == HISTORICALLY_TRIGGERED
    assert live_quote({"available": True, "stale": True, "price": 1.0}) is None


def test_non_triggered_rows_keep_their_status_as_state():
    r = setup_row(setup() | {"status": "WATCHING"}, "swing", 50.0, None, FRESH, live(50.0),
                  ENV_OPEN)
    assert r["signal_state"] == "WATCHING" and r["entry_checks"] == [] and not r["actionable"]


# ================================================================= 2. volume readings

def test_read_volume_levels():
    partial = VolumeSource("partial", "IEX", "unverified")
    assert read_volume(2.0, 1.8, True, partial) == read_volume(9, 1.8, True, partial)  # SIP wins
    assert read_volume(2.0, 1.8, True, partial).level == FULL
    v = read_volume(2.0, None, True, partial)
    assert (v.level, v.passed(1.5), v.rejects(1.5)) == (UNVERIFIED, None, False)
    low = read_volume(1.0, None, True, partial)
    assert low.rejects(1.5) and low.passed(1.5) is False  # a proxy may reject
    lim = read_volume(2.0, None, True, replace(partial, policy="limited"))
    assert (lim.level, lim.passed(1.5)) == (LIMITED, True)
    assert read_volume(None, None, False, partial).level == UNAVAILABLE
    assert read_volume(2.0, None, True, VolumeSource("consolidated", "SIP")).level == FULL
    assert "UNVERIFIED" in v.detail("x") and "IEX-only" in v.detail("x")
    assert v.quality_rv() == (None, "UNVERIFIED: IEX-only volume only (scored neutral)")


def test_volume_confirmation_is_the_weakest_required_level():
    c = lambda lvl, req=True: {"verification": lvl, "required": req}
    assert volume_confirmation([]) == NOT_REQUIRED
    assert volume_confirmation([c(FULL), {"name": "other"}]) == FULL
    assert volume_confirmation([c(FULL), c(UNVERIFIED)]) == UNVERIFIED
    assert volume_confirmation([c(UNAVAILABLE, req=False), c(LIMITED)]) == LIMITED


# ------------------------------------------- day engine: breakout & retest (5-minute)

def day_run(volume, rows=BR_FULL, planning=None):
    frames = SF.frames(rows)
    ctx = build_context("TEST", frames, SF.after_bars(len(rows)), SF.CAL, StrategyConfig(),
                        planning=planning or SF.PATTERN_PLANNING, volume=volume)
    from app.strategies.engine import evaluate_context
    return SF.pick(evaluate_context(ctx), "breakout_retest", "LONG"), frames


def overlay_all(frames, factor=40.0, covered=None, override=None):
    """SIP volumes = IEX volume x factor (IEX ~2.5% of the tape), all candles covered."""
    vols = {b.time: b.volume * factor for b in frames["5Min"]}
    vols.update(override or {})
    return {"5Min": VolumeOverlay(vols, covered or 2_000_000_000)}


def vol_cond(s):
    return next(c for c in s.confirmation if "relative volume" in c.name)


def test_day_iex_proxy_is_unverified_by_default_and_limited_on_request():
    partial = VolumeSource("partial", "IEX", "unverified")
    s, _ = day_run(partial)
    assert s.status == TRIGGERED  # the pattern happened...
    c = vol_cond(s)
    assert c.verification == UNVERIFIED and c.passed is None and "IEX-only" in c.detail
    assert s.to_dict()["volume_confirmation"] == UNVERIFIED  # ...but is not fully confirmed
    q = next(x for x in s.plan["quality"]["components"] if x["name"] == "relative_volume")
    assert "UNVERIFIED" in q["detail"]
    s, _ = day_run(replace(partial, policy="limited"))
    assert vol_cond(s).verification == LIMITED and vol_cond(s).passed is True
    assert s.to_dict()["volume_confirmation"] == LIMITED


def test_day_consolidated_overlay_confirms_and_overrides_the_proxy():
    _, frames = day_run(None)
    s, _ = day_run(VolumeSource("partial", "IEX", "unverified", overlay_all(frames)))
    assert s.status == TRIGGERED and vol_cond(s).verification == FULL
    assert vol_cond(s).passed is True and "consolidated volume (SIP)" in vol_cond(s).detail
    # The IEX proxy showed a strong breakout candle, but consolidated volume says it was
    # ordinary: SIP decides, the breakout is rejected.
    brk = SF.bar_time(3)
    weak = overlay_all(frames, override={brk: 1000 * 40.0})
    s, _ = day_run(VolumeSource("partial", "IEX", "unverified", weak))
    assert s.status != TRIGGERED
    assert any("FULL, consolidated volume (SIP)" in r for r in s.reasons)


def test_day_candles_newer_than_the_sip_embargo_stay_proxy():
    _, frames = day_run(None)
    # SIP covers everything up to the breakout candle's end only.
    cov = SF.bar_time(3) + 300
    s, _ = day_run(VolumeSource("partial", "IEX", "unverified",
                                overlay_all(frames, covered=cov)))
    assert vol_cond(s).verification == FULL  # breakout candle (3) is covered
    cov = SF.bar_time(3)  # breakout candle ends after coverage
    s, _ = day_run(VolumeSource("partial", "IEX", "unverified",
                                overlay_all(frames, covered=cov)))
    assert vol_cond(s).verification == UNVERIFIED


def test_day_feed_without_volume_is_unavailable_not_confirmed():
    rows = [r[:4] + (0.0,) for r in BR_FULL]
    for src in (None, VolumeSource("partial", "IEX", "limited")):
        s, _ = day_run(src, rows)
        assert vol_cond(s).verification == UNAVAILABLE
        assert s.to_dict()["volume_confirmation"] == UNAVAILABLE


# --------------------------------------------- swing engine: GOOG 2008-01-31 breakout

def swing_eval(bars, volume, as_of=at(2008, 1, 31)):
    from app.swing.engine import evaluate_swing_context
    from tests.test_swing import frames as swing_frames, CAL as SCAL, PLAN
    ctx = build_swing_context("TEST", swing_frames(bars), as_of, SCAL, SwingConfig(),
                              planning=PLAN, volume=volume)
    return swing_pick(evaluate_swing_context(ctx), "swing_breakout", "1Day", "LONG")


def iex_like(bars, share=0.03):
    return [replace(b, volume=b.volume * share) for b in bars]


def test_swing_breakout_volume_levels():
    i = idx(GOOG, date(2008, 1, 31))
    iex = iex_like(GOOG)
    sip = {b.time: b.volume for b in GOOG}
    cond = lambda s: next(c for c in s["confirmation"] if "Relative volume" in c["name"])
    # Consolidated history covering the trigger candle: FULL, same result as SIP data.
    full = swing_eval(iex, VolumeSource("partial", "IEX", "unverified",
                                        {"1Day": VolumeOverlay(sip, 2_000_000_000)}))
    assert full["status"] == TRIGGERED and full["volume_confirmation"] == FULL
    assert cond(full)["verification"] == FULL and cond(full)["passed"] is True
    # Trigger candle not covered yet (closed < 15 minutes ago): only the IEX proxy.
    end_prev = GOOG[i].time  # coverage ends before the trigger candle closes
    proxy = swing_eval(iex, VolumeSource("partial", "IEX", "unverified",
                                         {"1Day": VolumeOverlay(sip, end_prev)}))
    assert proxy["status"] == TRIGGERED and proxy["volume_confirmation"] == UNVERIFIED
    assert cond(proxy)["passed"] is None
    # Unchanged behaviour on consolidated data (bundled history is consolidated).
    assert swing_pick(swing_run(GOOG, at(2008, 1, 31)), "swing_breakout", "1Day",
                      "LONG")["volume_confirmation"] == FULL


# ============================================================ service, scanner, alerts

def test_volume_overlay_respects_the_free_plan_embargo():
    fake, market, _ = make(feed="iex", sip_allowed=False)
    bars = market.get_candles("SPY", "5Min", "regular", 300).bars
    ov = market.volume_overlay("SPY", "5Min", bars)
    now = market.now().timestamp()
    assert ov is not None and ov.covered_until <= now - 15 * 60
    assert ov.covered_until % 300 == 0 and ov.volumes
    sip = [r for r in fake.requests if r.url.params.get("feed") == "sip"]
    assert sip and all(datetime.fromisoformat(r.url.params["end"].replace("Z", "+00:00"))
                       .timestamp() <= now - 15 * 60 for r in sip)
    n = len(fake.requests)
    assert market.volume_overlay("SPY", "5Min", bars) == ov and len(fake.requests) == n
    _, sip_market, _ = make(feed="delayed_sip")
    assert sip_market.volume_overlay("SPY", "5Min", bars) is None
    assert demo_market().volume_overlay("SPY", "5Min", bars) is None


def test_overlay_failure_falls_back_to_labelled_proxy(tmp_path):
    import httpx
    fake, market, _ = make(feed="iex")
    bars = market.get_candles("SPY", "5Min", "regular", 300).bars
    fake.fail_next = [httpx.Response(403, json={"message": "forbidden"})] * 3
    assert market.volume_overlay("SPY", "5Min", bars) is None
    st = Settings(database_path=tmp_path / "t.db", scan_interval_seconds=0)
    svc = Service(st, market=market, db=Database(st.database_path))
    v = svc.volume_source("SPY", {"5Min": bars}, ("5Min",))
    assert v.scope == "partial" and v.feed_label == "IEX" and v.overlays == {"5Min": None}


def test_liquidity_is_labelled_with_feed_and_sessions():
    _, market, _ = make(feed="iex", sip_allowed=False)
    liq = market.consolidated_liquidity("SPY")
    assert liq["feed"] == "sip" and liq["sessions"] == 20
    assert liq["first_session"] < liq["last_session"] < liq["excludes_session"] == "2026-03-10"
    assert liq["fetched_at"].startswith("2026-03-10T11:02")
    _, sip_market, _ = make(feed="delayed_sip")
    assert sip_market.consolidated_liquidity("SPY") is None
    m = demo_market()
    frames = {tf: m.get_candles("SPY", tf, "regular", 500).bars
              for tf in ("1Day", "1Week", "1Hour", "4Hour")}
    low = liq | {"adv": 1e6}
    ctx = build_swing_context("SPY", frames, FIXED_NOW, m.calendar, SwingConfig(), liquidity=low)
    msg = next(g for g in ctx.gate_reasons if "Insufficient liquidity" in g)
    assert "consolidated volume (SIP), sessions" in msg and liq["last_session"] in msg
    ctx = build_swing_context("SPY", frames, FIXED_NOW, m.calendar, SwingConfig(),
                              liquidity=liq | {"adv": 5e8})
    assert not any("Insufficient liquidity" in g for g in ctx.gate_reasons)


@pytest.fixture(scope="module")
def demo_scan(tmp_path_factory):
    path = tmp_path_factory.mktemp("sq") / "t.db"
    st = Settings(database_path=path, scan_interval_seconds=0)
    svc = Service(st, market=demo_market(), db=Database(path))
    return svc, svc.scanner.run()


def test_scanner_states_are_consistent(demo_scan):
    svc, snap = demo_scan
    assert snap["rows"]
    for r in snap["rows"]:
        assert r["actionable"] == (r["signal_state"] == CURRENTLY_ACTIONABLE)
        if r["status"] == TRIGGERED:
            assert r["signal_state"] in (CURRENTLY_ACTIONABLE, HISTORICALLY_TRIGGERED)
            assert r["entry_checks"]
            assert r["actionable"] == all(c["passed"] for c in r["entry_checks"])
        else:
            assert r["signal_state"] == r["status"]
        assert r["volume_confirmation"] in (FULL, NOT_REQUIRED, UNAVAILABLE)  # demo volume
    # Demo data ends at the 16:00 close (FIXED_NOW is 18:00): no day trigger is a fresh
    # entry after the close.
    day = [r for r in snap["rows"] if r["mode"] == "day" and r["status"] == TRIGGERED]
    assert day and all(r["signal_state"] == HISTORICALLY_TRIGGERED for r in day)
    assert all("Entry window open" in [c["name"] for c in r["entry_checks"]
                                       if not c["passed"]] for r in day)
    ranks = [(r["status"], not r["actionable"]) for r in snap["rows"]]
    assert ranks == sorted(ranks, key=lambda x: ({"TRIGGERED": 0, "WATCHING": 1,
                                                  "NO_TRADE": 2}[x[0]], x[1]))
    for t in snap["tickers"]:
        assert t["volume"]["day"]["scope"] == "demo"


def test_actionable_alerts_match_currently_actionable_rows(demo_scan):
    _, snap = demo_scan
    actionable = {r["id"] for r in snap["rows"] if r["actionable"]}
    alerted = {a["setup_id"] for a in snap["new_alerts"] if a["actionable"]}
    assert alerted == actionable
    for a in snap["new_alerts"]:
        if a["actionable"]:
            assert a["signal_state"] == CURRENTLY_ACTIONABLE


def test_unverified_trigger_alerts_only_once_it_becomes_actionable(tmp_path):
    from app.alerts.engine import AlertEngine
    eng = AlertEngine(Database(tmp_path / "a.db"), clock=lambda: 1_773_000_000.0)
    base = alert_row("TRIGGERED", trig_time=1_773_000_000, outcome="open")
    hist = base | {"actionable": False, "signal_state": HISTORICALLY_TRIGGERED,
                   "volume_confirmation": UNVERIFIED,
                   "entry_checks": [{"name": "Volume confirmation verified", "passed": False,
                                     "detail": "UNVERIFIED"}]}
    assert eng.process(alert_snap([hist])) == []
    reason = eng.last_run["suppressed"][0]["reason"]
    assert "not currently actionable (Volume confirmation verified)" in reason
    # 15 minutes later consolidated volume confirms it while it is still a fresh entry.
    ok = base | {"actionable": True, "signal_state": CURRENTLY_ACTIONABLE,
                 "volume_confirmation": FULL, "entry_checks": []}
    out = eng.process(alert_snap([ok]))
    assert [a["type"] for a in out] == ["setup_triggered"]
    assert out[0]["signal_state"] == CURRENTLY_ACTIONABLE and out[0]["volume_confirmation"] == FULL
    assert eng.process(alert_snap([ok])) == []  # never twice


def test_policy_config_validation():
    with pytest.raises(ValueError):
        PlanningConfig().with_overrides({"partial_volume_policy": "trust_iex"})
    assert PlanningConfig().partial_volume_policy == "unverified"
