"""Trade journal extensions, performance analytics and saved setups.

Actual and paper trades never mix; saved (detected) setups are never trades; every number
comes from recorded fills."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from app.db import Database
from app.journal.pnl import Fill, compute

T = 1_773_000_000  # 2026-03-08
DAY = 86_400


def fill(side, q, p, fees=0.0, dt=0):
    return {"side": side, "quantity": q, "price": p, "fees": fees, "executed_at": T + dt}


def trade(client, account="actual", **kw):
    body = {"ticker": "NVDA", "direction": "LONG", "fills": [fill("BUY", 10, 100.0, 1.0)]} | kw
    r = client.post(f"/api/journal/{account}/trades", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------- new fields

def test_new_fields_evidence_snapshot_result_and_duration(client):
    t = trade(client, mode="day", strategy="liquidity_sweep_reversal", planned_stop=98.0,
              planned_target=104.0, planned_target2=106.0, quality=72,
              fills=[fill("BUY", 10, 100.0, 1.0), fill("SELL", 10, 103.0, 1.0, 3600)])
    assert t["mode"] == "day" and t["asset_type"] == "stock" and t["planned_target2"] == 106.0
    assert t["quality"] == 72 and t["evidence_status"] == "NEGATIVE_HISTORICAL_EVIDENCE"
    assert t["evidence_label"] == "Negative historical evidence"
    p = t["pnl"]
    assert p["result"] == "win" and p["duration_seconds"] == 3600
    assert p["realized_gross"] == 30.0 and p["realized_net"] == 28.0 and p["r_multiple"] == 1.4
    assert t["trade_date"] == "2026-03-08"
    other = trade(client, strategy="my own idea")
    assert other["evidence_status"] is None and other["evidence_label"] is None


def test_breakeven_and_loss_results():
    flat = compute("LONG", [Fill("BUY", 1, 10, 0, T), Fill("SELL", 1, 10, 0, T + 5)])
    loss = compute("SHORT", [Fill("SELL", 1, 10, 0, T), Fill("BUY", 1, 11, 0, T + 5)])
    assert flat["result"] == "breakeven" and loss["result"] == "loss"
    assert compute("LONG", [Fill("BUY", 1, 10, 0, T)])["result"] is None  # open


def test_options_use_the_contract_multiplier(client):
    t = trade(client, ticker="NVDA", asset_type="option", option_symbol="nvda261120c00180000",
              planned_stop=1.0, fills=[fill("BUY", 2, 1.50, 1.3), fill("SELL", 2, 2.00, 1.3, 600)])
    p = t["pnl"]
    assert t["asset_type"] == "option" and t["multiplier"] == 100.0
    assert t["option_symbol"] == "NVDA261120C00180000"
    assert p["realized_gross"] == 100.0 and p["realized_net"] == pytest.approx(97.4)
    assert p["initial_risk"] == 100.0 and p["r_multiple"] == pytest.approx(0.974)
    bad = client.post("/api/journal/actual/trades", json={
        "ticker": "NVDA", "direction": "LONG", "asset_type": "future",
        "fills": [fill("BUY", 1, 1.0)]})
    assert bad.status_code == 400


def test_close_and_edit(client):
    t = trade(client, planned_stop=95.0, fills=[fill("BUY", 10, 100.0), fill("SELL", 4, 104.0, 0, 60)])
    tid = t["id"]
    closed = client.post(f"/api/journal/actual/trades/{tid}/close",
                         json={"price": 106.0, "fees": 1.0, "executed_at": T + 120}).json()
    assert closed["pnl"]["status"] == "closed" and closed["pnl"]["exited_quantity"] == 10
    assert closed["pnl"]["realized_gross"] == 4 * 4 + 6 * 6
    assert client.post(f"/api/journal/actual/trades/{tid}/close",
                       json={"price": 106.0, "executed_at": T + 180}).status_code == 400
    e = client.patch(f"/api/journal/actual/trades/{tid}",
                   json={"mode": "swing", "planned_target2": 110, "quality": 55,
                         "strategy": "swing_breakout", "notes": "edited"}).json()
    assert e["mode"] == "swing" and e["planned_target2"] == 110 and e["quality"] == 55
    assert e["evidence_label"] == "Negative historical evidence" and e["notes"] == "edited"
    for bad in ({"mode": "scalp"}, {"quality": 150}):
        assert client.patch(f"/api/journal/actual/trades/{tid}", json=bad).status_code == 400


def test_old_databases_are_migrated_in_place(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE journal_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT NOT NULL, ticker TEXT NOT NULL, direction TEXT NOT NULL,
            strategy TEXT, setup_id TEXT, planned_stop REAL, planned_target REAL,
            notes TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL);
        INSERT INTO journal_trades (account, ticker, direction, created_at, updated_at)
            VALUES ('actual', 'AAPL', 'LONG', 1, 1);""")
    con.commit()
    con.close()
    db = Database(path)
    t = db.journal_trades("actual")[0]
    assert t["ticker"] == "AAPL" and t["asset_type"] == "stock" and t["multiplier"] == 1
    assert t["mode"] is None and t["evidence_label"] is None


# ------------------------------------------------------------------ performance

def _closed(client, account, ticker, strategy, mode, pnl_per_share, day, asset="stock"):
    kw = {"asset_type": asset} if asset == "option" else {}
    return trade(client, account, ticker=ticker, strategy=strategy, mode=mode, planned_stop=99.0,
                 fills=[fill("BUY", 1, 100.0, 0, day * DAY),
                        fill("SELL", 1, 100.0 + pnl_per_share, 0, day * DAY + 60)], **kw)


