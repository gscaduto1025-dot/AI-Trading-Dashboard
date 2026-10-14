"""Opportunity scanner: every row must be exactly what the strategy and planning engines
produced; filters, ranking, stale-data flags, candle-aware scheduling, watchlist
management, and no Claude calls."""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.data.base import ProviderUnavailable
from app.data.demo import DemoSource
from app.db import Database
from app.main import Service, create_app
from app.market.calendar import NY
from app.market.service import MarketDataService
from app.scanner.engine import PATTERN, filter_rows, include_row, rank_key

from .conftest import FIXED_NOW, WATCHLIST, demo_market


@pytest.fixture(scope="module")
def scanned(tmp_path_factory):
    path = tmp_path_factory.mktemp("scan") / "t.db"
    st = Settings(database_path=path, scan_interval_seconds=0)
    svc = Service(st, market=demo_market(), db=Database(path))
    snap = svc.scanner.run()
    return svc, snap


def engine_setups(svc) -> dict[str, tuple[str, dict]]:
    out = {s["id"]: ("day", s) for s in svc.strategy_setups(record=False)["setups"]}
    out.update({s["id"]: ("swing", s) for s in svc.swing_setups(record=False)["setups"]})
    return out


# ------------------------------------------------------------- engine parity

def test_every_row_matches_the_strategy_engine_output(scanned):
    svc, snap = scanned
    engine = engine_setups(svc)
    assert snap["rows"], "the demo watchlist should produce detected setups"
    for r in snap["rows"]:
        mode, s = engine[r["id"]]
        plan = s["plan"]
        price = lambda k: plan[k]["price"] if plan.get(k) else None
        assert r["mode"] == mode and r["ticker"] == s["ticker"]
        assert r["status"] == s["status"] == plan["status"]
        assert (r["strategy"], r["setup"]) == (s["strategy"], s["strategy_name"])
        assert (r["direction"], r["timeframe"]) == (s["direction"], s["timeframe"])
        assert r["entry_trigger"] == price("entry_trigger")
        assert r["stop"] == price("stop") == s["stop"]
        assert r["target1"] == price("target1") == s["target1"]
        assert r["risk_reward"] == plan["risk_reward_t1"] == s["risk_reward"]
        assert r["quality"] == plan["quality"]["score"]
        assert r["last_updated"] == s["evaluated_at"]
        assert r["pattern"] == PATTERN[s["strategy"]]
        assert r["side"] == ("bullish" if s["direction"] == "LONG" else "bearish")


def test_every_detected_setup_is_listed_once_and_ranked(scanned):
    svc, snap = scanned
    engine = engine_setups(svc)
    expected = {i for i, (_, s) in engine.items() if include_row(s)}
    ids = [r["id"] for r in snap["rows"]]
    assert len(ids) == len(set(ids)) and set(ids) == expected
    active = {i for i, (_, s) in engine.items() if s["status"] != "NO_TRADE"}
    assert active <= set(ids)  # every TRIGGERED / WATCHING setup appears
    assert snap["rows"] == sorted(snap["rows"], key=rank_key)
    statuses = [r["status"] for r in snap["rows"]]
    order = {"TRIGGERED": 0, "WATCHING": 1, "NO_TRADE": 2}
    assert [order[x] for x in statuses] == sorted(order[x] for x in statuses)
    modes = {r["mode"] for r in snap["rows"]}
    assert modes == {"day", "swing"}


def test_rows_failing_requirements_are_identified(scanned):
    _, snap = scanned
    for r in snap["rows"]:
        assert r["meets_requirements"] == (r["status"] != "NO_TRADE")
        assert r["actionable"] == (r["status"] == "TRIGGERED" and
                                   r["data_freshness"] in ("fresh", "demo") and
                                   not r["live_checks_failed"])
        if r["status"] == "TRIGGERED" and r["live_checks_failed"]:
            assert set(r["live_checks_failed"]) <= set(r["requirement_issues"])
        if r["status"] == "NO_TRADE":
            assert r["requirement_issues"]
            assert r["entry_trigger"] is not None  # a pattern was detected


def test_ticker_summaries_levels_bias_and_freshness(scanned):
    _, snap = scanned
    assert [t["ticker"] for t in snap["tickers"]] == WATCHLIST
    for t in snap["tickers"]:
        assert t["price"] and set(t["bias"]) == {"day", "swing"}
        for mode in ("day", "swing"):
            assert t["data"][mode]["freshness"] == "demo"
            assert t["data"][mode]["last_candle"]
            lv = t["levels"][mode]
            for side in ("support", "resistance"):
                if lv[side]:
                    assert lv[side]["distance_pct"] >= 0 and isinstance(lv[side]["near"], bool)
        assert t["flagged"] is False
    assert snap["started_at"] and snap["finished_at"] and snap["is_demo"] is True


