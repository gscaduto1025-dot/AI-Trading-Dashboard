"""Backtest <-> live parity: the same point-in-time information must give the same decision.

The live side is the real Service (strategy engine via strategy_setups(as_of), the
scanner's setup_row / entry rules, the market-data service's SIP overlay and liquidity);
the backtest side is replay + eligibility. Data: an IEX-like source whose consolidated
(SIP) volume differs from its IEX volume (tests/iex_like.py), and real GOOG history.
"""

from __future__ import annotations

import pathlib
import tempfile
from dataclasses import replace
from datetime import date, datetime

import pytest

from app.backtest import eligibility, replay
from app.backtest.config import BacktestConfig
from app.backtest.data import MarketHistory, Series
from app.backtest.eligibility import find_entry
from app.backtest.manager import engine_config
from app.backtest.replay import _evaluate, load_series, load_sip, pit_inputs, replay_symbol
from app.backtest.runner import run_backtest
from app.config import Settings
from app.db import Database
from app.main import Service
from app.market import service as market_service
from app.market.calendar import NY
from app.market.service import COMPLETION_GRACE_SECONDS, MarketDataService
from app.planning.config import PlanningConfig
from app.scanner import engine as scanner
from app.scanner.engine import CURRENTLY_ACTIONABLE, setup_row
from app.strategies import entry
from app.strategies.config import StrategyConfig
from app.strategies.entry import entry_env_at
from app.strategies.volume import FULL, LIMITED, UNAVAILABLE, UNVERIFIED
from app.swing.config import SwingConfig

from .conftest import FIXED_NOW, WATCHLIST
from .iex_like import IexLikeSource
from .test_swing import GOOG

SYMS = ["SPY", "NVDA", "TSLA"]
START, END = date(2026, 2, 23), date(2026, 3, 9)
GRACE = COMPLETION_GRACE_SECONDS
FRESH = {"freshness": "fresh", "reason": None, "as_of": None, "last_candle": "x"}


def make_service(policy="unverified", delayed=False) -> Service:
    m = MarketDataService(IexLikeSource(now=FIXED_NOW, delayed=delayed), symbols=WATCHLIST,
                          now_fn=lambda: FIXED_NOW)
    p = pathlib.Path(tempfile.mkdtemp()) / "t.db"
    svc = Service(Settings(database_path=p, scan_interval_seconds=0), market=m,
                  db=Database(p))
    svc.planning_config = replace(svc.planning_config, partial_volume_policy=policy)
    return svc


def backtest(svc, mode="day", syms=SYMS, start=START, end=END):
    prov = MarketHistory(svc.market)
    return run_backtest(prov, mode=mode, symbols=syms, start=start, end=end,
                        cal=svc.market.calendar, engine_cfg=engine_config(svc, prov))


@pytest.fixture(scope="module")
def unverified():
    svc = make_service("unverified")
    return svc, backtest(svc)


@pytest.fixture(scope="module")
def limited():
    svc = make_service("limited")
    return svc, backtest(svc)


def live_setup(svc, sym, mode, at_ts):
    """What the live service reported for a setup at `at_ts` (point-in-time)."""
    when = datetime.fromtimestamp(at_ts, NY)
    call = svc.strategy_setups if mode == "day" else svc.swing_setups
    return {s["id"]: s for s in call([sym], as_of=when, record=False)["setups"]}


def live_row(svc, s, mode, decided, price):
    env = entry_env_at(datetime.fromtimestamp(decided + GRACE, NY), svc.market.calendar,
                       svc.strategy_config.gates, svc.planning_config)
    live = {"price": price, "time": None, "day_low": None, "day_high": None}
    return setup_row(s, mode, price, None, FRESH, live, env)


# ------------------------------------------------------------------ one set of rules

def test_backtest_and_scanner_share_the_same_rule_functions():
    assert eligibility.entry_checks is scanner.entry_checks is entry.entry_checks
    assert replay.sip_covered_until is market_service.sip_covered_until
    assert replay.liquidity_from_daily is market_service.liquidity_from_daily


# ----------------------------------------------- identical point-in-time inputs (day)

MOMENTS = [datetime(2026, 3, 5, h, m, tzinfo=NY) for h, m in
           ((9, 50), (10, 35), (11, 20), (12, 5), (13, 40), (14, 25), (15, 10), (15, 55))]


@pytest.fixture(scope="module")
def day_inputs():
    svc = make_service()
    prov = MarketHistory(svc.market)
    cal = svc.market.calendar
    data = {}
    for sym in ("SPY", "NVDA"):
        series, _ = load_series(prov, sym, "day", date(2026, 3, 2), date(2026, 3, 6), cal)
        sip, notes = load_sip(prov, sym, "day", date(2026, 3, 2), date(2026, 3, 6), cal)
        assert not notes
        data[sym] = (series, sip)
    return svc, engine_config(svc, prov), data


