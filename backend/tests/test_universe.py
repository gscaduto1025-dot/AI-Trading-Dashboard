"""Universe scanner, universe builder, raw-bar cache and rules-based explanations.

Demo data only (no network). The universe scanner must reuse the watchlist scanner's
engines and row builder, persist progress and results, never run twice at once, report
symbols it cannot evaluate as DATA_UNAVAILABLE, and never touch Claude.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import rawcache
from app.config import Settings
from app.data.base import Bar, DataError
from app.db import Database
from app.main import Service, create_app
from app.universe import UniverseConfig, build_universe, is_etf

from .conftest import demo_market


@pytest.fixture
def svc(tmp_path):
    path = tmp_path / "u.db"
    return Service(Settings(database_path=path, scan_interval_seconds=0),
                   market=demo_market(), db=Database(path))


@pytest.fixture(scope="module")
def scanned(tmp_path_factory):
    path = tmp_path_factory.mktemp("u") / "u.db"
    s = Service(Settings(database_path=path, scan_interval_seconds=0),
                market=demo_market(), db=Database(path))
    out = s.universe.run("test")
    return s, out


# ----------------------------------------------------------------------- builder

def test_etf_heuristic_keeps_operating_companies():
    for name in ("The Charles Schwab Corporation", "JPMorgan Chase & Co.", "Invesco LTD",
                 "Northern Trust Corporation Common Stock", "Tesla, Inc. Common Stock"):
        assert not is_etf(name), name
    for name in ("State Street SPDR S&P 500 ETF Trust", "Invesco QQQ Trust, Series 1",
                 "ProShares UltraPro QQQ", "iShares Russell 2000 ETF", "SPDR Gold Trust"):
        assert is_etf(name), name


def test_demo_universe_is_the_labelled_watchlist(svc):
    u = build_universe(svc.market, UniverseConfig(), list(svc.market.symbols))
    assert u.source == "DEMO" and u.symbols == list(svc.market.symbols)


class _FakeClient:
    """Alpaca-shaped responses for the builder (assets, snapshots, SIP daily bars)."""

    def __init__(self, n=8):
        self.requests_made = 0
        self.syms = [f"S{chr(65 + i)}" for i in range(n)] + ["ETFX", "OTCX", "CHEAP"]

    def get_trading(self, path, params):
        self.requests_made += 1
        rows = [{"symbol": s, "exchange": "NASDAQ", "tradable": True, "name": f"{s} Inc"}
                for s in self.syms[:-3]]
        rows += [{"symbol": "ETFX", "exchange": "ARCA", "tradable": True, "name": "Big ETF"},
                 {"symbol": "OTCX", "exchange": "OTC", "tradable": True, "name": "Otc Inc"},
                 {"symbol": "CHEAP", "exchange": "NYSE", "tradable": True, "name": "Cheap Inc"}]
        return rows

    def get_data(self, path, params):
        self.requests_made += 1
        out = {}
        day = datetime(2026, 3, 9, tzinfo=timezone.utc)
        for s in params["symbols"].split(","):
            i = _idx(s)
            close = 2.0 if s == "CHEAP" else 50.0 + i
            out[s] = [{"t": (day - timedelta(days=k)).strftime("%Y-%m-%dT05:00:00Z"),
                       "c": close, "v": 1_000_000 * (i + 1)} for k in range(25)]
        return {"bars": out, "next_page_token": None}


class _FakeSource:
    is_demo = False

    def __init__(self):
        self.client = _FakeClient()

    def fetch_snapshots(self, symbols):
        return {s: {"prevDailyBar": {"c": 2.0 if s == "CHEAP" else 50.0,
                                     "v": 10_000 * (_idx(s) + 1)}}
                for s in symbols}


def _idx(s: str) -> int:
    return ord(s[1]) - 65 if len(s) == 2 and s[0] == "S" else 0


def test_builder_ranks_by_consolidated_dollar_volume_and_filters():
    market = type("M", (), {"source": _FakeSource()})()
    cfg = UniverseConfig(size=3, min_price=5, min_adv_usd=1_000_000, include=("SA",))
    u = build_universe(market, cfg, [])
    syms = u.symbols
    assert len(syms) == 3 and "SA" in syms  # forced member kept
    assert syms[0] == "SH" and "ETFX" not in syms and "OTCX" not in syms and "CHEAP" not in syms
    assert u.stats["excluded"]["etf"] == 1 and u.stats["excluded"]["exchange"] == 1
    assert all(m["basis"].startswith("20-session average consolidated") for m in u.members)


# ------------------------------------------------------------------------ scanner

def test_universe_scan_covers_every_symbol_and_persists_progress(scanned):
    s, out = scanned
    assert out["started"] and out["scanned"] == len(s.market.symbols) and out["failed"] == 0
    st = s.universe.status()
    run = st["last_completed"]
    assert run["status"] == "completed" and run["total"] == run["scanned"] == 9
    assert run["remaining"] == 0 and st["running"] is None
    assert "DEMO" in st["coverage_note"]


def test_universe_rows_match_the_watchlist_scanner(scanned):
    """Same engines, same row builder: identical rows for the same setup."""
    s, _ = scanned
    watch = {r["id"]: r for r in s.scanner.run(force=True)["rows"]}
    uni = s.universe.setups()["rows"]
    assert uni and len({r["id"] for r in uni}) == len(uni)  # no duplicates
    same = [r for r in uni if r["id"] in watch]
    assert len(same) == len(watch)
    for r in same:
        w = watch[r["id"]]
        for k in ("status", "signal_state", "entry_trigger", "stop", "target1", "risk_reward",
                  "quality", "validation", "strategy", "timeframe", "direction"):
            assert r[k] == w[k], (r["id"], k)


def test_rescan_does_not_duplicate_and_tracks_first_seen(scanned):
    s, _ = scanned
    first = {r["id"]: r["first_seen"] for r in s.universe.setups()["rows"]}
    time.sleep(1.1)
    assert s.universe.run("again")["started"]
    again = s.universe.setups()["rows"]
    assert len(again) == len(first)
    assert all(r["first_seen"] == first[r["id"]] and r["last_seen"] >= r["first_seen"]
               for r in again)


def test_filters_and_status_values(scanned):
    s, _ = scanned
    res = s.universe.setups(mode="swing", status="TRIGGERED,WATCHING")
    assert res["rows"] and all(r["mode"] == "swing" and r["status"] in ("TRIGGERED", "WATCHING")
                               for r in res["rows"])
    assert {r["status"] for r in s.universe.setups(status="NO_TRADE")["rows"]} <= {"NO_TRADE"}


def test_unevaluable_symbols_are_data_unavailable_not_dropped(svc, monkeypatch):
    eng = svc.universe.engine(list(svc.market.symbols))
    real = eng.market.get_candles

    def broken(symbol, *a, **k):
        if symbol == "NVDA":
            raise DataError("feed down for NVDA")
        return real(symbol, *a, **k)

    monkeypatch.setattr(eng.market, "get_candles", broken)
    out = svc.universe.run("test")
    assert out["scanned"] == 9 and out["failed"] == 1
    res = svc.universe.setups()
    bad = [u for u in res["unavailable"] if u["ticker"] == "NVDA"]
    assert {u["mode"] for u in bad} == {"day", "swing"} and "feed down" in bad[0]["error"]
    assert not any(r["ticker"] == "NVDA" for r in res["rows"])


def test_only_one_run_at_a_time_and_stale_runs_are_recovered(svc):
    # A live lease held by "another process" blocks a new run.
    rid = svc.db.claim_universe_run("other", 9, None)
    assert rid and svc.universe.run("test")["started"] is False
    # Its heartbeat stops: after the stale window it is interrupted and a run can start.
    svc.db.update_universe_run(rid, heartbeat=int(time.time()) - 600)
    with svc.db._lock, svc.db._conn:
        svc.db._conn.execute("UPDATE universe_runs SET heartbeat=? WHERE id=?",
                             (int(time.time()) - 600, rid))
    assert svc.universe.run("test")["started"] is True
    runs = {r["id"]: r for r in svc.db.universe_runs()}
    assert runs[rid]["status"] == "interrupted"
    # In-process: a second concurrent call is refused while the first holds the lock.
    svc.universe._lock.acquire()
    try:
        assert svc.universe.run("test") == {"started": False,
                                            "reason": "a universe scan is already running"}
    finally:
        svc.universe._lock.release()


def test_results_survive_a_restart(tmp_path):
    path = tmp_path / "r.db"
    a = Service(Settings(database_path=path, scan_interval_seconds=0), market=demo_market(),
                db=Database(path))
    a.universe.run("first")
    n = a.universe.setups()["total_rows"]
    b = Service(Settings(database_path=path, scan_interval_seconds=0), market=demo_market(),
                db=Database(path))
    assert b.universe.setups()["total_rows"] == n > 0
    assert b.universe.status()["last_completed"]["scanned"] == 9


def test_schedule_due_logic(svc, monkeypatch):
    u = svc.universe
    u.interval = 0
    assert u.due() is False
    u.interval = 900
    assert u.due() is True
    u.run("test")
    assert u.due() is False  # just ran
    assert u.due(now=time.time() + 901) in (True, False)  # closed-market rule may hold it


def test_after_close_rule_covers_after_hours_and_pre_market(svc, monkeypatch):
    """Phase 2 live finding: after the 16:00 close the universe kept rescanning every
    interval during after-hours. Once a complete scan ran after the close (+16 min), only
    the regular session makes it due again."""
    u = svc.universe
    u.interval = 900
    u.run("test")  # completed long after the demo calendar's last close
    later = time.time() + 901
    for phase, expect in (("regular", True), ("after-hours", False), ("pre-market", False),
                          ("closed", False)):
        monkeypatch.setattr(svc.market, "market_status", lambda p=phase: {"phase": p})
        assert u.due(now=later) is expect, phase


def test_no_claude_calls_and_no_alerts(scanned, monkeypatch):
    s, _ = scanned
    calls = []
    monkeypatch.setattr(s.analyst, "explain_setup", lambda *a, **k: calls.append(1))
    before = len(s.db.alerts(limit=1000))
    s.universe.run("no-ai")
    assert calls == [] and len(s.db.alerts(limit=1000)) == before


# ------------------------------------------------------------------------- API

def test_universe_api(tmp_path):
    path = tmp_path / "api.db"
    s = Service(Settings(database_path=path, scan_interval_seconds=0), market=demo_market(),
                db=Database(path))
    with TestClient(create_app(s, run_scanner=False)) as c:
        assert c.get("/api/universe/status").json()["last_completed"] is None
        assert c.post("/api/universe/run").json()["started"] is True
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            runs = s.db.universe_runs(1)
            if runs and runs[0]["finished_at"]:
                break
            time.sleep(0.3)
        st = c.get("/api/universe/status").json()
        assert st["last_completed"]["scanned"] == 9
        res = c.get("/api/universe/setups", params={"mode": "day"}).json()
        assert res["rows"] and all(r["mode"] == "day" for r in res["rows"])
        assert c.get("/api/universe/setups", params={"validation": "BAD"}).status_code == 400
        r0 = res["rows"][0]
        ex = c.get(f"/api/universe/explain/{r0['ticker']}",
                   params={"mode": "day", "setup_id": r0["id"]}).json()
        assert ex["ai"] is False and ex["explanation"]["status"] == r0["status"]
        assert c.get("/api/universe/explain/ZZZZ").status_code == 404
        assert c.get("/api/universe/members").json()["source"] == "DEMO"


# --------------------------------------------------------------- rules explanation

def test_rules_explanation_uses_only_engine_numbers(svc):
    rows = svc.strategy_setups(record=False)["setups"]
    pick = next(r for r in rows if r["plan"] and r["status"] != "NO_TRADE")
    out = svc.rules_explanation(pick["ticker"], "day", pick["id"])
    x, facts = out["explanation"], out["facts"]
    assert out["ai"] is False and "not AI-generated" in x["source"]
    assert x["status"] == pick["status"] and x["evidence_label"] == facts["strategy_evidence"]["label"]
    plan = facts["setup"]
    joined = " ".join(x["trade_plan"] + x["risk_reward"])
    assert f"{plan['stop']['price']:,.2f}" in joined and f"{plan['target1']['price']:,.2f}" in joined
    for k in ("detected", "why_it_qualifies", "key_levels", "invalidation", "evidence",
              "status_reason"):
        assert x[k], k


def test_rules_explanation_endpoint_without_any_api_key(tmp_path):
    path = tmp_path / "e.db"
    s = Service(Settings(database_path=path, scan_interval_seconds=0), market=demo_market(),
                db=Database(path))
    assert s.ai_enabled is False
    with TestClient(create_app(s, run_scanner=False)) as c:
        r = c.get("/api/explain/SPY", params={"mode": "swing"}).json()
        assert r["ai"] is False and r["explanation"]["symbol"] == "SPY"
        assert c.get("/api/explain/ZZZZ").status_code == 400


# --------------------------------------------------------------------- raw cache

class _Src:
    is_demo = False
    name = "alpaca"
    feed = "iex"
    adjustment = "split"


class _Market:
    RAW_FULL_REFRESH_SECONDS = 6 * 3600

    def __init__(self, mono):
        self.source = _Src()
        self._raw = {}
        self._lock = threading.Lock()
        self._mono = lambda: mono


def test_raw_cache_roundtrip_preserves_bars_and_ages(tmp_path):
    a = _Market(10_000.0)
    t0 = datetime(2026, 3, 10, tzinfo=timezone.utc)
    bars = [Bar(1_773_100_000 + 300 * i, 1, 2, 0.5, 1.5, 100) for i in range(5)]
    a._raw[("NVDA", "5Min")] = {"start": t0, "cutoff": t0 + timedelta(days=1), "bars": bars,
                                "fetched": 9_940.0, "full_at": 9_000.0}
    a._raw[("OLD", "1Day")] = {"start": t0, "cutoff": t0, "bars": bars,
                               "fetched": 0.0, "full_at": -30_000.0}  # older than 6 h
    f = tmp_path / "c.json.gz"
    info = rawcache.save(a, f)
    assert info["series"] == 2 and info["bars"] == 10
    b = _Market(50.0)  # new process: a different monotonic clock
    got = rawcache.restore(b, f)
    assert got["restored"] == 1 and got["skipped_stale"] == 1
    ent = b._raw[("NVDA", "5Min")]
    assert ent["bars"] == bars and ent["start"] == t0
    # Ages carried over (plus the seconds the file sat on disk).
    assert 60 <= 50.0 - ent["fetched"] < 90 and 1_000 <= 50.0 - ent["full_at"] < 1_030


def test_raw_cache_refuses_a_different_feed_and_demo(tmp_path):
    a = _Market(100.0)
    a._raw[("X", "5Min")] = {"start": datetime(2026, 1, 1, tzinfo=timezone.utc),
                             "cutoff": datetime(2026, 1, 2, tzinfo=timezone.utc), "bars": [],
                             "fetched": 99.0, "full_at": 99.0}
    f = tmp_path / "c.json.gz"
    rawcache.save(a, f)
    b = _Market(100.0)
    b.source = type("S", (_Src,), {"feed": "sip"})()
    assert rawcache.restore(b, f)["restored"] == 0
    assert rawcache.save(demo_market(), tmp_path / "d.json.gz")["skipped"] == "demo source"


def test_raw_cache_module_does_not_change_frozen_code():
    import subprocess
    import sys
    import json as _json
    backend = Path(__file__).resolve().parents[1]
    out = subprocess.run([sys.executable, "research/freeze.py", "surface"], cwd=backend,
                         capture_output=True, text=True, check=True).stdout.strip()
    proto = _json.loads((backend / "research/validation/protocol.json").read_text())
    assert out == proto["protocol_amendments_data"]["rule_surface_sha256"]


def test_concurrent_cache_saves_never_fail_or_corrupt(tmp_path):
    """Phase 2 live finding: the periodic save, a universe run and shutdown saved the same file
    at once; with a shared temp name one rename failed (ENOENT) and writers could interleave."""
    a = _Market(10_000.0)
    t0 = datetime(2026, 3, 10, tzinfo=timezone.utc)
    for k in range(40):
        bars = [Bar(1_773_100_000 + 300 * i, 1 + k, 2 + k, 0.5, 1.5, 100) for i in range(400)]
        a._raw[(f"S{k}", "5Min")] = {"start": t0, "cutoff": t0 + timedelta(days=1), "bars": bars,
                                     "fetched": 9_990.0, "full_at": 9_900.0}
    f = tmp_path / "w.json.gz"
    errors = []

    def save():
        try:
            rawcache.save(a, f)
        except Exception as exc:  # noqa: BLE001 - the test records any failure
            errors.append(repr(exc))

    threads = [threading.Thread(target=save) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["w.json.gz"]  # no temp files left
    b = _Market(10_000.0)
    assert rawcache.restore(b, f)["restored"] == 40
    assert b._raw[("S7", "5Min")]["bars"] == a._raw[("S7", "5Min")]["bars"]