# ---------------------------------------------------------------------- filters

def test_filters(scanned):
    _, snap = scanned
    rows = snap["rows"]
    assert all(r["mode"] == "day" for r in filter_rows(rows, mode="day"))
    assert all(r["side"] == "bearish" for r in filter_rows(rows, side="bearish"))
    trig = filter_rows(rows, status="TRIGGERED")
    assert trig and all(r["status"] == "TRIGGERED" for r in trig)
    both = filter_rows(rows, status="triggered,watching")
    assert {r["status"] for r in both} <= {"TRIGGERED", "WATCHING"}
    assert all(r["risk_reward"] >= 3 for r in filter_rows(rows, min_rr=3))
    assert all(r["quality"] >= 60 for r in filter_rows(rows, min_quality=60))
    assert all(r["pattern"] == "reversal" for r in filter_rows(rows, strategy="reversal"))
    assert all(r["strategy"] == "swing_breakout"
               for r in filter_rows(rows, strategy="swing_breakout"))
    assert all(r["status"] != "NO_TRADE" for r in filter_rows(rows, include_no_trade=False))
    n = len(rows)
    assert len(filter_rows(rows, side="bullish")) + len(filter_rows(rows, side="bearish")) == n


def test_scanner_api(client):
    j = client.get("/api/scanner", params={"mode": "swing", "status": "TRIGGERED,WATCHING",
                                           "min_rr": 2}).json()
    assert j["total_rows"] >= len(j["rows"])
    assert all(r["mode"] == "swing" and r["risk_reward"] >= 2 for r in j["rows"])
    assert j["scan_count"] == 1 and j["due"] is False and j["watchlist"] == WATCHLIST
    for k in ("started_at", "finished_at", "duration_ms", "tickers", "signature", "feed"):
        assert k in j
    assert client.get("/api/scanner", params={"side": "up"}).status_code == 400
    run = client.post("/api/scanner/run").json()
    assert run["scan_id"] == 2
    legacy = client.post("/api/scan").json()  # recent-setups feed still recorded
    assert legacy["results"] and legacy["scan_id"] == 3


# ------------------------------------------------------------ stale / missing data

class _Flaky(MarketDataService):
    """Demo market where one ticker is stale and another unavailable."""

    def get_candles(self, symbol, timeframe, session="regular", limit=300):
        if symbol == "AMD":
            raise ProviderUnavailable("Alpaca returned 503 for AMD")
        c = super().get_candles(symbol, timeframe, session, limit)
        if symbol == "TSLA":
            c.status = {**c.status, "freshness": "stale",
                        "reason": "Latest candle is 40 minutes old"}
        return c


def test_stale_and_missing_tickers_are_flagged_and_never_actionable(tmp_path):
    st = Settings(database_path=tmp_path / "t.db", scan_interval_seconds=0)
    market = _Flaky(DemoSource(now=FIXED_NOW), symbols=["SPY", "TSLA", "AMD"],
                    now_fn=lambda: FIXED_NOW)
    svc = Service(st, market=market, db=Database(st.database_path))
    snap = svc.scanner.run()
    t = {x["ticker"]: x for x in snap["tickers"]}
    assert t["TSLA"]["flagged"] and "stale" in " ".join(t["TSLA"]["flags"])
    assert t["AMD"]["flagged"] and "unavailable" in " ".join(t["AMD"]["flags"]).lower()
    assert not t["SPY"]["flagged"]
    for r in snap["rows"]:
        if r["ticker"] in ("TSLA", "AMD"):
            assert r["status"] == "NO_TRADE" and not r["actionable"]
            assert r["requirement_issues"][0].startswith("Data ")
    assert not any(r["ticker"] == "AMD" for r in snap["rows"] if r["status"] != "NO_TRADE")
    assert any(e["symbol"] in ("TSLA", "AMD") for e in snap["errors"])


# --------------------------------------------------------------------- scheduling

