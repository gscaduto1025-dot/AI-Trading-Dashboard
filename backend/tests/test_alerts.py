"""Alert engine: status transitions, duplicate prevention, cooldowns, preferences, stale and
incomplete data, user price levels, webhook delivery and the server-side scheduler."""

from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.alerts import AlertEngine, PrefsError, WebhookNotifier, merge_prefs
from app.config import Settings
from app.data.demo import DemoSource
from app.db import Database
from app.main import Service, create_app
from app.market.service import MarketDataService

from .conftest import FIXED_NOW, demo_market
from .test_scanner import _Flaky

T0 = 1_773_000_000
CANDLE = "2026-03-10T14:35:00-04:00"


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def data(freshness="fresh", candle=CANDLE):
    return {"freshness": freshness, "reason": None if freshness == "fresh" else "40 min old",
            "as_of": candle, "last_candle": candle, "timeframe": "5Min"}


def row(status="WATCHING", *, ticker="SPY", mode="day", strategy="trend_pullback",
        pattern="pullback", tf="5Min", direction="LONG", trig_time=None, outcome=None,
        quality=65.0, rr=2.5, stop=99.0, target1=105.0, target2=108.0, entry=101.0,
        price=101.2, freshness="fresh", session="2026-03-10", candle=CANDLE, sid="s1"):
    fresh = freshness in ("fresh", "demo")
    return {
        "id": sid, "ticker": ticker, "mode": mode, "price": price, "market_bias": "bullish",
        "setup": strategy.replace("_", " ").title(), "strategy": strategy, "pattern": pattern,
        "timeframe": tf, "direction": direction,
        "side": "bullish" if direction == "LONG" else "bearish",
        "entry_trigger": 100.8, "entry": entry if status == "TRIGGERED" else None,
        "stop": stop, "target1": target1, "target2": target2, "risk_reward": rr,
        "quality": quality, "status": status, "meets_requirements": status != "NO_TRADE",
        "actionable": status == "TRIGGERED" and fresh,
        "requirement_issues": ["Risk/reward: 1.4 below 2.0"] if status == "NO_TRADE" else [],
        "reason": "conditions", "last_updated": candle, "session_date": session,
        "key_level": {"label": "VWAP", "low": 100.5, "high": 100.5, "timeframe": "5Min"},
        "trigger_time": trig_time, "outcome": outcome, "outcome_time": None,
        "entry_conditions": [{"name": "Close above trigger", "passed": status == "TRIGGERED",
                              "detail": "5Min close"}],
        "invalidation": ["A close below 99.50 invalidates the pullback."],
        "data_freshness": freshness, "data_as_of": candle, "last_candle": candle,
    }


def ticker(sym="SPY", price=101.2, day="fresh", swing="fresh", levels=None, flags=None,
           candle=CANDLE):
    return {"ticker": sym, "price": price, "data": {"day": data(day, candle),
                                                     "swing": data(swing, candle)},
            "levels": levels or {"day": {"support": None, "resistance": None},
                                 "swing": {"support": None, "resistance": None}},
            "flags": flags if flags is not None else
            [f"{m}: data {f} - 40 min old" for m, f in (("day", day), ("swing", swing))
             if f not in ("fresh", "demo")],
            "counts": {"TRIGGERED": 0, "WATCHING": 0, "NO_TRADE": 0}}


def snap(rows, tickers=None, scan_id=1, is_demo=False):
    return {"scan_id": scan_id, "is_demo": is_demo, "setup_states": rows,
            "tickers": tickers or [ticker()],
            "feed": {"source": "alpaca", "label": "IEX", "latency": "real-time",
                     "delay_seconds": 0}}


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "a.db")


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def engine(db, clock):
    return AlertEngine(db, clock=clock)


def types(alerts):
    return [a["type"] for a in alerts]


# ------------------------------------------------------------------ status changes

