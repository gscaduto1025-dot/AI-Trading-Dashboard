"""Regression tests for the Phase 1 pre-launch audit fixes: the request guard (cross-site
requests, DNS rebinding), journal input validation and CSV formula defusing."""

from __future__ import annotations

import time

from app.guard import host_ok, reject_reason

NONE = frozenset()


# ------------------------------------------------------------------- request guard

def test_cross_site_and_same_site_requests_are_refused():
    for site in ("cross-site", "same-site", "CROSS-SITE"):
        assert reject_reason({"sec-fetch-site": site, "host": "localhost:3000"}, NONE)
    for site in ("same-origin", "none", ""):
        assert reject_reason({"sec-fetch-site": site, "host": "localhost:3000"}, NONE) is None
    assert reject_reason({"host": "127.0.0.1:8000"}, NONE) is None  # curl / scripts


def test_only_local_hosts_are_accepted():
    for h in ("localhost:3000", "LOCALHOST", "app.localhost", "127.0.0.1:8000", "[::1]:8000",
              "::1", "192.168.1.20:3000", "10.0.0.5", "172.16.3.4:3000", "169.254.1.1"):
        assert host_ok(h, NONE), h
    for h in ("attacker.example", "attacker.example:3000", "8.8.8.8:3000", "evil.com.",
              "localhost.evil.com"):
        assert not host_ok(h, NONE), h
    assert host_ok("mypc.tailnet.ts.net:3000", frozenset({"mypc.tailnet.ts.net"}))
    assert host_ok("anything.example", frozenset({"*"}))


def test_forwarded_host_from_the_dashboard_proxy_is_checked():
    # The Next.js proxy rewrites Host to the backend and forwards the browser's Host.
    rebinding = {"host": "127.0.0.1:8000", "x-forwarded-host": "attacker.example:3000"}
    assert "not allowed" in reject_reason(rebinding, NONE)
    phone = {"host": "127.0.0.1:8000", "x-forwarded-host": "192.168.1.20:3000",
             "sec-fetch-site": "same-origin"}
    assert reject_reason(phone, NONE) is None


def test_api_refuses_cross_site_posts_and_foreign_hosts(client):
    r = client.post("/api/scanner/run", headers={"Sec-Fetch-Site": "cross-site",
                                                 "Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "FORBIDDEN"
    r = client.get("/api/journal/actual", headers={"Host": "attacker.example"})
    assert r.status_code == 403
    assert client.get("/api/journal/actual",
                      headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200


# ------------------------------------------------------------------ journal inputs

def _post(client, **fill):
    f = {"side": "BUY", "quantity": 1, "price": 100, "executed_at": 1_773_000_000} | fill
    return client.post("/api/journal/actual/trades",
                       json={"ticker": "SPY", "direction": "LONG", "fills": [f]})


def test_millisecond_and_out_of_range_times_are_rejected_and_journal_keeps_working(client):
    assert _post(client).status_code == 200
    for bad in (1_773_000_000_000, 0, -5, time.time() + 3 * 86_400, "1969-12-31T00:00:00Z"):
        r = _post(client, executed_at=bad)
        assert r.status_code == 400, bad
    for path in ("/api/journal/actual", "/api/journal/actual/export.csv", "/api/performance"):
        assert client.get(path).status_code == 200, path
    assert len(client.get("/api/journal/actual").json()["trades"]) == 1


def test_non_finite_numbers_and_too_many_fills_are_rejected(client):
    for field in ("quantity", "price", "fees"):
        for bad in ("inf", "-inf", "nan", "1e999"):
            assert _post(client, **{field: bad}).status_code == 400, (field, bad)
    many = [{"side": "BUY", "quantity": 1, "price": 100, "executed_at": 1_773_000_000 + i}
            for i in range(501)]
    r = client.post("/api/journal/actual/trades",
                    json={"ticker": "SPY", "direction": "LONG", "fills": many})
    assert r.status_code == 400 and "500 fills" in r.text


def test_csv_export_defuses_formulas(client):
    r = client.post("/api/journal/paper/trades", json={
        "ticker": "SPY", "direction": "LONG", "strategy": '=HYPERLINK("http://x","y")',
        "fills": [{"side": "BUY", "quantity": 1, "price": 100, "executed_at": 1_773_000_000}]})
    assert r.status_code == 200
    csv = client.get("/api/journal/paper/export.csv").text
    assert "'=HYPERLINK" in csv and ",=HYPERLINK" not in csv and ',"=HYPERLINK' not in csv


# ------------------------------------------------------- data-source labels (phase 2)

def test_stale_data_is_never_labelled_with_the_feeds_nominal_latency():
    from app.ai.facts import _source_label

    fresh = {"feed_label": "IEX", "latency": "real-time", "freshness": "fresh"}
    assert _source_label(fresh) == "IEX (real-time)"
    stale = fresh | {"freshness": "stale",
                     "reason": "Latest 5 minutes candle is 33 min old during an open session"}
    label = _source_label(stale)
    assert label.startswith("IEX (real-time feed; data STALE: Latest 5 minutes candle is 33")
    assert "(real-time)" not in label
    assert "UNAVAILABLE" in _source_label(fresh | {"freshness": "unavailable", "reason": None})