def test_point_in_time_volume_and_liquidity_equal_the_live_service(day_inputs):
    svc, cfg, data = day_inputs
    m = svc.market
    for sym, (series, sip) in data.items():
        for t in MOMENTS:
            ts = int(t.timestamp())
            frames, _ = svc._strategy_frames(sym, t, True)
            live_v = svc.volume_source(sym, frames, ("5Min", "15Min"), t)
            live_liq = m.consolidated_liquidity(sym, as_of=t)
            bt_v, bt_liq = pit_inputs(cfg, sip, "day", ts, ts, m.calendar)
            assert (bt_v.scope, bt_v.policy, bt_v.feed_label) == \
                (live_v.scope, live_v.policy, live_v.feed_label) == ("partial", "unverified", "IEX")
            for tf in ("5Min", "15Min"):
                lo, bo = live_v.overlay(tf), bt_v.overlay(tf)
                # Same coverage, and never newer than 16 minutes before the decision.
                assert lo.covered_until == bo.covered_until <= ts - 16 * 60
                for b in frames[tf]:
                    if b.time in lo.volumes:
                        assert bo.volumes[b.time] == lo.volumes[b.time]
            assert bt_liq["adv"] == pytest.approx(live_liq["adv"])
            for k in ("sessions", "first_session", "last_session", "excludes_session"):
                assert bt_liq[k] == live_liq[k]


def _decision(s):
    plan = s.get("plan") or {}
    num = lambda k: round(plan[k]["price"], 4) if plan.get(k) else None  # noqa: E731
    return (s["status"], s.get("trigger_time"), s.get("volume_confirmation"),
            num("entry"), num("stop"), num("target1"),
            [(c["name"], c["passed"], c.get("verification")) for c in s["confirmation"]])


def test_live_engine_and_replay_give_the_same_decisions_at_each_moment(day_inputs):
    svc, cfg, data = day_inputs
    compared = 0
    for sym, (series, sip) in data.items():
        for t in MOMENTS:
            ts = int(t.timestamp())
            live = {(s["strategy"], s["timeframe"], s["direction"]): s
                    for s in live_setup(svc, sym, "day", ts).values()}
            bt = {(s["strategy"], s["timeframe"], s["direction"]): s
                  for s in _evaluate(sym, series, "day", ts, svc.market.calendar, cfg, sip)}
            assert live.keys() == bt.keys()
            for k in live:
                assert _decision(live[k]) == _decision(bt[k]), (sym, t, k)
                compared += 1
    assert compared >= 2 * len(MOMENTS) * 10


def test_no_sip_volume_from_after_the_embargo_is_used(day_inputs):
    """Wildly change every consolidated volume a user could not yet have had: the
    replayed decision at that moment must not change."""
    svc, cfg, data = day_inputs
    series, sip = data["SPY"]
    for t in MOMENTS:
        ts = int(t.timestamp())
        hidden = {tf: {bt: (v * 50 if bt + 300 > market_service.sip_covered_until(t, tf)
                             else v) for bt, v in vols.items()}
                  for tf, vols in sip.items() if tf in ("5Min", "15Min")}
        mutated = {**sip, **hidden}
        a = [_decision(s) for s in _evaluate("SPY", series, "day", ts, svc.market.calendar,
                                             cfg, sip)]
        b = [_decision(s) for s in _evaluate("SPY", series, "day", ts, svc.market.calendar,
                                             cfg, mutated)]
        assert a == b


# --------------------------------------------- trades == what the live scanner allowed

def test_every_backtest_trade_was_currently_actionable_in_the_live_scanner(unverified):
    svc, run = unverified
    assert len(run["trades"]) >= 20
    for t in run["trades"]:
        decided = t["entry_decided_at"]
        assert t["entry_time"] == decided and t["entry_time"] >= t["signal_time"]
        s = live_setup(svc, t["ticker"], "day", decided + GRACE)[t["id"]]
        row = live_row(svc, s, "day", decided, t["open"])
        assert row["signal_state"] == CURRENTLY_ACTIONABLE, row["live_checks_failed"]
        assert t["volume_confirmation"] == row["volume_confirmation"]
        assert t["volume_confirmation"] not in (UNVERIFIED, UNAVAILABLE)


def test_detected_but_untraded_signals_were_not_actionable_live(unverified):
    svc, run = unverified
    assert run["signals_detected"] >= len(run["trades"]) + 1
    not_actionable = [s for s in run["skipped"] if s.get("decided_at")]
    assert not_actionable
    for sk in not_actionable:
        when = sk["decided_at"]
        setups = live_setup(svc, sk["ticker"], "day", when + GRACE)
        s = setups.get(sk["signal_id"])
        if s is None or s["status"] != "TRIGGERED":
            continue  # gone live too
        bars = [b for b in svc.market.get_history(sk["ticker"], "5Min",
                                                  datetime.fromtimestamp(when - 600, NY),
                                                  datetime.fromtimestamp(when + 600, NY)).bars
                if b.time == when]
        row = live_row(svc, s, "day", when, bars[0].open)
        assert not row["actionable"]


