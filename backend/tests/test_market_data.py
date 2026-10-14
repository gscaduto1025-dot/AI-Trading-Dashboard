"""Market data service tests against a fake Alpaca server (no network)."""

from datetime import date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.data.base import AuthError, RateLimitError, SubscriptionError
from app.db import Database
from app.main import Service, create_app
from app.market.alpaca_client import AlpacaClient, RateLimiter
from app.market.calendar import NY
from app.market.service import MarketDataService
from app.market.sources import AlpacaSource
from tests.conftest import WATCHLIST
from tests.fake_alpaca import FakeAlpaca

# Tuesday 2026-03-10, 11:02:30 New York time (regular session).
OPEN_NOW = datetime(2026, 3, 10, 11, 2, 30, tzinfo=NY)


def make(now=OPEN_NOW, feed="iex", **fake_kwargs):
    fake = FakeAlpaca(now, **fake_kwargs)
    sleeps: list[float] = []
    clock = [0.0]  # fake monotonic clock that advances when the client "sleeps"

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    client = AlpacaClient("PKTESTKEY", "SECRETVALUE", transport=fake.transport(), sleep=sleep,
                          clock=lambda: clock[0],
                          rate_limiter=RateLimiter(10_000, clock=lambda: clock[0], sleep=sleep),
                          wall_clock=lambda: now.timestamp() + clock[0])
    market = MarketDataService(AlpacaSource(client, feed), symbols=WATCHLIST,
                               now_fn=lambda: now)
    return fake, market, sleeps


def ny(ts: int) -> str:
    return datetime.fromtimestamp(ts, NY).strftime("%Y-%m-%d %H:%M")


# ------------------------------------------------------------------ HTTP client

def test_auth_headers_and_feed_params_are_sent():
    fake, market, _ = make()
    market.get_candles("SPY", "5Min", limit=50)
    req = next(r for r in fake.requests if r.url.path == "/v2/stocks/bars")
    assert req.headers["APCA-API-KEY-ID"] == "PKTESTKEY"
    assert req.url.params["feed"] == "iex"
    assert req.url.params["timeframe"] == "5Min"
    assert req.url.params["adjustment"] == "split"
    assert req.url.params["sort"] == "asc"
    assert req.method == "GET"


