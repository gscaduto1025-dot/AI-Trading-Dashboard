from app.ai.claude import ClaudeAnalyst


def test_health_reports_demo_mode(client):
    r = client.get("/api/health").json()
    assert r["is_demo"] is True
    assert r["data_mode"] == "demo"
    assert r["live_trading"] is False
    assert r["ai_enabled"] is False


def test_watchlist(client):
    r = client.get("/api/watchlist").json()
    assert r["is_demo"] is True
    symbols = [q["symbol"] for q in r["quotes"]]
    assert symbols == ["SPY", "QQQ", "NVDA", "TSLA", "AAPL", "AMD", "MSFT", "META", "AMZN"]


def test_bars_and_analysis(client):
    bars = client.get("/api/bars/NVDA", params={"timeframe": "15Min"}).json()
    assert len(bars["bars"]) == 300
    a = client.get("/api/analysis/NVDA", params={"mode": "day", "timeframe": "15Min"}).json()
    assert a["is_demo"] is True
    assert a["setup"]["status"] in ("ACTIONABLE", "FORMING", "NO_TRADE")
    assert "ema9" in a["overlays"] and "vwap" in a["overlays"]
    swing = client.get("/api/analysis/NVDA", params={"mode": "swing"}).json()
    assert swing["timeframe"] == "1Day"
    assert "sma200" in swing["overlays"]


def test_bad_params(client):
    assert client.get("/api/analysis/SPY", params={"mode": "scalp"}).status_code == 400
    assert client.get("/api/bars/SPY", params={"timeframe": "2Min"}).status_code == 400


def test_scan_records_setups_and_alerts_once(client):
    first = client.post("/api/scan").json()
    assert len(first["results"]) == 18
    setups = client.get("/api/setups/recent").json()["setups"]
    alerts = client.get("/api/alerts").json()["alerts"]
    non_trivial = [r for r in first["results"] if r["status"] != "NO_TRADE"]
    assert len(setups) == len(non_trivial)
    assert len(alerts) == len(first["new_alerts"])
    # Setup-engine alerts (FORMING/ACTIONABLE) plus strategy-engine TRIGGERED signals.
    assert len([a for a in alerts if a["status"] != "TRIGGERED"]) == len(non_trivial)
    signals = client.get("/api/strategies/signals").json()["signals"]
    assert len([a for a in alerts if a["status"] == "TRIGGERED"]) == len(signals)
    # Re-scanning unchanged (demo) data must not duplicate setups or alerts.
    second = client.post("/api/scan").json()
    assert second["new_alerts"] == []
    assert len(client.get("/api/alerts").json()["alerts"]) == len(alerts)
    if alerts:
        aid = alerts[0]["id"]
        assert client.post(f"/api/alerts/{aid}/ack").json() == {"ok": True}
        unacked = client.get("/api/alerts", params={"unacknowledged": True}).json()["alerts"]
        assert aid not in [a["id"] for a in unacked]


def test_ai_without_key_is_explicitly_unavailable(client):
    r = client.post("/api/ai/SPY", params={"mode": "day"}).json()
    assert r["available"] is False
    assert "ANTHROPIC_API_KEY" in r["reason"]


class _FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeResponse:
    stop_reason = "end_turn"
    model = "claude-opus-5-5"
    _request_id = "req_test"

    def __init__(self, text):
        self.content = [_FakeBlock(text)]


class _FakeMessages:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = []

    def create(self, **kwargs):
        import json
        self.calls.append(kwargs)
        return _FakeResponse(json.dumps({
            "headline": "h", "verdict": self.verdict, "summary": "s", "bullish_case": [],
            "bearish_case": [], "key_levels_commentary": "k", "what_to_watch": [],
            "risk_notes": []}))


class _FakeClient:
    def __init__(self, verdict):
        self.beta = type("B", (), {})()
        self.beta.messages = _FakeMessages(verdict)


def test_ai_verdict_never_more_aggressive_than_engine(service):
    from fastapi.testclient import TestClient
    from app.main import create_app

    fake = _FakeClient("TAKE_SETUP")
    service.analyst = ClaudeAnalyst("claude-opus-5-5", client=fake)
    service.ai_enabled = True
    with TestClient(create_app(service, run_scanner=False)) as c:
        engine = c.get("/api/analysis/SPY", params={"mode": "swing"}).json()["setup"]["status"]
        r = c.post("/api/ai/SPY", params={"mode": "swing"}).json()
        assert r["available"] is True and r["cached"] is False
        if engine != "ACTIONABLE":
            assert r["verdict"] in ("WAIT", "NO_TRADE")
        call = fake.beta.messages.calls[0]
        assert call["model"] == "claude-opus-5-5"
        assert call["fallbacks"] == "default"
        assert "DEMO DATA" in call["messages"][0]["content"]
        # Second call is served from the SQLite cache.
        assert c.post("/api/ai/SPY", params={"mode": "swing"}).json()["cached"] is True
        assert len(fake.beta.messages.calls) == 1