def test_watching_to_triggered_alerts_once_with_full_content(engine):
    assert engine.process(snap([row("WATCHING")])) == []
    out = engine.process(snap([row("TRIGGERED", trig_time=T0, outcome="open")]))
    assert types(out) == ["setup_triggered"]
    a = out[0]
    assert a["previous_status"] == "WATCHING" and a["status"] == "TRIGGERED"
    assert "WATCHING -> TRIGGERED" in a["message"] and a["actionable"] is True
    # Every field the alert must carry.
    for k in ("ticker", "mode", "strategy", "setup", "direction", "price", "key_level", "status",
              "entry_conditions", "stop", "targets", "risk_reward", "timestamp",
              "feed_status", "candle_time", "invalidation", "entry", "entry_trigger"):
        assert a[k] not in (None, [], ""), k
    assert a["targets"] == [105.0, 108.0] and a["feed_status"]["freshness"] == "fresh"
    assert a["feed_status"]["feed"] == "IEX" and "no order is placed" in a["message"]
    # Same trigger on the next scan: nothing new.
    assert engine.process(snap([row("TRIGGERED", trig_time=T0, outcome="open")])) == []


def test_alert_type_follows_strategy_family(engine):
    rows = [row("TRIGGERED", strategy="opening_range_breakout", pattern="breakout",
                trig_time=T0, outcome="open"),
            row("TRIGGERED", strategy="breakout_retest", pattern="retest", trig_time=T0,
                outcome="open"),
            row("TRIGGERED", strategy="sr_rejection", pattern="reversal", trig_time=T0,
                outcome="open", direction="SHORT")]
    assert sorted(types(engine.process(snap(rows)))) == [
        "breakout_confirmed", "retest_confirmed", "setup_triggered"]


def test_a_new_trigger_on_the_same_setup_alerts_again(engine):
    engine.process(snap([row("TRIGGERED", trig_time=T0, outcome="open")]))
    out = engine.process(snap([row("TRIGGERED", trig_time=T0 + 900, outcome="open", sid="s2")]))
    assert types(out) == ["setup_triggered"] and out[0]["trigger_time"] == T0 + 900


def test_watching_setup_that_becomes_invalid(engine):
    engine.process(snap([row("WATCHING")]))
    out = engine.process(snap([row("NO_TRADE", candle="2026-03-10T14:40:00-04:00")]))
    assert types(out) == ["setup_invalidated"]
    assert out[0]["previous_status"] == "WATCHING" and out[0]["actionable"] is False
    assert "Risk/reward" in out[0]["message"]
    assert engine.process(snap([row("NO_TRADE")])) == []  # NO_TRADE -> NO_TRADE: quiet


def test_targets_and_stop_reached(engine):
    engine.process(snap([row("TRIGGERED", trig_time=T0, outcome="open")]))
    out = engine.process(snap([row("TRIGGERED", trig_time=T0, outcome="target1")]))
    assert types(out) == ["target_reached"] and out[0]["key_level"]["label"] == "T1"
    out = engine.process(snap([row("NO_TRADE", trig_time=T0, outcome="target1_then_stopped")]))
    assert types(out) == ["stop_reached"]  # resolved: not reported as "invalidated"
    assert out[0]["key_level"]["low"] == 99.0


def test_target2_hit_in_one_candle_reports_both_targets(engine):
    engine.process(snap([row("TRIGGERED", trig_time=T0, outcome="open")]))
    out = engine.process(snap([row("NO_TRADE", trig_time=T0, outcome="target2")]))
    assert [a["key_level"]["label"] for a in out] == ["T1", "T2"]


def test_outcome_of_a_trigger_never_seen_open_is_not_alerted(engine):
    # First scan ever sees an already-stopped trade: history, not news.
    assert engine.process(snap([row("NO_TRADE", trig_time=T0, outcome="stopped")])) == []


def test_new_trading_day_does_not_invalidate_yesterdays_watch(engine):
    engine.process(snap([row("WATCHING", session="2026-03-09")]))
    assert engine.process(snap([row("NO_TRADE", session="2026-03-10")])) == []