def test_pagination_collects_every_page():
    fake, market, _ = make()
    fake.page_size = 1000  # force several next_page_token round trips
    start = datetime(2026, 3, 2, tzinfo=NY)
    end = datetime(2026, 3, 9, 20, 0, tzinfo=NY)
    expected = fake.series("1Min", start, end)
    raw = market.source.fetch_bars("SPY", "1Min", start, end)
    assert len(raw) == len(expected) > 3000
    bar_requests = [r for r in fake.requests if r.url.path == "/v2/stocks/bars"]
    assert len(bar_requests) == -(-len(expected) // 1000)
    assert "page_token" not in bar_requests[0].url.params
    assert bar_requests[1].url.params["page_token"] == "1000"


def test_retries_429_using_retry_after_then_succeeds():
    fake, market, sleeps = make()
    fake.fail_next = [httpx.Response(429, headers={"Retry-After": "2"}, json={"message": "rate"}),
                      httpx.Response(503, json={"message": "unavailable"})]
    candles = market.get_candles("SPY", "15Min", limit=40)
    assert candles.bars
    assert 2.0 in sleeps  # honoured Retry-After
    assert len([r for r in fake.requests if r.url.path == "/v2/stocks/bars"]) == 3


def test_auth_and_subscription_errors_are_not_retried_and_hide_secrets():
    fake, market, _ = make()
    fake.fail_next = [httpx.Response(401, json={"message": "bad key SECRETVALUE"})]
    with pytest.raises(AuthError) as e:
        market.source.fetch_bars("SPY", "5Min", OPEN_NOW - timedelta(days=1), OPEN_NOW)
    assert "SECRETVALUE" not in str(e.value)
    assert len(fake.requests) == 1

    fake, market, _ = make(feed="sip", sip_allowed=False)
    with pytest.raises(SubscriptionError) as e:
        market.get_candles("SPY", "5Min", limit=50)
    assert "recent SIP" in str(e.value)
    assert len([r for r in fake.requests if r.url.path == "/v2/stocks/bars"]) == 1


def test_gives_up_after_max_retries():
    fake, market, _ = make()
    fake.fail_next = [httpx.Response(500, json={})] * 10
    with pytest.raises(Exception) as e:
        market.source.fetch_bars("SPY", "5Min", OPEN_NOW - timedelta(days=1), OPEN_NOW)
    assert getattr(e.value, "code", "") == "PROVIDER_UNAVAILABLE"
    assert len(fake.requests) == 4  # 1 + 3 retries


def test_rate_limiter_waits_when_bucket_empty():
    t = [0.0]
    slept: list[float] = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    rl = RateLimiter(60, clock=lambda: t[0], sleep=sleep)  # 1 token / second
    for _ in range(60):
        rl.acquire()
    assert slept == []
    rl.acquire()
    assert slept and slept[0] == pytest.approx(1.0, abs=0.01)
    rl.block_until(t[0] + 100)
    with pytest.raises(RateLimitError):
        rl.acquire(max_wait=5)


def test_network_failure_is_unavailable_not_fabricated():
    def boom(request):
        raise httpx.ConnectError("down")

    client = AlpacaClient("k", "s", transport=httpx.MockTransport(boom), sleep=lambda s: None,
                          rate_limiter=RateLimiter(1000, sleep=lambda s: None))
    market = MarketDataService(AlpacaSource(client), symbols=WATCHLIST, now_fn=lambda: OPEN_NOW)
    with pytest.raises(Exception) as e:
        market.get_quotes()
    assert e.value.code == "PROVIDER_UNAVAILABLE"


# --------------------------------------------------------------- service logic

def test_regular_session_completion_and_freshness():
    fake, market, _ = make()
    c = market.get_candles("SPY", "5Min", "regular", limit=100)
    times = [ny(b.time)[-5:] for b in c.bars]
    assert all("09:30" <= t < "16:00" for t in times)  # no extended-hours bars
    assert times[-1] == "11:00"
    assert c.complete[-1] is False  # 11:00-11:05 candle still forming at 11:02:30
    assert all(c.complete[:-1])
    assert c.status["freshness"] == "fresh"
    assert c.status["latency"] == "real-time"
    assert c.status["feed"] == "iex"
    assert c.completed_bars[-1].time == c.bars[-2].time


def test_extended_session_includes_premarket():
    fake, market, _ = make()
    c = market.get_candles("SPY", "15Min", "extended", limit=200)
    today = [ny(b.time) for b in c.bars if ny(b.time).startswith("2026-03-10")]
    assert today[0].endswith("04:00")
    assert any("16:00" <= t[-5:] < "20:00" for t in (ny(b.time) for b in c.bars))


def test_hourly_and_four_hour_candles_anchor_to_session_open():
    fake, market, _ = make(now=datetime(2026, 3, 10, 18, 0, tzinfo=NY))
    h1 = [ny(b.time)[-5:] for b in market.get_candles("SPY", "1Hour", limit=7).bars]
    assert h1 == ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    h4 = market.get_candles("SPY", "4Hour", limit=4)
    assert [ny(b.time)[-5:] for b in h4.bars] == ["09:30", "13:30", "09:30", "13:30"]
    assert all(h4.complete)
    ext = market.get_candles("SPY", "4Hour", "extended", limit=4)
    assert [ny(b.time)[-5:] for b in ext.bars] == ["04:00", "08:00", "12:00", "16:00"]


def test_early_close_day_truncates_session():
    # Day after Thanksgiving 2026: 13:00 close, after-hours until 17:00.
    now = datetime(2026, 11, 27, 13, 30, tzinfo=NY)
    fake, market, _ = make(now=now)
    c = market.get_candles("SPY", "30Min", limit=10)
    assert ny(c.bars[-1].time) == "2026-11-27 12:30"
    assert c.complete[-1]  # closed at 13:00
    h4 = market.get_candles("SPY", "4Hour", limit=2)
    assert ny(h4.bars[-1].time) == "2026-11-27 09:30" and h4.complete[-1]
    status = market.market_status()
    assert status["phase"] == "after-hours"
    assert status["early_close"] == "Day after Thanksgiving"


def test_holiday_status_and_historical_latency():
    now = datetime(2026, 12, 25, 12, 0, tzinfo=NY)
    fake, market, _ = make(now=now)
    status = market.market_status()
    assert status["phase"] == "closed" and status["holiday"] == "Christmas Day"
    assert status["calendar_source"] == "alpaca"
    c = market.get_candles("SPY", "5Min", limit=50)
    assert ny(c.bars[-1].time) == "2026-12-24 12:55"  # Christmas Eve early close
    assert c.status["latency"] == "historical" and c.status["freshness"] == "fresh"
    d = market.get_candles("SPY", "1Day", limit=30)
    assert d.status["freshness"] == "fresh" and all(d.complete)


def test_stale_feed_is_detected_and_blocks_analysis(tmp_path):
    fake, market, _ = make(data_until=datetime(2026, 3, 10, 10, 0, tzinfo=NY))
    c = market.get_candles("SPY", "5Min", limit=100)
    assert c.status["freshness"] == "stale"
    assert c.analysis_block()[0] == "STALE_DATA"
    svc = Service(Settings(database_path=tmp_path / "t.db", scan_interval_seconds=0),
                  market=market, db=Database(tmp_path / "t.db"))
    with TestClient(create_app(svc, run_scanner=False)) as client:
        r = client.get("/api/analysis/SPY", params={"mode": "day", "timeframe": "5Min"})
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "STALE_DATA"
        # The chart can still show the (labelled) stale candles.
        bars = client.get("/api/bars/SPY", params={"timeframe": "5Min"}).json()
        assert bars["status"]["freshness"] == "stale"


def test_delayed_sip_never_requests_the_last_15_minutes():
    fake, market, _ = make(feed="delayed_sip", sip_allowed=False)
    c = market.get_candles("SPY", "5Min", limit=60)
    req = next(r for r in fake.requests if r.url.path == "/v2/stocks/bars")
    assert req.url.params["feed"] == "sip"
    end = datetime.fromisoformat(req.url.params["end"].replace("Z", "+00:00"))
    assert end <= OPEN_NOW - timedelta(minutes=15)
    assert c.status["latency"] == "delayed"
    assert ny(c.bars[-1].time).endswith("10:45")  # data cutoff 10:47:30
    market.get_quotes()
    snap = next(r for r in fake.requests if r.url.path == "/v2/stocks/snapshots")
    assert snap.url.params["feed"] == "delayed_sip"


def test_corrupt_candle_is_dropped_and_blocks_analysis():
    fake, market, _ = make()
    fake.corrupt_at = int(datetime(2026, 3, 10, 10, 0, tzinfo=NY).timestamp())
    c = market.get_candles("SPY", "5Min", limit=100)
    assert not c.quality.ok
    assert fake.corrupt_at not in [b.time for b in c.bars]
    assert c.analysis_block()[0] == "DATA_INTEGRITY"


def test_cache_avoids_repeat_requests():
    fake, market, _ = make()
    market.get_candles("SPY", "5Min", limit=80)
    n = len(fake.requests)
    market.get_candles("SPY", "5Min", limit=80)
    assert len(fake.requests) == n


def test_quotes_regular_session_and_premarket():
    fake, market, _ = make()
    q = {x["symbol"]: x for x in market.get_quotes()}["NVDA"]
    assert q["available"] and q["latency"] == "real-time"
    assert q["volume"] == 1e6
    assert q["change"] == pytest.approx(q["price"] - q["prev_close"], abs=1e-3)

    # 05:00 ET pre-market, before the feed has a daily bar for today: change must be
    # measured from yesterday's close (the latest daily bar) and today's volume is zero.
    fake, market, _ = make(now=datetime(2026, 3, 10, 5, 0, tzinfo=NY))
    fake.daily_bar_from_regular_open = True
    q = {x["symbol"]: x for x in market.get_quotes()}["SPY"]
    assert q["prev_close"] == pytest.approx(fake._daily(fake.calendar.day(date(2026, 3, 9)))["c"])
    assert q["volume"] == 0
    assert q["latency"] == "real-time"

    # 03:00 ET overnight: still showing the last session's move, labelled historical.
    fake, market, _ = make(now=datetime(2026, 3, 10, 3, 0, tzinfo=NY))
    q = {x["symbol"]: x for x in market.get_quotes()}["SPY"]
    assert q["prev_close"] == pytest.approx(fake._daily(fake.calendar.day(date(2026, 3, 6)))["c"])
    assert q["volume"] == 1e6
    assert q["latency"] == "historical"


def test_calendar_falls_back_to_rules_when_alpaca_calendar_fails():
    fake, market, _ = make()
    orig = fake.handle

    def handle(request):
        if request.url.path == "/v2/calendar":
            return httpx.Response(403, json={"message": "forbidden"})
        return orig(request)

    fake.handle = handle
    client = AlpacaClient("PKTESTKEY", "SECRETVALUE", transport=httpx.MockTransport(handle),
                          sleep=lambda s: None, rate_limiter=RateLimiter(1000, sleep=lambda s: None))
    market = MarketDataService(AlpacaSource(client), symbols=WATCHLIST, now_fn=lambda: OPEN_NOW)
    status = market.market_status()
    assert status["calendar_source"] == "nyse-rules"
    assert status["calendar_error"]
    assert status["phase"] == "regular"


def test_unknown_symbol_rejected():
    _, market, _ = make()
    with pytest.raises(Exception) as e:
        market.get_candles("GME", "5Min")
    assert e.value.code == "INVALID_REQUEST"


# ------------------------------------------------------------------------- API

@pytest.fixture
def live_client(tmp_path):
    fake, market, _ = make()
    settings = Settings(alpaca_key_id="PKTESTKEY", alpaca_secret_key="SECRETVALUE",
                        database_path=tmp_path / "t.db", scan_interval_seconds=0)
    svc = Service(settings, market=market, db=Database(tmp_path / "t.db"))
    with TestClient(create_app(svc, run_scanner=False)) as c:
        yield fake, c


def test_api_serves_real_candles_and_completed_only_analysis(live_client):
    fake, client = live_client
    health = client.get("/api/health")
    assert health.json()["is_demo"] is False
    assert "SECRETVALUE" not in health.text and "PKTESTKEY" not in health.text
    bars = client.get("/api/bars/SPY", params={"timeframe": "5Min"}).json()
    assert bars["status"]["source"] == "alpaca" and bars["last_bar_complete"] is False
    a = client.get("/api/analysis/SPY", params={"mode": "day", "timeframe": "5Min"}).json()
    assert a["is_demo"] is False
    assert a["analyzed_through"] == bars["bars"][-2]["time"]  # forming candle excluded
    assert a["excluded_forming_candle"] is True
    wl = client.get("/api/watchlist").json()
    assert wl["market"]["phase"] == "regular" and len(wl["quotes"]) == 9


def test_api_reports_market_data_unavailable(live_client):
    fake, client = live_client
    fake.fail_next = [httpx.Response(500, json={})] * 20
    r = client.get("/api/watchlist")
    assert r.status_code == 503
    assert r.json()["detail"]["display"] == "MARKET DATA UNAVAILABLE"
    assert "price" not in r.text


def test_history_endpoint_json_and_csv(live_client):
    fake, client = live_client
    params = {"timeframe": "1Day", "start": "2025-01-01", "end": "2025-12-31"}
    r = client.get("/api/history/AAPL", params=params).json()
    days = [datetime.fromtimestamp(b["time"], NY).date() for b in r["bars"]]
    assert len(days) == 250  # NYSE 2025 had 250 trading days (incl. the Jan 9 closure)
    assert date(2025, 1, 9) not in days and date(2025, 7, 4) not in days
    csv_text = client.get("/api/history/AAPL", params={**params, "format": "csv"}).text
    assert csv_text.splitlines()[0].startswith("time_utc,time_ny,open,high,low,close,volume")
    assert len(csv_text.splitlines()) == 251


def test_settings_repr_hides_secrets():
    s = Settings(alpaca_key_id="PKTESTKEY", alpaca_secret_key="SECRETVALUE")
    assert "SECRETVALUE" not in repr(s) and "PKTESTKEY" not in repr(s)
