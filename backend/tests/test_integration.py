"""Full-system integration through the REAL Alpaca code path (HTTP client, rate limiter,
sources, market service, engines, scanner, alerts, options, API), backed by in-process
fakes of Alpaca's documented endpoints. Includes failure injection: a provider outage
mid-session, a lagging (stale) feed, missing subscriptions, incomplete candles and
duplicate signals."""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import Service, create_app
from app.market.alpaca_client import AlpacaClient, RateLimiter
from app.market.calendar import NY
from app.market.service import MarketDataService
from app.market.sources import AlpacaSource

from .fake_alpaca import FakeAlpaca
from .fake_options import FakeOptions

OPEN_NOW = datetime(2026, 3, 10, 11, 2, 30, tzinfo=NY)  # Tuesday, regular session
SYMBOLS = ["SPY", "QQQ"]


class Stack:
    """A Service wired to fake Alpaca stock + options endpoints, with a movable clock."""

    def __init__(self, tmp_path, *, feed="iex", options_feed="indicative", sip_allowed=True):
        self.clock = [OPEN_NOW]
        self.fa = FakeAlpaca(OPEN_NOW, sip_allowed=sip_allowed)
        self.fa.page_size = 10_000
        self.fo = FakeOptions(OPEN_NOW, underlying="SPY", price=100.0,
                              strikes=[100 + d for d in range(-8, 9)], page_size=1000)
        self.down = False  # simulate an Alpaca outage for stock data

        def handle(req: httpx.Request) -> httpx.Response:
            if "/options/" in req.url.path:
                return self.fo.handle(req)
            if self.down and req.url.path.startswith("/v2/stocks"):
                return httpx.Response(503, json={"message": "service unavailable"})
            return self.fa.handle(req)

        self.client = AlpacaClient("PKTESTKEY", "SECRETVALUE", transport=httpx.MockTransport(handle),
                                   rate_limiter=RateLimiter(100_000), max_retries=1,
                                   sleep=lambda s: None,
                                   wall_clock=lambda: self.clock[0].timestamp())
        market = MarketDataService(AlpacaSource(self.client, feed), symbols=list(SYMBOLS),
                                   now_fn=lambda: self.clock[0],
                                   monotonic=lambda: self.clock[0].timestamp())
        st = Settings(database_path=tmp_path / "int.db", scan_interval_seconds=0,
                      alpaca_options_feed=options_feed)
        self.svc = Service(st, market=market, db=Database(st.database_path))

    def advance(self, minutes: int) -> None:
        self.clock[0] = self.clock[0] + timedelta(minutes=minutes)
        self.fa.now = self.clock[0]


@pytest.fixture
def stack(tmp_path):
    return Stack(tmp_path)


def test_full_stack_scan_alerts_and_options_on_the_alpaca_path(stack):
    svc = stack.svc
    assert svc.is_demo is False and svc.options is not None
    snap = svc.scanner.run()
    # scan coverage: every watchlist ticker in both modes, fresh, no errors
    assert snap["errors"] == []
    assert [t["ticker"] for t in snap["tickers"]] == SYMBOLS
    for t in snap["tickers"]:
        assert not t["flagged"]
        assert {m: t["data"][m]["freshness"] for m in ("day", "swing")} == \
            {"day": "fresh", "swing": "fresh"}
    # incomplete candles: at 11:02:30 the 11:00 5-minute candle is still forming
    last = datetime.fromisoformat(snap["tickers"][0]["data"]["day"]["last_candle"])
    assert last.astimezone(NY).strftime("%H:%M") == "10:55"
    # market data validity: quotes come from the provider, never filled in
    q = {x["symbol"]: x for x in svc.market.get_quotes()}
    assert all(q[s]["available"] and q[s]["latency"] == "real-time" for s in SYMBOLS)
    assert {t["ticker"]: t["price"] for t in snap["tickers"]} == {s: q[s]["price"] for s in SYMBOLS}
    # alerts carry the real feed, and are actionable only from fresh complete rows
    alerts = snap["new_alerts"]
    assert alerts and all(a["feed_status"]["feed"] == "IEX" and not a["is_demo"] for a in alerts)
    rows = {r["id"]: r for r in snap["rows"]}
    for a in alerts:
        if a["actionable"]:
            r = rows[a["setup_id"]]
            assert r["status"] == "TRIGGERED" and r["data_freshness"] == "fresh"
            assert (a["stop"], a["risk_reward"]) == (r["stop"], r["risk_reward"])
    # options on the same path: indicative feed -> explorable, never recommended
    o = svc.options_analysis("SPY", "day")
    assert o["available"] and o["contracts_total"] > 0 and o["feed"]["id"] == "indicative"
    assert o["recommendation"]["contract"] is None
    assert any("not actual OPRA" in r for r in o["recommendation"]["reasons"])
    assert any(str(r.url.path).startswith("/v1beta1/options/snapshots/SPY")
               for r in stack.fo.requests)