def test_strong_swing_candidate(engine):
    weak = row("WATCHING", mode="swing", tf="1Day", strategy="swing_breakout",
               pattern="breakout", quality=60)
    assert engine.process(snap([weak])) == []
    strong = {**weak, "quality": 78.0}
    out = engine.process(snap([strong]))
    assert types(out) == ["swing_candidate"] and "not confirmed" in out[0]["message"]
    assert engine.process(snap([strong])) == []  # still a candidate: no repeat


# ------------------------------------------------------------------ duplicates

def test_duplicate_prevention_across_rescans_and_restarts(db, clock):
    s = snap([row("TRIGGERED", trig_time=T0, outcome="open")])
    first = AlertEngine(db, clock=clock).process(s)
    assert len(first) == 1
    assert AlertEngine(db, clock=clock).process(s) == []  # restart, state intact
    db.put_state("engine", {})  # state lost (e.g. restored backup): dedupe key still holds
    clock.t += 3600
    eng = AlertEngine(db, clock=clock)
    assert eng.process(s) == []
    assert any(x["reason"] == "duplicate" for x in eng.last_run["suppressed"])
    assert len(db.alerts()) == 1


def test_dedupe_key_is_enforced_by_the_database(db):
    a = {"type": "setup_triggered", "severity": "actionable", "actionable": True,
         "ticker": "SPY", "mode": "day"}
    assert db.insert_alert(a, "k1", "c1", T0) is not None
    assert db.insert_alert(a, "k1", "c1", T0 + 1) is None  # e.g. a second scanner process
    assert len(db.alerts()) == 1


# ------------------------------------------------------------------ cooldowns

def test_cooldown_suppresses_repeats_until_it_expires(engine, clock):
    engine.update_prefs({"cooldown_minutes": {"setup_invalidated": 30}})
    watch, dead = row("WATCHING"), row("NO_TRADE")
    engine.process(snap([watch]))
    assert types(engine.process(snap([dead]))) == ["setup_invalidated"]
    clock.t += 600
    engine.process(snap([{**watch, "last_candle": "c2"}]))
    assert engine.process(snap([{**dead, "last_candle": "c3"}])) == []
    assert engine.last_run["suppressed"][0]["reason"].startswith("cooldown")
    clock.t += 1900  # 30+ minutes after the first alert
    engine.process(snap([{**watch, "last_candle": "c4"}]))
    assert types(engine.process(snap([{**dead, "last_candle": "c5"}]))) == ["setup_invalidated"]


def test_zero_cooldown_for_triggers(engine, clock):
    engine.process(snap([row("TRIGGERED", trig_time=T0, outcome="open")]))
    clock.t += 1
    out = engine.process(snap([row("TRIGGERED", trig_time=T0 + 300, outcome="open")]))
    assert types(out) == ["setup_triggered"]


# ------------------------------------------------------------------ stale / incomplete data

def test_stale_data_never_produces_an_actionable_alert(engine):
    stale = snap([row("TRIGGERED", trig_time=T0, outcome="open", freshness="stale")],
                 [ticker(day="stale")])
    out = engine.process(stale)
    assert types(out) == ["data_stale"] and out[0]["severity"] == "warning"
    assert not any(a["actionable"] for a in out)
    assert engine.last_run["suppressed"][0]["reason"] == "data not fresh"
    assert engine.process(stale) == []  # one warning per outage


def test_stale_data_cannot_invalidate_or_trigger_and_state_carries_forward(engine):
    engine.process(snap([row("WATCHING")]))
    # Outage: the engine gate reports NO_TRADE for the stale ticker - must not "invalidate".
    out = engine.process(snap([row("NO_TRADE", freshness="stale")], [ticker(day="stale")]))
    assert types(out) == ["data_stale"]
    # Data recovers and the setup has triggered meanwhile: alert with the true transition.
    out = engine.process(snap([row("TRIGGERED", trig_time=T0, outcome="open")]))
    assert types(out) == ["setup_triggered"] and out[0]["previous_status"] == "WATCHING"


def test_unavailable_feed_flag_blocks_alerts_even_if_freshness_looks_ok(engine):
    t = ticker(flags=["day: MARKET DATA UNAVAILABLE - Alpaca returned 503"])
    out = engine.process(snap([row("TRIGGERED", trig_time=T0, outcome="open")], [t]))
    assert types(out) == ["data_stale"]