def test_performance_metrics_filters_and_account_separation(client):
    seq = [10, -5, 20, -15, 5]
    for i, x in enumerate(seq):
        _closed(client, "actual", "AAPL" if i % 2 else "MSFT", "A", "day", x, i)
    for i in range(5):
        _closed(client, "actual", "TSLA", "B", "swing", -1, 10 + i)
    _closed(client, "paper", "AAPL", "A", "day", 1000, 1)
    trade(client, "actual", ticker="AMD")  # open trade: not in realized stats
    p = client.get("/api/performance", params={"account": "actual"}).json()
    s = p["summary"]
    assert s["closed_trades"] == 10 and p["open_trades"] == 1 and s["total_trades"] == 11
    assert s["net_pnl"] == sum(seq) - 5  # paper trade excluded
    assert s["win_rate"] == 30.0 and s["avg_win"] == round(35 / 3, 2)
    assert s["profit_factor"] == round(35 / 25, 3)
    # cumulative: 10, 5, 25, 10, 15, then -1 x5 -> 10. Max drawdown 25 -> 10 = 15.
    assert [c["cumulative"] for c in p["cumulative"]][:5] == [10, 5, 25, 10, 15]
    assert s["max_drawdown"] == 15.0
    assert p["best_strategy"]["key"] == "A" and p["worst_strategy"]["key"] == "B"
    assert {g["key"] for g in p["by_mode"]} == {"day", "swing"}
    assert p["monthly"] and sum(m["net_pnl"] for m in p["monthly"]) == s["net_pnl"]
    only = client.get("/api/performance", params={"account": "actual", "ticker": "tsla"}).json()
    assert only["summary"]["closed_trades"] == 5 and only["summary"]["net_pnl"] == -5
    assert client.get("/api/performance", params={"account": "actual", "mode": "day"}
                      ).json()["summary"]["closed_trades"] == 5
    assert client.get("/api/performance", params={"account": "actual", "strategy": "B"}
                      ).json()["summary"]["net_pnl"] == -5
    first_day = datetime.fromtimestamp(T, timezone.utc).date().isoformat()
    ranged = client.get("/api/performance", params={"account": "actual", "start": first_day,
                                                     "end": first_day}).json()
    assert ranged["summary"]["closed_trades"] == 1
    paper = client.get("/api/performance", params={"account": "paper"}).json()
    assert paper["summary"]["net_pnl"] == 1000 and paper["simulated"] is True
    assert client.get("/api/performance", params={"account": "backtest"}).status_code == 400
    assert client.get("/api/performance", params={"mode": "scalp"}).status_code == 400
    assert client.get("/api/performance", params={"start": "03/01/2026"}).status_code == 400


def test_best_worst_need_enough_trades(client):
    _closed(client, "actual", "AAPL", "A", "day", 5, 0)
    p = client.get("/api/performance").json()
    assert p["best_strategy"] is None and "at least 5" in p["strategy_note"]


def test_options_filter(client):
    _closed(client, "actual", "NVDA", "A", "day", 1, 0, asset="option")
    _closed(client, "actual", "NVDA", "A", "day", 1, 1)
    opt = client.get("/api/performance", params={"asset_type": "option"}).json()
    assert opt["summary"]["closed_trades"] == 1 and opt["summary"]["net_pnl"] == 100


# ------------------------------------------------------------------ saved setups

def test_saved_setups_are_not_trades(client, service):
    snap = service.scanner.run(force=True)
    row = snap["rows"][0]
    saved = client.post("/api/setups/saved", json={"setup_id": row["id"],
                                                   "source": "watchlist", "note": "watch"}).json()
    assert saved["setup_id"] == row["id"] and saved["ticker"] == row["ticker"]
    assert saved["payload"]["stop"] == row["stop"]  # copied from the scanner, not the client
    assert saved["payload"]["validation"]["status"] == row["validation"]["status"]
    again = client.post("/api/setups/saved", json={"setup_id": row["id"], "source": "watchlist"})
    assert again.status_code == 200
    listing = client.get("/api/setups/saved").json()
    assert listing["are_trades"] is False and len(listing["setups"]) == 1
    # Not a trade: no journal entry, no P&L.
    assert client.get("/api/journal/actual").json()["trades"] == []
    assert client.get("/api/journal/paper").json()["trades"] == []
    assert client.get("/api/performance").json()["summary"]["total_trades"] == 0
    assert client.post("/api/setups/saved", json={"setup_id": "nope",
                                                  "source": "watchlist"}).status_code == 400
    assert client.post("/api/setups/saved", json={"setup_id": row["id"],
                                                  "source": "broker"}).status_code == 400
    sid = listing["setups"][0]["id"]
    assert client.delete(f"/api/setups/saved/{sid}").status_code == 200
    assert client.delete(f"/api/setups/saved/{sid}").status_code == 404


def test_universe_setups_can_be_saved(client, service):
    service.universe.run("test")
    row = service.universe.setups()["rows"][0]
    saved = client.post("/api/setups/saved", json={"setup_id": row["id"],
                                                   "source": "universe"}).json()
    assert saved["source"] == "universe" and saved["payload"]["entry_trigger"] == row["entry_trigger"]
