

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