def test_rescans_only_when_new_eligible_candles_exist(tmp_path):
    clock = [datetime(2026, 3, 10, 10, 2, tzinfo=NY)]
    market = MarketDataService(DemoSource(now=FIXED_NOW), symbols=["SPY"],
                               now_fn=lambda: clock[0])
    st = Settings(database_path=tmp_path / "t.db", scan_interval_seconds=0)
    svc = Service(st, market=market, db=Database(st.database_path))
    sc = svc.scanner
    assert sc.due()
    first = sc.signature()
    sc._signature, sc.snapshot = first, {"rows": []}  # pretend we scanned at 10:02
    clock[0] = datetime(2026, 3, 10, 10, 4, tzinfo=NY)
    assert not sc.due()  # same 5-minute candle still forming: nothing to recompute
    clock[0] = datetime(2026, 3, 10, 10, 5, 30, tzinfo=NY)
    assert sc.due()  # the 10:00-10:05 candle has closed
    sc._signature = sc.signature()
    clock[0] = datetime(2026, 3, 10, 17, 0, tzinfo=NY)
    assert sc.due()  # session closed: the daily candle is complete
    sc._signature = sc.signature()
    clock[0] = datetime(2026, 3, 10, 19, 0, tzinfo=NY)
    assert not sc.due()  # nothing new after the close
    svc.add_symbol("IWM")
    assert sc.due()  # watchlist changed


def test_scan_skips_redundant_work_and_never_calls_claude(tmp_path):
    class Counting:
        model, effort, calls = "claude-opus-5-5", "medium", 0

        def explain_setup(self, facts):
            Counting.calls += 1
            raise AssertionError("the scanner must not call Claude")

    st = Settings(database_path=tmp_path / "t.db", scan_interval_seconds=0)
    svc = Service(st, market=demo_market(), db=Database(st.database_path))
    svc.analyst, svc.ai_enabled = Counting(), True
    svc.scanner.run()
    snap = svc.scanner.run()  # no new candles: returns the same snapshot
    assert svc.scanner.scan_count == 1 and snap["scan_id"] == 1
    assert Counting.calls == 0


# ---------------------------------------------------------------------- watchlist

def test_watchlist_add_remove_validate_and_persist(tmp_path):
    st = Settings(database_path=tmp_path / "t.db", scan_interval_seconds=0)
    db = Database(st.database_path)
    svc = Service(st, market=demo_market(), db=db)
    with TestClient(create_app(svc, run_scanner=False)) as c:
        assert c.post("/api/watchlist", json={"symbol": "iwm"}).json()["watchlist"][-1] == "IWM"
        assert c.post("/api/watchlist", json={"symbol": "IWM"}).status_code == 400  # duplicate
        assert c.post("/api/watchlist", json={"symbol": "12$"}).status_code == 400
        assert c.post("/api/watchlist", json={"symbol": "BRK.B"}).status_code == 200
        assert "IWM" in [q["symbol"] for q in c.get("/api/watchlist").json()["quotes"]]
        assert c.get("/api/health").json()["watchlist"][-2:] == ["IWM", "BRK.B"]
        assert c.delete("/api/watchlist/META").json()["watchlist"].count("META") == 0
        assert c.delete("/api/watchlist/NOPE").status_code == 400
    again = Service(st, market=demo_market(), db=db)  # persisted across restarts
    assert again.market.symbols == [s for s in WATCHLIST if s != "META"] + ["IWM", "BRK.B"]
    one = Service(Settings(database_path=tmp_path / "o.db", scan_interval_seconds=0,
                           watchlist=["SPY"]),
                  market=MarketDataService(DemoSource(now=FIXED_NOW), symbols=["SPY"],
                                           now_fn=lambda: FIXED_NOW),
                  db=Database(tmp_path / "o.db"))
    with pytest.raises(Exception, match="at least one"):
        one.remove_symbol("SPY")


def test_background_scheduler_scans_by_itself_on_each_new_candle(tmp_path):
    """The real server loop (app lifespan), not a manual scan: it scans at startup, stays
    idle while the 5-minute candle is forming, and rescans once it closes."""
    import time as _time
    clock = [datetime(2026, 3, 10, 10, 2, tzinfo=NY)]
    market = MarketDataService(DemoSource(now=FIXED_NOW), symbols=["SPY"],
                               now_fn=lambda: clock[0])
    st = Settings(database_path=tmp_path / "t.db", scan_interval_seconds=1)
    svc = Service(st, market=market, db=Database(st.database_path))

    def wait_for(n: int, seconds: float = 90) -> None:
        end = _time.monotonic() + seconds
        while svc.scanner.scan_count < n and _time.monotonic() < end:
            _time.sleep(0.1)
        assert svc.scanner.scan_count >= n, f"scheduler did not reach scan {n}"

    with TestClient(create_app(svc, run_scanner=True)):
        wait_for(1)
        first = svc.scanner.snapshot
        assert first["trigger"] == "scheduled" and first["signature"][2] == 6
        _time.sleep(2.5)  # several polls inside the same forming candle
        assert svc.scanner.scan_count == 1
        clock[0] = datetime(2026, 3, 10, 10, 5, 30, tzinfo=NY)  # 10:00-10:05 closed
        wait_for(2)
        assert svc.scanner.snapshot["trigger"] == "scheduled"
        assert svc.scanner.snapshot["signature"][2] == 7
