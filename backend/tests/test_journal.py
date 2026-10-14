"""Trade log (actual + paper): P&L for longs, shorts, partial exits and scaling in; fees;
R multiples; validation; duplicate fills; strict separation from paper trades and from
simulated backtest trades."""

from __future__ import annotations

import sqlite3

import pytest

from app.journal.pnl import Fill, JournalError, compute, stats

T = 1_773_000_000


def F(side, q, p, fees=0.0, dt=0):
    return Fill(side, q, p, fees, T + dt)


# ------------------------------------------------------------------ P&L engine

def test_simple_long():
    r = compute("LONG", [F("BUY", 100, 50.0, 1.0), F("SELL", 100, 52.0, 1.0, 60)], 49.0)
    assert r["status"] == "closed" and r["realized_gross"] == 200.0
    assert r["fees"] == 2.0 and r["realized_net"] == 198.0
    assert r["initial_risk"] == 100.0 and r["r_multiple"] == 1.98
    assert r["avg_entry"] == 50.0 and r["avg_exit"] == 52.0 and r["partial_exits"] == 0


def test_short_profits_when_price_falls():
    r = compute("SHORT", [F("SELL", 50, 100.0, 0.5), F("BUY", 50, 96.0, 0.5, 60)], 102.0)
    assert r["realized_gross"] == 200.0 and r["realized_net"] == 199.0
    assert r["initial_risk"] == 100.0 and r["r_multiple"] == 1.99


def test_short_loss():
    r = compute("SHORT", [F("SELL", 10, 100.0), F("BUY", 10, 103.0, dt=60)])
    assert r["realized_net"] == -30.0 and r["r_multiple"] is None  # no stop -> no R


def test_partial_exits_long():
    fills = [F("BUY", 300, 20.0, 1.0), F("SELL", 100, 21.0, 1.0, 60),
             F("SELL", 100, 22.0, 1.0, 120), F("SELL", 100, 19.5, 1.0, 180)]
    r = compute("LONG", fills, 19.0)
    assert r["partial_exits"] == 2 and r["status"] == "closed"
    assert r["realized_gross"] == 100 + 200 - 50
    assert r["realized_net"] == 250 - 4
    assert r["initial_risk"] == 300.0 and r["r_multiple"] == pytest.approx(246 / 300, abs=1e-4)
    assert [e["pnl"] for e in r["exits"]] == [100.0, 200.0, -50.0]


def test_open_position_after_partial_exit_and_unrealized():
    r = compute("LONG", [F("BUY", 100, 10.0), F("SELL", 40, 12.0, dt=60)], mark=11.0)
    assert r["status"] == "open" and r["open_quantity"] == 60
    assert r["realized_net"] == 80.0 and r["unrealized"] == 60.0
    assert r["r_multiple"] is None  # only closed trades get an R


def test_scaling_in_uses_average_cost():
    r = compute("LONG", [F("BUY", 100, 10.0), F("BUY", 100, 12.0, dt=60),
                         F("SELL", 200, 13.0, dt=120)], 9.0)
    assert r["realized_gross"] == 400.0  # avg entry 11
    assert r["max_position"] == 200 and r["initial_risk"] == 200.0  # first entry 10 - stop 9


def test_short_scale_in_and_partial_cover():
    r = compute("SHORT", [F("SELL", 100, 50.0), F("SELL", 100, 52.0, dt=10),
                          F("BUY", 150, 48.0, dt=20)])
    assert r["status"] == "open" and r["open_quantity"] == 50
    assert r["realized_gross"] == 150 * (51 - 48)


@pytest.mark.parametrize("direction,fills,msg", [
    ("LONG", [F("BUY", 10, 5.0), F("SELL", 11, 6.0, dt=1)], "exceeds the open position"),
    ("SHORT", [F("SELL", 10, 5.0), F("BUY", 20, 4.0, dt=1)], "exceeds the open position"),
    ("LONG", [F("SELL", 10, 5.0)], "no open LONG position"),
    ("SHORT", [F("BUY", 10, 5.0)], "no open SHORT position"),
    ("LONG", [F("BUY", 0, 5.0)], "positive"),
    ("LONG", [F("BUY", 1, 5.0, fees=-1)], "fees"),
    ("UP", [F("BUY", 1, 5.0)], "direction"),
])
def test_inconsistent_fills_are_rejected(direction, fills, msg):
    with pytest.raises(JournalError, match=msg):
        compute(direction, fills)


def test_same_timestamp_fills_keep_recorded_order():
    # regression: a new exit with the entry's timestamp was sorted before it and rejected
    entry = Fill("SELL", 10, 400.0, 0.0, T, id=1)
    r = compute("SHORT", [entry, Fill("BUY", 4, 396.0, 0.0, T)])
    assert r["realized_net"] == 16.0 and r["open_quantity"] == 6
    r = compute("LONG", [Fill("BUY", 5, 10.0, 0.0, T), Fill("SELL", 5, 11.0, 0.0, T)])
    assert r["status"] == "closed" and r["realized_net"] == 5.0


def test_stats_separate_and_small_sample_flagged():
    closed = lambda net: {"pnl": {"status": "closed", "realized_net": net, "fees": 1.0,  # noqa: E731
                                  "r_multiple": net / 100}}
    s = stats([closed(200), closed(-100), closed(-100), {"pnl": {"status": "open", "fees": 0}}])
    assert s["closed_trades"] == 3 and s["open_trades"] == 1
    assert s["win_rate"] == pytest.approx(33.33) and s["net_pnl"] == 0
    assert s["profit_factor"] == 1.0 and "too few" in s["sample_note"]