def test_duplicate_signals_and_alerts_are_not_recreated(stack):
    svc = stack.svc
    svc.scanner.run()
    signals = svc.db.signals(500)
    alerts = svc.db.alerts(500)
    again = svc.scanner.run(force=True)  # same candles: a rescan must add nothing
    assert again["new_alerts"] == []
    assert len(svc.db.signals(500)) == len(signals)
    assert len(svc.db.alerts(500)) == len(alerts)
    ids = [s["id"] for s in svc.db.signals(500)]
    assert len(ids) == len(set(ids))


def test_provider_outage_mid_session_is_flagged_and_never_actionable(stack):
    svc = stack.svc
    svc.scanner.run()
    stack.down = True
    stack.advance(30)  # new candles are due, caches expire, Alpaca returns 503
    snap = svc.scanner.run(force=True)
    assert all(t["flagged"] for t in snap["tickers"])
    assert any(e["code"] in ("PROVIDER_UNAVAILABLE", "MARKET_DATA_UNAVAILABLE")
               for e in snap["errors"])
    assert not any(a["actionable"] for a in snap["new_alerts"])
    assert {a["type"] for a in snap["new_alerts"]} <= {"data_stale"}
    with TestClient(create_app(svc, run_scanner=False)) as c:
        r = c.get("/api/analysis/SPY", params={"mode": "day"})
        assert r.status_code == 503
        assert r.json()["detail"]["display"] == "MARKET DATA UNAVAILABLE"
    stack.down = False  # recovery: fresh data, analysis works again
    stack.advance(5)
    snap = svc.scanner.run(force=True)
    assert not any(t["flagged"] for t in snap["tickers"])


def test_lagging_feed_is_stale_and_never_actionable(tmp_path):
    s = Stack(tmp_path)
    s.fa.data_until = OPEN_NOW - timedelta(minutes=40)
    snap = s.svc.scanner.run()
    day = [t["data"]["day"] for t in snap["tickers"]]
    assert all(d["freshness"] == "stale" for d in day)
    assert all(t["flagged"] for t in snap["tickers"])
    assert not any(r["actionable"] for r in snap["rows"])
    assert not any(a["actionable"] for a in snap["new_alerts"])
    assert any(a["type"] == "data_stale" for a in snap["new_alerts"])


def test_missing_subscriptions_are_reported_not_hidden(tmp_path):
    s = Stack(tmp_path, feed="sip", options_feed="opra", sip_allowed=False)
    s.fo.status["/v1beta1/options"] = 403
    with TestClient(create_app(s.svc, run_scanner=False)) as c:
        r = c.get("/api/bars/SPY", params={"timeframe": "5Min"})
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "SUBSCRIPTION_NOT_PERMITTED"
        snap = s.svc.scanner.run()
        assert all(t["flagged"] for t in snap["tickers"])
        assert not any(a["actionable"] for a in snap["new_alerts"])
        s2 = Stack(tmp_path / "o", options_feed="opra")  # stocks OK, OPRA not subscribed
        s2.fo.status["/v1beta1/options"] = 403
        o = s2.svc.options_analysis("SPY", "day")
        assert o["available"] is False and o["code"] == "SUBSCRIPTION_NOT_PERMITTED"
        assert "OPTIONS DATA UNAVAILABLE" in o["reason"]


def test_health_reports_the_real_feed_and_no_live_trading(stack):
    with TestClient(create_app(stack.svc, run_scanner=False)) as c:
        h = c.get("/api/health").json()
        assert h["data_mode"] == "alpaca" and h["is_demo"] is False
        assert h["feed"]["label"] == "IEX" and h["live_trading"] is False
        routes = [r.path for r in c.app.routes]
        assert not any("order" in p for p in routes)
