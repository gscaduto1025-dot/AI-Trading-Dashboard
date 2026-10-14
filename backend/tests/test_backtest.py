"""Backtesting: execution assumptions, data integrity, signal timing / lookahead, parity
with the live engines, metrics, validation and the API."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from app.backtest import metrics as M
from app.backtest.config import DAY_FRAMES, SWING_FRAMES, BacktestConfig
from app.backtest.data import BundledHistory, MarketHistory, Series, validate_series
from app.backtest.execution import simulate
from app.backtest.replay import load_series, replay_symbol
from app.backtest.runner import report, run_backtest
from app.config import Settings
from app.data.base import Bar
from app.db import Database
from app.main import Service, create_app
from app.market.calendar import NY, MarketCalendar
from app.planning.config import PlanningConfig
from app.strategies.config import StrategyConfig
from app.swing.config import SwingConfig

from .conftest import demo_market
from .test_swing import AAPL, run as swing_run

CAL = MarketCalendar()
ENGINE = {"strategy": StrategyConfig(), "swing": SwingConfig(), "planning": PlanningConfig()}
NOCOST = BacktestConfig().with_overrides({"half_spread_bps": {"day": 0, "swing": 0},
                                          "slippage_bps": {"day": 0, "swing": 0},
                                          "commission_per_share": 0})


def ts(y, m, d, hh=0, mm=0) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=NY).timestamp())


def five(start_hh_mm=(10, 0), rows=(), day=(2026, 3, 10)) -> list[Bar]:
    """5-minute bars from rows of (open, high, low, close)."""
    h, m = start_hh_mm
    t0 = ts(*day, h, m)
    return [Bar(t0 + 300 * i, o, hi, lo, c, 1000) for i, (o, hi, lo, c) in enumerate(rows)]


def sig(direction="LONG", stop=99.0, t1=102.0, t2=104.0, zone=(99.5, 100.5),
        confirmed=None, mode_day=True):
    confirmed = confirmed or ts(2026, 3, 10, 10, 0)
    return {"id": "s1", "ticker": "TEST", "strategy": "breakout_retest",
            "strategy_name": "Breakout and retest", "timeframe": "5Min",
            "direction": direction, "trigger_time": confirmed - 300, "confirmed_at": confirmed,
            "seen_at": confirmed + 5, "entry_trigger": 100.0, "plan_entry": 100.0,
            "stop": stop, "target1": t1, "target2": t2,
            "entry_zone": list(zone) if zone else None, "planned_rr": 2.0, "quality": 60.0}


# ===================================================================== execution

def test_entry_at_next_open_with_spread_slippage_and_commission():
    cfg = BacktestConfig()  # day: 2 bps half spread + 3 bps slippage, $0.005/share/side
    bars = five(rows=[(100, 100.4, 99.8, 100.2), (100.2, 102.2, 100.1, 102.1)])
    tr, skip = simulate(sig(), bars, cfg, "day")
    assert skip is None and tr["open"] == 100
    assert tr["entry"] == pytest.approx(100 * (1 + 5e-4))
    assert tr["exit"] == 102.0 and tr["exit_reason"] == "target1"  # limit: no slippage
    pnl = 102.0 - tr["entry"] - 0.01
    assert tr["pnl_per_share"] == pytest.approx(pnl, abs=1e-4)
    assert tr["r"] == pytest.approx(pnl / (tr["entry"] - 99.0), abs=1e-4)
    assert tr["r"] < tr["gross_r"] and tr["cost_per_share"] > 0


def test_zero_costs_make_net_equal_gross():
    bars = five(rows=[(100, 100.4, 99.8, 100.2), (100.2, 102.2, 100.1, 102.1)])
    tr, _ = simulate(sig(), bars, NOCOST, "day")
    assert tr["r"] == tr["gross_r"] == pytest.approx(2.0)


def test_stop_and_target_in_the_same_candle_counts_the_stop():
    bars = five(rows=[(100, 100.3, 99.9, 100.1), (100.1, 102.5, 98.5, 101.0)])
    tr, _ = simulate(sig(), bars, NOCOST, "day")
    assert tr["exit_reason"] == "stop (same candle as target)" and tr["r"] == pytest.approx(-1)


def test_stop_and_target_on_the_entry_candle_counts_the_stop():
    bars = five(rows=[(100, 102.5, 98.9, 101.0)])
    tr, _ = simulate(sig(), bars, NOCOST, "day")
    assert tr["exit_reason"].startswith("stop") and tr["r"] == pytest.approx(-1)


def test_target_must_trade_through_by_one_tick():
    touch = five(rows=[(100, 102.0, 99.9, 101.9), (101.9, 101.95, 101.5, 101.6)])
    tr, _ = simulate(sig(), touch, NOCOST, "day")
    assert tr["exit_reason"] == "data end"  # only touched 102.00: a limit may not have filled
    through = five(rows=[(100, 102.01, 99.9, 101.9)])
    assert simulate(sig(), through, NOCOST, "day")[0]["exit_reason"] == "target1"


def test_gap_through_stop_fills_at_the_worse_open():
    bars = five(rows=[(100, 100.4, 99.6, 100.1), (98.0, 98.5, 97.5, 98.2)])
    tr, _ = simulate(sig(), bars, NOCOST, "day")
    assert tr["exit_reason"] == "stop (gap)" and tr["exit"] == 98.0
    assert tr["r"] == pytest.approx(-2.0)  # worse than -1R: a stop is not a guarantee


def test_gap_through_target_fills_at_target_not_the_better_open():
    bars = five(rows=[(100, 100.4, 99.6, 100.1), (103.5, 104.0, 103.2, 103.8)])
    tr, _ = simulate(sig(), bars, NOCOST, "day")
    assert tr["exit"] == 102.0 and tr["exit_reason"] == "target1"


@pytest.mark.parametrize("open_, reason", [
    (98.9, "opened through the stop"), (102.0, "opened at/through T1 (reward gone)"),
    (100.9, "opened outside the plan's entry zone (no chasing)")])
def test_no_entry_when_the_open_breaks_the_plan(open_, reason):
    bars = five(rows=[(open_, open_ + 0.2, open_ - 0.2, open_)])
    tr, skip = simulate(sig(), bars, NOCOST, "day")
    assert tr is None and skip["reason"] == reason


def test_entry_zone_rule_can_be_disabled():
    cfg = replace(NOCOST, respect_entry_zone=False)
    bars = five(rows=[(100.9, 101.0, 100.8, 100.9)])
    assert simulate(sig(), bars, cfg, "day")[0]["open"] == 100.9


def test_short_is_symmetric():
    s = sig("SHORT", stop=101.0, t1=98.0, t2=96.0, zone=(99.5, 100.5))
    bars = five(rows=[(100, 100.2, 99.7, 99.9), (99.9, 100.0, 97.9, 98.1)])
    tr, _ = simulate(s, bars, NOCOST, "day")
    assert tr["exit_reason"] == "target1" and tr["r"] == pytest.approx(2.0)


def test_day_trades_are_flat_at_the_session_close_and_never_enter_overnight():
    rows = [(100, 100.4, 99.8, 100.2)] * 3
    today = five((15, 45), rows)
    tomorrow = five((9, 30), [(100.2, 103, 100, 102.5)], day=(2026, 3, 11))
    s = sig(confirmed=ts(2026, 3, 10, 15, 45))
    tr, _ = simulate(s, today + tomorrow, NOCOST, "day")
    assert tr["exit_reason"] == "session close" and tr["exit"] == 100.2
    assert tr["exit_time"] == today[-1].time  # never carried into the next session
    late = sig(confirmed=ts(2026, 3, 10, 16, 0))  # the 15:55-16:00 candle confirmed it
    tr, skip = simulate(late, tomorrow, NOCOST, "day")
    assert tr is None and "session close" in skip["reason"]


def test_swing_time_exit_and_data_end():
    cfg = NOCOST.with_overrides({"max_hold_bars": {"swing": 3}})
    days = [Bar(ts(2026, 3, d), 100, 100.5, 99.5, 100.1, 1e6) for d in (2, 3, 4, 5, 6)]
    s = sig(confirmed=ts(2026, 2, 27, 16))
    tr, _ = simulate(s, days, cfg, "swing")
    assert tr["exit_reason"] == "time exit" and tr["bars_held"] == 3
    tr, _ = simulate(s, days[:2], NOCOST, "swing")
    assert tr["exit_reason"] == "data end"


def test_split_exit_policy():
    cfg = NOCOST.with_overrides({"exit_policy": "split"})
    bars = five(rows=[(100, 100.3, 99.9, 100.1), (100.1, 102.3, 100.0, 102.2),
                      (102.2, 102.4, 98.5, 98.9)])
    tr, _ = simulate(sig(), bars, cfg, "day")
    assert [leg["reason"] for leg in tr["legs"]] == ["target1", "stop"]
    assert tr["r"] == pytest.approx(0.5 * 2 + 0.5 * -1)


def test_entry_delay_bars():
    cfg = NOCOST.with_overrides({"entry_delay_bars": 1})
    bars = five(rows=[(100, 100.3, 99.9, 100.1), (100.2, 100.4, 100.0, 100.3)])
    assert simulate(sig(), bars, cfg, "day")[0]["open"] == 100.2


def test_execution_never_sees_candles_before_the_signal():
    bars = five((9, 55), [(100, 100.3, 99.9, 100.1), (100, 100.3, 99.9, 100.1)])
    with pytest.raises(ValueError, match="at or after"):
        simulate(sig(), bars, NOCOST, "day")


# ===================================================================== data integrity

def test_validate_series_sorts_dedupes_drops_bad_candles_and_counts_gaps():
    good = [Bar(ts(2026, 3, d), 10, 11, 9, 10.5, 100) for d in (2, 3, 5, 6)]  # 4th missing
    bad = Bar(ts(2026, 3, 9), 10, 9, 11, 10, 100)  # high < low
    dup = good[1]
    bars, rep = validate_series("T", "1Day", [good[2], good[0], dup, good[1], good[3], bad], CAL)
    assert [b.time for b in bars] == [b.time for b in good]
    assert (rep.duplicates, rep.dropped_invalid, rep.out_of_order) == (1, 1, 1)
    assert rep.missing_sessions == 1 and "2026-03-04" in " ".join(rep.issues)


def test_bundled_history_is_real_and_complete():
    bars, rep = validate_series("AAPL", "1Day", BundledHistory().bars(
        "AAPL", "1Day", datetime(2015, 1, 1, tzinfo=NY), datetime(2018, 1, 1, tzinfo=NY)), CAL)
    assert rep.bars == 506 and rep.dropped_invalid == 0 and rep.missing_sessions == 0
    assert rep.first.startswith("2015-02-17") and rep.last.startswith("2017-02-16")


def test_lookback_windows_match_the_live_service():
    assert DAY_FRAMES == Service.STRATEGY_FRAMES
    assert SWING_FRAMES == Service.SWING_FRAMES


# ===================================================================== signal timing

def test_completed_only_slicing_hides_forming_candles():
    daily = Series("1Day", [Bar(ts(2026, 3, d), 1, 2, 0.5, 1.5, 1) for d in (9, 10)], CAL)
    assert len(daily.completed(ts(2026, 3, 10, 12))) == 1  # today's daily bar is forming
    assert len(daily.completed(ts(2026, 3, 10, 16))) == 2  # final at the close
    hourly = Series("1Hour", [Bar(ts(2026, 3, 10, 10, 30), 1, 2, 0.5, 1.5, 1)], CAL)
    assert hourly.completed(ts(2026, 3, 10, 11, 0)) == []
    assert len(hourly.completed(ts(2026, 3, 10, 11, 30))) == 1


@pytest.fixture(scope="module")
def aapl_run():
    return run_backtest(BundledHistory(), mode="swing", symbols=["AAPL"],
                        start=date(2015, 9, 1), end=date(2017, 12, 29), cal=CAL,
                        engine_cfg=ENGINE)


def test_every_trade_starts_after_its_signal(aapl_run):
    assert aapl_run["trades"], "real AAPL history produces swing trades"
    for t in aapl_run["trades"]:
        assert t["entry_time"] >= t["signal_time"]
        assert t["exit_time"] >= t["entry_time"]
        sd = datetime.fromtimestamp(t["signal_time"], NY)
        assert (sd.hour, sd.minute) == (16, 0)  # daily triggers confirm at the close
        assert datetime.fromtimestamp(t["entry_time"], NY).date() > sd.date()  # next session


def test_signals_equal_the_live_engine_at_that_moment(aapl_run):
    """Independent reference: the swing tests' own helper evaluates the live engine on the
    candles up to the signal day; it must report the same TRIGGERED plan."""
    for t in aapl_run["trades"]:
        d = datetime.fromtimestamp(t["signal_time"], NY).date()
        bars = [b for b in AAPL if datetime.fromtimestamp(b.time, NY).date() <= d]
        live = {s["id"]: s for s in swing_run(bars[-SWING_FRAMES["1Day"]:],
                                              datetime(d.year, d.month, d.day, 16, 0, 5,
                                                       tzinfo=NY), ticker="AAPL")}
        s = live[t["id"]]
        assert s["status"] == "TRIGGERED"
        assert (s["stop"], s["target1"]) == (t["stop"], t["target1"])
        assert s["plan"]["quality"]["score"] == t["quality"]


def test_future_candles_cannot_change_past_signals():
    """Replace everything after a cut-off with wild prices: signals before it are identical."""
    cut = date(2016, 9, 1)

    class Mutated(BundledHistory):
        def bars(self, symbol, tf, start, end):
            out = super().bars(symbol, tf, start, end)
            lim = datetime(cut.year, cut.month, cut.day, tzinfo=NY).timestamp()
            return [b if b.time < lim else replace(b, open=b.open * 2, high=b.high * 3,
                                                  low=b.low * 0.3, close=b.close * 0.5)
                    for b in out]

    def signals(provider):
        series, _ = load_series(provider, "AAPL", "swing", date(2015, 9, 1), date(2017, 6, 30),
                                CAL)
        sigs, _, _ = replay_symbol("AAPL", "swing", date(2015, 9, 1), date(2017, 6, 30),
                                   series, CAL, ENGINE)
        return [s for s in sigs if datetime.fromtimestamp(s["confirmed_at"], NY).date() < cut]

    base = signals(BundledHistory())
    assert base and signals(Mutated()) == base


def test_fast_replay_equals_exhaustive_candle_by_candle_replay_swing():
    series, _ = load_series(BundledHistory(), "AAPL", "swing", date(2016, 1, 4),
                            date(2016, 9, 30), CAL)
    fast, _, ev_fast = replay_symbol("AAPL", "swing", date(2016, 1, 4), date(2016, 9, 30),
                                     series, CAL, ENGINE)
    full, _, ev_full = replay_symbol("AAPL", "swing", date(2016, 1, 4), date(2016, 9, 30),
                                     series, CAL, ENGINE, exhaustive=True)
    assert fast and fast == full and ev_fast < ev_full


def test_fast_replay_equals_exhaustive_candle_by_candle_replay_day():
    m = demo_market()
    series, _ = load_series(MarketHistory(m), "NVDA", "day", date(2026, 3, 2),
                            date(2026, 3, 3), m.calendar)
    fast, _, _ = replay_symbol("NVDA", "day", date(2026, 3, 2), date(2026, 3, 3), series,
                               m.calendar, ENGINE)
    full, _, ev = replay_symbol("NVDA", "day", date(2026, 3, 2), date(2026, 3, 3), series,
                                m.calendar, ENGINE, exhaustive=True)
    assert ev == 2 * 78 and fast == full


# ===================================================================== metrics

def _t(r, q=60.0, t=0, **kw):
    return {"id": f"t{t}", "r": r, "return_pct": r, "exit_time": t, "signal_time": t,
            "legs": [{"reason": "target1" if r > 0 else "stop"}], "bars_held": 1,
            "planned_rr": 2.0, "quality": q, "gross_r": r + 0.05, "ticker": "X",
            "strategy": "s", "timeframe": "5Min", "direction": "LONG", **kw}


def test_summary_metrics_by_hand():
    trades = [_t(2, t=1), _t(-1, t=2), _t(-1, t=3), _t(3, t=4), _t(-1, t=5)]
    s = M.summarize(trades)
    assert (s["trades"], s["wins"], s["win_rate"]) == (5, 2, 40.0)
    assert s["avg_win_r"] == 2.5 and s["avg_loss_r"] == -1.0
    assert s["profit_factor"] == pytest.approx(5 / 3, abs=1e-3)
    assert s["expectancy_r"] == pytest.approx(0.4)
    assert s["max_drawdown_r"] == 2.0  # +2 then -1, -1
    assert s["avg_planned_rr"] == 2.0 and s["avg_cost_r"] == pytest.approx(0.05)
    assert M.max_drawdown([0, 3, 1, 4, -2]) == 6


def test_quality_correlation():
    up = [_t(q / 10 - 6, q=q, t=i) for i, q in enumerate(range(40, 100, 2))]
    qa = M.quality_analysis(up)
    assert qa["spearman"] == 1.0 and "positively" in qa["verdict"]
    assert "Too few" in M.quality_analysis(up[:10])["verdict"]


def test_verdict_never_claims_profit_without_evidence():
    small = M.summarize([_t(1, t=i) for i in range(20)])
    assert M.verdict(small, small, {"oos": small}, True, False, 100, 30)["label"] == \
        "NOT EVIDENCE"
    v = M.verdict(small, small, {"oos": small}, False, False, 100, 30)
    assert v["label"] == "INSUFFICIENT EVIDENCE" and not v["positive_evidence"]
    big = [_t(1.5 if i % 2 else -1, t=i) for i in range(400)]
    s = M.summarize(big)
    assert M.verdict(s, s, {"oos": s}, False, False, 100, 30)["positive_evidence"]
    biased = M.verdict(s, s, {"oos": s}, False, True, 100, 30)
    assert not biased["positive_evidence"] and "survivorship" in " ".join(biased["reasons"])
    losing = M.summarize([_t(1 if i % 3 == 0 else -1, t=i) for i in range(400)])
    assert M.verdict(losing, losing, {"oos": losing}, False, False, 100, 30)["label"] == \
        "NO EDGE FOUND"


def test_walk_forward_chooses_the_filter_out_of_sample_only():
    # Train period: high-quality trades win, low-quality lose -> filter 70 chosen.
    train = [_t(2 if i % 2 else -1, q=80, t=i) for i in range(40)] + \
        [_t(-1, q=40, t=i) for i in range(40)] + [_t(-1, q=60, t=i) for i in range(20)]
    test = [_t(-1, q=80, t=200 + i) for i in range(20)] + [_t(3, q=40, t=200 + i)
                                                          for i in range(20)]
    wf = M.walk_forward(train + test, 0, 240, 2, [0, 50, 70], 1.0)
    assert wf["folds"][0]["min_quality"] == 70
    # changing the TEST outcomes must not change the filter chosen for that fold
    flipped = [dict(t, r=-t["r"]) for t in test]
    assert M.walk_forward(train + flipped, 0, 240, 2, [0, 50, 70], 1.0)["folds"][0][
        "min_quality"] == 70


def test_in_and_out_of_sample_split_by_signal_time():
    trades = [_t(1, t=i) for i in range(10)]
    ins, outs, cut = M.split_in_out(trades, 0, 10, 0.7)
    assert cut == 7 and len(ins) == 7 and len(outs) == 3


def test_report_filters_and_groupings(aapl_run):
    rep = report(aapl_run)
    n = rep["overall"]["trades"]
    assert sum(g["trades"] for g in rep["by_strategy"].values()) == n
    assert sum(g["trades"] for g in rep["by_ticker"].values()) == n
    assert sum(g["trades"] for g in rep["by_timeframe"].values()) == n
    st = aapl_run["trades"][0]["strategy"]
    only = report(aapl_run, strategy=st)
    assert all(t["strategy"] == st for t in only["trades"])
    assert report(aapl_run, start="2030-01-01")["overall"]["trades"] == 0
    assert rep["in_sample"]["until"] == only["in_sample"]["until"]  # filters keep the split
    assert rep["verdict"]["label"] in ("INSUFFICIENT EVIDENCE", "NOT DEMONSTRATED",
                                       "NO EDGE FOUND")
    assert aapl_run["survivorship_biased"] and aapl_run["order_execution"] is False


def test_point_in_time_universe_excludes_signals_outside_membership():
    run = run_backtest(BundledHistory(), mode="swing", symbols=[], start=date(2015, 9, 1),
                       end=date(2017, 12, 29), cal=CAL, engine_cfg=ENGINE,
                       universe=[{"symbol": "AAPL", "start": "2017-01-01"}])
    assert not run["survivorship_biased"]
    assert all(datetime.fromtimestamp(t["signal_time"], NY).date() >= date(2017, 1, 1)
               for t in run["trades"])
    assert any(s["reason"].startswith("not in the point-in-time universe")
               for s in run["skipped"])


def test_missing_history_is_reported_not_hidden():
    run = run_backtest(BundledHistory(), mode="swing", symbols=["AAPL", "MSFT"],
                       start=date(2016, 1, 4), end=date(2016, 6, 30), cal=CAL,
                       engine_cfg=ENGINE)
    assert [e["symbol"] for e in run["data_errors"]] == ["MSFT"]


# ===================================================================== API

def test_backtest_api(tmp_path):
    st = Settings(database_path=tmp_path / "b.db", scan_interval_seconds=0)
    svc = Service(st, market=demo_market(), db=Database(st.database_path))
    with TestClient(create_app(svc, run_scanner=False)) as c:
        cfg = c.get("/api/backtests/config").json()
        assert cfg["order_execution"] is False and cfg["limitations"]
        assert c.post("/api/backtests", json={"mode": "day", "start": "2026-03-02",
                                              "end": "2026-03-04", "data_source": "bundled"}
                      ).status_code == 400
        assert c.post("/api/backtests", json={"mode": "swing", "start": "x",
                                              "end": "2016-01-01"}).status_code == 400
        assert c.post("/api/backtests", json={"mode": "swing", "start": "2016-01-01",
                                              "end": "2016-06-01", "settings": {"nope": 1}}
                      ).status_code == 400
        r = c.post("/api/backtests", json={"mode": "swing", "data_source": "bundled",
                                           "symbols": ["AAPL"], "start": "2016-01-04",
                                           "end": "2016-12-30"})
        assert r.status_code == 200
        run_id = r.json()["id"]
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            j = c.get(f"/api/backtests/{run_id}").json()
            if j["status"] != "running":
                break
            time.sleep(0.3)
        assert j["status"] == "done", j.get("error")
        assert j["report"]["verdict"]["label"] != "POSITIVE EVIDENCE (hypothetical)"
        assert j["run"]["data_source"] == "bundled" and "trades" not in j["run"]
        runs = c.get("/api/backtests").json()["runs"]
        assert runs[0]["id"] == run_id and runs[0]["summary"]["trades"] == \
            j["report"]["overall"]["trades"]
        csv_ = c.get(f"/api/backtests/{run_id}/trades.csv")
        assert csv_.status_code == 200 and csv_.text.startswith("id,ticker,strategy")
        assert len(csv_.text.strip().splitlines()) == j["report"]["overall"]["trades"] + 1
        assert c.get("/api/backtests/nope").status_code == 404
        assert c.delete(f"/api/backtests/{run_id}").json() == {"ok": True}
        assert not any("order" in r.path for r in c.app.routes)