def test_incomplete_plan_is_not_alerted_until_complete(engine):
    out = engine.process(snap([row("TRIGGERED", trig_time=T0, outcome="open", stop=None)]))
    assert out == [] and "missing stop" in engine.last_run["suppressed"][0]["reason"]
    out = engine.process(snap([row("TRIGGERED", trig_time=T0, outcome="open", price=None)]))
    assert out == [] and "missing price" in engine.last_run["suppressed"][0]["reason"]
    out = engine.process(snap([row("TRIGGERED", trig_time=T0, outcome="open")]))
    assert types(out) == ["setup_triggered"]


def test_scanner_integration_stale_and_missing_tickers(tmp_path):
    st = Settings(database_path=tmp_path / "t.db", scan_interval_seconds=0)
    market = _Flaky(DemoSource(now=FIXED_NOW), symbols=["SPY", "TSLA", "AMD"],
                    now_fn=lambda: FIXED_NOW)
    svc = Service(st, market=market, db=Database(st.database_path))
    out = svc.scanner.run()["new_alerts"]
    bad = [a for a in out if a["ticker"] in ("TSLA", "AMD")]
    assert {a["type"] for a in bad} == {"data_stale"}
    assert {(a["ticker"], a["mode"]) for a in bad} >= {("TSLA", "day"), ("AMD", "day"),
                                                       ("AMD", "swing")}
    assert not any(a["actionable"] and a["ticker"] in ("TSLA", "AMD") for a in out)


def test_scanner_alerts_match_scanner_rows(tmp_path):
    st = Settings(database_path=tmp_path / "t.db", scan_interval_seconds=0)
    svc = Service(st, market=demo_market(), db=Database(st.database_path))
    snap_ = svc.scanner.run()
    rows = {r["id"]: r for r in snap_["rows"]}
    trig = [a for a in snap_["new_alerts"] if a["actionable"]]
    assert trig, "demo data has TRIGGERED setups"
    assert {a["setup_id"] for a in trig} == {r["id"] for r in snap_["rows"]
                                             if r["status"] == "TRIGGERED"}
    for a in trig:
        r = rows[a["setup_id"]]
        assert (a["stop"], a["risk_reward"], a["entry"]) == (r["stop"], r["risk_reward"], r["entry"])
        assert a["targets"][0] == r["target1"] and a["is_demo"] and a["title"].startswith("[DEMO]")
    assert svc.scanner.run(force=True)["new_alerts"] == []  # rescan: no duplicates


# ------------------------------------------------------------------ preferences

def test_disabled_types_muted_tickers_and_modes(engine):
    engine.update_prefs({"enabled": {"setup_triggered": False}})
    assert engine.process(snap([row("TRIGGERED", trig_time=T0, outcome="open")])) == []
    assert engine.last_run["suppressed"][0]["reason"] == "type disabled"
    engine.update_prefs({"enabled": {"setup_triggered": True}})
    assert engine.process(snap([row("TRIGGERED", trig_time=T0, outcome="open")])) == []  # no replay
    engine.update_prefs({"muted_tickers": ["spy"]})
    assert engine.prefs()["muted_tickers"] == ["SPY"]
    assert engine.process(snap([row("TRIGGERED", trig_time=T0 + 300, outcome="open")])) == []
    engine.update_prefs({"muted_tickers": [], "modes": ["swing"]})
    assert engine.process(snap([row("TRIGGERED", trig_time=T0 + 600, outcome="open")])) == []
    assert engine.last_run["suppressed"][0]["reason"] == "mode disabled"


def test_quality_and_rr_floors(engine):
    engine.update_prefs({"min_quality": 70, "min_risk_reward": 3})
    engine.process(snap([row("TRIGGERED", trig_time=T0, outcome="open", quality=65)]))
    assert "quality" in engine.last_run["suppressed"][0]["reason"]
    engine.process(snap([row("TRIGGERED", trig_time=T0 + 1, outcome="open", quality=80)]))
    assert "R:R" in engine.last_run["suppressed"][0]["reason"]