def test_volume_policy_is_preserved_in_backtests(unverified, limited):
    _, ru = unverified
    _, rl = limited
    assert {t["volume_confirmation"] for t in ru["trades"]} <= {FULL, "NOT_REQUIRED"}
    lim = [t for t in rl["trades"] if t["volume_confirmation"] == LIMITED]
    assert lim, "the limited policy admits IEX-only volume as LIMITED"
    u_trades = {t["id"] for t in ru["trades"]}
    u_skips = {s["signal_id"]: s for s in ru["skipped"]}
    for t in lim:
        assert t["id"] not in u_trades
        failed = [c["name"] for c in u_skips[t["id"]].get("entry_checks") or []
                  if not c["passed"]]
        assert "Volume confirmation verified" in failed
    assert ru["volume"] == {"scope": "partial", "feed": "IEX",
                            "partial_volume_policy": "unverified", "feed_delay_seconds": 0}


# ------------------------------------------------------------------- feed delay

def test_delayed_feed_signals_are_acted_on_only_after_the_delay():
    svc = make_service(delayed=True)
    run = backtest(svc, syms=["SPY", "NVDA"])
    assert run["volume"]["feed_delay_seconds"] == 900 and run["volume"]["scope"] == "consolidated"
    for t in run["trades"]:
        assert t["entry_time"] >= t["signal_time"] + 900
        assert t["timeframe"] == "15Min"  # 5-minute triggers are 3 candles old by then
    five = [s for s in run["skipped"] if s["timeframe"] == "5Min"
            and s["reason"].startswith("never currently actionable")]
    assert five and all("Trigger at most 3" in s["reason"] for s in five)
    sigs, _, _ = replay_symbol(
        "SPY", "day", START, END,
        load_series(MarketHistory(svc.market), "SPY", "day", START, END, svc.market.calendar)[0],
        svc.market.calendar, engine_config(svc, MarketHistory(svc.market)))
    assert sigs and all(s["seen_at"] == s["confirmed_at"] + GRACE + 900 for s in sigs)


# ------------------------------------------------- swing on real history, IEX-like volume

def _goog_series(cal):
    iex = [replace(b, volume=b.volume * 0.03) for b in GOOG]
    from app.backtest.data import weekly_from_daily
    return {"1Day": Series("1Day", iex, cal), "1Week": Series("1Week", weekly_from_daily(iex), cal)}


def test_swing_volume_comes_from_consolidated_history_point_in_time():
    from app.market.calendar import MarketCalendar
    from app.backtest.data import weekly_from_daily
    cal = MarketCalendar()
    series = _goog_series(cal)
    sip = {"1Day": {b.time: b.volume for b in GOOG},
           "1Week": {b.time: b.volume for b in weekly_from_daily(GOOG)}, "daily": GOOG}
    cfg = {"strategy": StrategyConfig(), "swing": SwingConfig(), "planning": PlanningConfig(),
           "volume_scope": "partial", "volume_label": "IEX", "feed_delay": 0}
    # 2008-01-31: the real breakout is detected, but GOOG gapped below the stop at the next
    # open (earnings that evening) - the live scanner would refuse it, so no trade.
    sigs, _, _ = replay_symbol("GOOG", "swing", date(2007, 11, 1), date(2008, 3, 31), series,
                               cal, cfg, sip=sip)
    jan = next(s for s in sigs if s["strategy"] == "swing_breakout"
               and datetime.fromtimestamp(s["confirmed_at"], NY).date() == date(2008, 1, 31))
    j, info = find_entry("GOOG", jan, "swing", series, cal, cfg, BacktestConfig(), sip)
    assert j is None and "Stop not reached" in info["reason"]
    # 2010-10-15: detected at the close with UNVERIFIED volume (SIP does not cover the
    # candle yet); by the next open it does: FULL and actionable at the 2010-10-18 open.
    sigs, _, _ = replay_symbol("GOOG", "swing", date(2010, 9, 1), date(2010, 10, 29), series,
                               cal, cfg, sip=sip)
    sig = next(s for s in sigs if s["strategy"] == "swing_breakout")
    assert datetime.fromtimestamp(sig["confirmed_at"], NY).date() == date(2010, 10, 15)
    assert sig["volume_confirmation"] == UNVERIFIED
    j, info = find_entry("GOOG", sig, "swing", series, cal, cfg, BacktestConfig(), sip)
    assert j == 0 and info["setup"]["volume_confirmation"] == FULL
    assert datetime.fromtimestamp(info["decided_at"], NY) == datetime(2010, 10, 18, 9, 30,
                                                                       tzinfo=NY)
    # Without consolidated history the IEX proxy never confirms it (default policy): it
    # stays unverified until the trigger expires - detected, never traded.
    j, info = find_entry("GOOG", sig, "swing", series, cal, cfg, BacktestConfig(), None)
    assert j is None
    assert "Volume confirmation verified" in [c["name"] for c in info["entry_checks"]
                                              if not c["passed"]]
    # With the limited policy the IEX proxy is accepted, labelled LIMITED.
    lim = cfg | {"planning": replace(PlanningConfig(), partial_volume_policy="limited")}
    j, info = find_entry("GOOG", sig, "swing", series, cal, lim, BacktestConfig(), None)
    assert j == 0 and info["setup"]["volume_confirmation"] == LIMITED