# ------------------------------------------------------------------ API / storage

BUY = {"side": "BUY", "quantity": 100, "price": 50, "fees": 1, "executed_at": "2026-03-10T10:00:00-04:00"}


def test_journal_api_long_partial_exits_and_duplicates(client):
    r = client.post("/api/journal/actual/trades",
                    json={"ticker": "spy", "direction": "LONG", "planned_stop": 49, "fills": [BUY]})
    assert r.status_code == 200
    t = r.json()
    assert t["ticker"] == "SPY" and t["pnl"]["status"] == "open"
    assert "demo" in t["mark_note"].lower()  # demo prices never value a real position
    tid = t["id"]
    part = {"side": "SELL", "quantity": 40, "price": 52, "fees": 1,
            "executed_at": "2026-03-10T11:00:00-04:00"}
    t = client.post(f"/api/journal/actual/trades/{tid}/fills", json=part).json()
    assert t["pnl"]["realized_net"] == 80 - 2 and t["pnl"]["open_quantity"] == 60
    dup = client.post(f"/api/journal/actual/trades/{tid}/fills", json=part)
    assert dup.status_code == 409  # duplicate fill
    over = client.post(f"/api/journal/actual/trades/{tid}/fills",
                       json=part | {"quantity": 100, "executed_at": "2026-03-10T12:00:00-04:00"})
    assert over.status_code == 400 and "exceeds" in over.json()["detail"]
    t = client.post(f"/api/journal/actual/trades/{tid}/fills",
                    json=part | {"quantity": 60, "price": 49.5,
                                 "executed_at": "2026-03-10T15:00:00-04:00"}).json()
    p = t["pnl"]
    assert p["status"] == "closed" and p["partial_exits"] == 1
    assert p["realized_net"] == pytest.approx(80 - 30 - 3) and p["r_multiple"] == 0.47
    # deleting the first exit is fine; deleting the entry would orphan the exits
    fills = {f["side"] + str(f["quantity"]): f["id"] for f in t["fills"]}
    assert client.delete(f"/api/journal/actual/trades/{tid}/fills/{fills['BUY100.0']}"
                         ).status_code == 400
    j = client.get("/api/journal/actual").json()
    assert j["stats"]["closed_trades"] == 1 and j["simulated"] is False
    assert j["order_execution"] is False
    csv_ = client.get("/api/journal/actual/export.csv").text
    assert csv_.count("\n") == 4 and "SPY" in csv_


def test_short_trade_via_api(client):
    t = client.post("/api/journal/paper/trades", json={
        "ticker": "TSLA", "direction": "SHORT", "planned_stop": 210,
        "fills": [{"side": "SELL", "quantity": 10, "price": 200, "executed_at": T},
                  {"side": "BUY", "quantity": 10, "price": 190, "executed_at": T + 600}]}).json()
    assert t["pnl"]["realized_net"] == 100 and t["pnl"]["r_multiple"] == 1.0


def test_accounts_are_separate_and_backtests_cannot_enter(client, service):
    client.post("/api/journal/paper/trades", json={"ticker": "AAPL", "direction": "LONG",
                                                  "fills": [BUY]})
    assert client.get("/api/journal/actual").json()["trades"] == []
    assert len(client.get("/api/journal/paper").json()["trades"]) == 1
    r = client.post("/api/journal/backtest/trades", json={"ticker": "AAPL", "direction": "LONG",
                                                         "fills": [BUY]})
    assert r.status_code == 400 and "kept separately" in r.json()["detail"]
    assert client.get("/api/journal/backtest").status_code == 400
    # paper trade ids are not reachable through the actual account
    pid = client.get("/api/journal/paper").json()["trades"][0]["id"]
    assert client.delete(f"/api/journal/actual/trades/{pid}").status_code == 404
    # the database itself refuses any other account (defence in depth)
    with pytest.raises(sqlite3.IntegrityError):
        service.db.journal_insert_trade("backtest", {"ticker": "X", "direction": "LONG"}, [])
    # backtest runs live in their own table and never appear in the trade log
    service.backtests.start({"mode": "swing", "data_source": "bundled", "symbols": ["AAPL"],
                             "start": "2015-09-01", "end": "2017-02-16"}, background=False)
    run = service.db.backtests()[0]
    assert run["status"] == "done" and run["summary"]["trades"] > 0
    assert len(client.get("/api/journal/paper").json()["trades"]) == 1
    assert client.get("/api/journal/actual").json()["trades"] == []


def test_validation_errors(client):
    bad = [
        {"ticker": "??", "direction": "LONG", "fills": [BUY]},
        {"ticker": "SPY", "direction": "LONG", "fills": []},
        {"ticker": "SPY", "direction": "SIDEWAYS", "fills": [BUY]},
        {"ticker": "SPY", "direction": "LONG", "fills": [BUY | {"side": "SELL"}]},
        {"ticker": "SPY", "direction": "LONG", "fills": [BUY | {"executed_at": "yesterday"}]},
        {"ticker": "SPY", "direction": "LONG", "fills": [BUY, BUY]},
    ]
    for body in bad:
        assert client.post("/api/journal/actual/trades", json=body).status_code == 400, body
    assert client.post("/api/journal/actual/trades/999/fills", json=BUY).status_code == 404