def test_preference_validation():
    with pytest.raises(PrefsError):
        merge_prefs({}, {"enabled": {"nope": True}})
    with pytest.raises(PrefsError):
        merge_prefs({}, {"cooldown_minutes": {"data_stale": -1}})
    with pytest.raises(PrefsError):
        merge_prefs({}, {"modes": ["scalp"]})
    with pytest.raises(PrefsError):
        merge_prefs({}, {"whatever": 1})
    p = merge_prefs({"proximity_atr": 1}, {"cooldown_minutes": {"data_stale": 5}})
    assert p["proximity_atr"] == 1.0 and p["cooldown_minutes"]["data_stale"] == 5
    assert p["cooldown_minutes"]["swing_candidate"] == 1440  # untouched default


# ------------------------------------------------------------------ key levels

def _levels(dist_atr, label="Daily support zone", low=99.0, high=100.0):
    lv = {"label": label, "low": low, "high": high, "timeframe": "1Day", "strength": 80,
          "distance": 1.2, "distance_pct": 1.2, "distance_atr": dist_atr, "near": True,
          "inside": False}
    return {"day": {"support": lv, "resistance": None},
            "swing": {"support": None, "resistance": None}}


def test_approaching_support_uses_proximity_preference(engine):
    assert engine.process(snap([], [ticker(levels=_levels(0.8))])) == []
    engine.update_prefs({"proximity_atr": 1.0})
    out = engine.process(snap([], [ticker(levels=_levels(0.8))]))
    assert types(out) == ["approaching_support"]
    assert out[0]["key_level"]["label"] == "Daily support zone" and not out[0]["actionable"]
    assert engine.process(snap([], [ticker(levels=_levels(0.3))])) == []  # same zone


# ------------------------------------------------------------------ user price levels

def test_price_levels_api_one_shot(client):
    price = next(q["price"] for q in client.get("/api/watchlist").json()["quotes"]
                 if q["symbol"] == "SPY")
    r = client.post("/api/alerts/levels", json={"ticker": "spy", "price": price * 0.5})
    assert r.status_code == 200 and r.json()["level"]["condition"] == "below"
    bad = client.post("/api/alerts/levels", json={"ticker": "SPY", "price": price * 2,
                                                   "condition": "below"})
    assert bad.status_code == 400 and "already below" in bad.json()["detail"]
    assert client.post("/api/alerts/levels", json={"ticker": "ZZZZ", "price": 1}).status_code == 400
    hit = client.post("/api/alerts/levels", json={"ticker": "SPY", "price": price * 1.5,
                                                   "note": "test"}).json()["level"]
    svc = client.app.state.service
    svc.db._conn.execute("UPDATE alert_levels SET price=? WHERE id=?", (price * 0.99, hit["id"]))
    svc.scanner.run()
    got = [a for a in svc.db.alerts() if a["type"] == "price_level"]
    assert len(got) == 1 and got[0]["ticker"] == "SPY" and "test" in got[0]["message"]
    levels = {lv["id"]: lv for lv in client.get("/api/alerts/levels").json()["levels"]}
    assert levels[hit["id"]]["active"] is False and levels[hit["id"]]["triggered_at"]
    svc.scanner.run(force=True)
    assert len([a for a in svc.db.alerts() if a["type"] == "price_level"]) == 1
    assert client.delete(f"/api/alerts/levels/{hit['id']}").json() == {"ok": True}
    assert client.delete(f"/api/alerts/levels/{hit['id']}").status_code == 404


def test_price_level_waits_for_fresh_data(engine, db):
    lv = db.add_price_level("SPY", 100.0, "above", "")
    engine.process(snap([], [ticker(price=101.2, day="stale")]))
    assert db.price_levels()[0]["active"] is True
    out = engine.process(snap([], [ticker(price=101.2)]))
    assert types(out) == ["price_level"] and out[0]["key_level"]["low"] == lv["price"]


