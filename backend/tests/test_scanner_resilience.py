"""Scanner resilience (simulated, against the in-process fake Alpaca): failed API requests
and a stalled feed never crash the scanner or produce actionable signals, and the
scanner recovers by itself when data returns. Mirrors the 2026-10-15 Alpaca vendor
outage (no new trades or bars on IEX or SIP for 75 minutes)."""

from __future__ import annotations

import time as _time
from datetime import datetime, timedelta

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import Service, create_app
from app.market.alpaca_client import AlpacaClient, RateLimiter
from app.market.calendar import NY
from app.market.service import MarketDataService
from app.market.sources import AlpacaSource
from tests.fake_alpaca import FakeAlpaca

SYMBOLS = ["SPY", "QQQ"]


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start
        self.mono = 0.0

    def sleep(self, s: float) -> None:
        self.mono += s


def make(tmp_path, start=datetime(2026, 3, 10, 10, 31, tzinfo=NY)):
    clock = Clock(start)
    fake = FakeAlpaca(clock.now)
    client = AlpacaClient("PKTESTKEY", "SECRETVALUE", transport=fake.transport(),
                          sleep=clock.sleep, clock=lambda: clock.mono,
                          rate_limiter=RateLimiter(10_000, clock=lambda: clock.mono,
                                                   sleep=clock.sleep),
                          wall_clock=lambda: clock.now.timestamp() + clock.mono)
    # Cache TTLs follow the simulated clock, as real time would pass between scans.
    market = MarketDataService(AlpacaSource(client, "iex"), symbols=SYMBOLS,
                               now_fn=lambda: clock.now,
                               monotonic=lambda: clock.now.timestamp() + clock.mono)
    svc = Service(Settings(database_path=tmp_path / "t.db", scan_interval_seconds=0,
                           watchlist=SYMBOLS),
                  market=market, db=Database(tmp_path / "t.db"), with_universe=False)

    def advance(minutes: float) -> None:
        clock.now += timedelta(minutes=minutes)
        fake.now = clock.now

    return fake, svc, advance


def freshness(snap) -> set[str]:
    return {t["data"]["day"]["freshness"] for t in snap["tickers"]}


def actionable_alerts(svc) -> int:
    return sum(1 for a in svc.db.alerts(500, False, None, None, None) if a["actionable"])


def test_api_errors_and_stalled_feed_never_crash_and_recovery_is_automatic(tmp_path):
    fake, svc, advance = make(tmp_path)
    sc = svc.scanner

    s1 = sc.run(trigger="scheduled")
    assert freshness(s1) == {"fresh"} and not s1["errors"]
    rows_before = len(s1["rows"])
    assert rows_before > 0

    # 1. Hard API failures (HTTP 503 on every stock-data request, retries included).
    advance(5)
    fake.fail_next = [httpx.Response(503, json={"message": "unavailable"})] * 500
    s2 = sc.run(trigger="scheduled")  # must not raise
    assert sc.scan_count == 2
    assert not any(r["actionable"] for r in s2["rows"])
    fake.fail_next = []

    # 2. Stalled feed: no new bars after 10:36 (like the 2026-10-15 vendor outage).
    advance(1)
    fake.data_until = fake.now
    advance(34)
    s3 = sc.run(trigger="scheduled")
    assert sc.scan_count == 3
    assert "fresh" not in freshness(s3), freshness(s3)
    assert not any(r["actionable"] for r in s3["rows"])
    assert actionable_alerts(svc) == 0

    # 3. Data returns: the next scheduled scan is fresh again without any restart.
    fake.data_until = None
    advance(5)
    assert sc.due()
    s4 = sc.run(trigger="scheduled")
    assert sc.scan_count == 4
    assert freshness(s4) == {"fresh"}, freshness(s4)
    assert len(s4["rows"]) >= rows_before
    assert not [e for e in s4["errors"] if e["symbol"] != "*"]


def test_background_scan_loop_survives_exceptions(tmp_path, monkeypatch):
    fake, svc, _ = make(tmp_path)
    svc.settings = Settings(database_path=tmp_path / "t.db", scan_interval_seconds=0.05,
                            watchlist=SYMBOLS)
    calls = {"n": 0}
    real_run = svc.scanner.run

    def flaky(force=False, trigger="scheduled"):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("simulated scan crash")
        return real_run(force=True, trigger=trigger)

    monkeypatch.setattr(svc.scanner, "run", flaky)
    monkeypatch.setattr(svc.scanner, "due", lambda: True)
    with TestClient(create_app(svc, run_scanner=True)) as client:
        deadline = _time.time() + 20
        while _time.time() < deadline and calls["n"] < 4:
            _time.sleep(0.05)
        assert calls["n"] >= 4, "the loop stopped after an exception"
        assert svc.scanner.last_error is None
        assert client.get("/api/health").status_code == 200


def test_the_sessions_last_candle_is_scanned_after_the_close(tmp_path):
    """Phase 2 live finding: the 16:00:03 scan ran before the 15:55 candle was final (5 s
    completion grace), and the signature was already "closed", so the last candle of the
    session was never scanned. The signature now advances only once a candle is final."""
    fake, svc, advance = make(tmp_path, datetime(2026, 3, 10, 15, 57, tzinfo=NY))
    sc = svc.scanner
    sc.run(trigger="scheduled")
    advance(3 + 3 / 60)  # 16:00:03, inside the completion grace
    assert not sc.due()
    advance(4 / 60)  # 16:00:07
    assert sc.due()
    snap = sc.run(trigger="scheduled")
    assert snap["signature"][2] == "closed"
    assert {t["data"]["day"]["last_candle"] for t in snap["tickers"]} == {
        "2026-03-10T15:55:00-04:00"}
    advance(60)
    assert not sc.due()  # nothing further after the close