# ------------------------------------------------------------------ API, webhook, scheduler

def test_alerts_api(client):
    client.post("/api/scanner/run")
    j = client.get("/api/alerts").json()
    assert j["alerts"] and j["counts"]["unread"] == len(j["alerts"])
    only = client.get("/api/alerts", params={"type": "setup_triggered"}).json()["alerts"]
    assert only and all(a["type"] == "setup_triggered" for a in only)
    assert client.get("/api/alerts", params={"type": "bogus"}).status_code == 400
    newest = j["alerts"][0]["id"]
    assert client.get("/api/alerts", params={"after_id": newest}).json()["alerts"] == []
    p = client.get("/api/alerts/preferences").json()
    assert set(p["types"]) >= {"approaching_support", "stop_reached", "data_stale"}
    assert p["webhook"] == {"configured": False, "host": None}
    r = client.put("/api/alerts/preferences", json={"cooldown_minutes": {"data_stale": 15}})
    assert r.json()["preferences"]["cooldown_minutes"]["data_stale"] == 15
    assert client.put("/api/alerts/preferences", json={"modes": ["x"]}).status_code == 400
    reset = client.post("/api/alerts/preferences/reset").json()["preferences"]
    assert reset["cooldown_minutes"]["data_stale"] == 60
    assert client.post("/api/alerts/ack-all").json()["acknowledged"] == len(j["alerts"])
    h = client.get("/api/health").json()
    assert h["alerts"]["unread"] == 0 and h["alerts"]["last_run"]["created"] >= 1
    assert h["scanner"]["scan_count"] == 1 and h["live_trading"] is False


def test_webhook_delivery_and_secret_url_never_exposed(tmp_path):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200)

    url = "https://hooks.example.com/services/SECRET-TOKEN"
    notifier = WebhookNotifier(url, client=httpx.Client(transport=httpx.MockTransport(handler)))
    db = Database(tmp_path / "w.db")
    eng = AlertEngine(db, clock=Clock(), notifier=notifier)
    out = eng.process(snap([row("TRIGGERED", trig_time=T0, outcome="open")]))
    assert out[0]["delivery"] == "webhook sent" and len(sent) == 1
    assert b"never places orders" in sent[0].content
    assert db.alerts()[0]["delivery"] == "webhook sent"

    st = Settings(database_path=tmp_path / "s.db", scan_interval_seconds=0, alert_webhook_url=url)
    assert "SECRET" not in repr(st)
    svc = Service(st, market=demo_market(), db=Database(st.database_path))
    with TestClient(create_app(svc, run_scanner=False)) as c:
        body = c.get("/api/alerts/preferences").text + c.get("/api/health").text
        assert "SECRET" not in body and "hooks.example.com" in body


def test_failed_webhook_is_recorded_not_raised(db):
    def boom(request):
        raise httpx.ConnectError("refused")

    n = WebhookNotifier("https://x.invalid/h", client=httpx.Client(
        transport=httpx.MockTransport(boom)))
    out = AlertEngine(db, clock=Clock(), notifier=n).process(
        snap([row("TRIGGERED", trig_time=T0, outcome="open")]))
    assert out[0]["delivery"].startswith("webhook failed") and len(db.alerts()) == 1


def test_scheduler_creates_alerts_with_no_dashboard_open(tmp_path):
    st = Settings(database_path=tmp_path / "bg.db", scan_interval_seconds=1)
    market = MarketDataService(DemoSource(now=FIXED_NOW), symbols=["NVDA"],
                               now_fn=lambda: FIXED_NOW)
    svc = Service(st, market=market, db=Database(st.database_path))
    with TestClient(create_app(svc, run_scanner=True)):
        deadline = time.monotonic() + 60
        while svc.scanner.scan_count == 0 and time.monotonic() < deadline:
            time.sleep(0.2)
    # Nobody called the API: the server-side loop scanned and the alert engine ran.
    assert svc.scanner.scan_count >= 1 and svc.alerts.last_run is not None
    assert any(a["ticker"] == "NVDA" for a in svc.db.alerts())
