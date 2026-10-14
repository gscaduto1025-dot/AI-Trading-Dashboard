"""Options module: Alpaca parsing, liquidity filters, spreads, data staleness, missing and
inconsistent Greeks, recommendation gates, stock- vs option-level risk, and the API."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.data.base import SubscriptionError
from app.db import Database
from app.main import Service, create_app
from app.market.alpaca_client import AlpacaClient, RateLimiter
from app.market.calendar import NY
from app.options import OPTIONS_FEEDS, AlpacaOptionsSource, OptionsConfig, analyze, parse_occ

from .conftest import demo_market
from .fake_options import FakeOptions

NOW = datetime(2026, 3, 10, 11, 0, tzinfo=NY)  # Tuesday, regular session
TODAY = NOW.date()
CFG = OptionsConfig()


def source(fake: FakeOptions, feed: str = "opra") -> AlpacaOptionsSource:
    client = AlpacaClient("PKTESTKEY", "SECRETVALUE", transport=fake.transport(),
                          rate_limiter=RateLimiter(10_000), max_retries=0,
                          sleep=lambda s: None)
    return AlpacaOptionsSource(client, feed)


def setup_(direction="LONG", status="TRIGGERED"):
    return {"id": "setup-1", "ticker": "SPY", "strategy": "breakout_retest",
            "strategy_name": "Breakout and retest", "direction": direction, "status": status,
            "timeframe": "5Min", "reasons": ["Risk/reward 1.4 below 2.0"]}


def plan_(direction="LONG", stop=98.0, t1=104.0, t2=106.0, entry=100.2):
    if direction == "SHORT":
        stop, t1, t2 = 2 * 100 - stop, 2 * 100 - t1, 2 * 100 - t2
    ref = lambda x: {"price": x} if x is not None else None  # noqa: E731
    return {"entry_trigger": ref(entry), "entry": ref(entry), "stop": ref(stop),
            "target1": ref(t1), "target2": ref(t2), "reward_per_share_t1": 3.8,
            "risk_reward_t1": 2.0, "invalidation": ["A close below 98.00 invalidates."]}


def run(fake=None, *, direction="LONG", status="TRIGGERED", feed="opra", market_open=True,
        underlying=None, now=NOW, expiration=None, cfg=CFG, plan=None, mode="day"):
    fake = fake or FakeOptions(NOW)
    src = source(fake, feed)
    kind = "call" if direction == "LONG" else "put"
    lo, hi = cfg.dte_window[mode]
    e_lo = e_hi = date.fromisoformat(expiration) if expiration else None
    contracts = src.contracts("SPY", TODAY, TODAY + timedelta(days=180), kind, 80, 120)
    chain = src.chain("SPY", e_lo or TODAY + timedelta(days=lo),
                      e_hi or TODAY + timedelta(days=hi), kind, 80, 120)
    return analyze(symbol="SPY", mode=mode, setup=setup_(direction, status),
                   plan=plan or plan_(direction),
                   underlying=underlying or {"price": 100.0, "stale": False, "reason": None},
                   contracts=contracts, chain=chain, feed=OPTIONS_FEEDS[feed],
                   market_open=market_open, now=now, cfg=cfg, expiration=expiration)


def row(res, sym):
    return next(r for r in res["contracts"] if r["symbol"] == sym)


def ids(r):
    return {c["id"] for c in r["checks"]}


def blocks(r):
    return {c["id"] for c in r["checks"] if c["severity"] == "block"}


# ------------------------------------------------------------------ source / parsing

def test_occ_symbol_parsing():
    assert parse_occ("SPY260313C00100000") == {"root": "SPY", "expiration": "2026-03-13",
                                               "type": "call", "strike": 100.0}
    assert parse_occ("AAPL260116P00187500")["strike"] == 187.5
    assert parse_occ("not-an-option") is None


def test_source_paginates_filters_and_never_fills_gaps():
    fake = FakeOptions(NOW, page_size=7)
    sym = fake.symbol(0, 100)
    fake.patch(sym, greeks=None, iv=None, quote={"bp": None})
    src = source(fake)
    con = src.contracts("SPY", TODAY, TODAY + timedelta(days=30), "call", 95, 105)
    ch = src.chain("SPY", TODAY, TODAY + timedelta(days=30), "call", 95, 105)
    # 2 expirations within 30 days x 11 strikes, gathered across 4 pages of 7
    assert len(con["contracts"]) == 2 * 11 and all(c["type"] == "call" for c in con["contracts"])
    assert len(ch["snapshots"]) == 22
    s = ch["snapshots"][sym]
    assert s["bid"] is None and s["greeks"] is None and s["iv"] is None  # nothing invented
    assert s["ask"] is not None and s["quote_time"] is not None
    c = next(c for c in con["contracts"] if c["symbol"] == sym)
    assert (c["open_interest"], c["multiplier"], c["strike"]) == (2500, 100, 100.0)
    chain_req = [r for r in fake.requests if "snapshots" in r.url.path][0]
    q = dict(chain_req.url.params)
    assert q["feed"] == "opra" and q["type"] == "call" and q["limit"] == "1000"
    assert q["strike_price_gte"] == "95" and q["expiration_date_lte"] == "2026-04-09"
    con_req = [r for r in fake.requests if r.url.path == "/v2/options/contracts"][0]
    assert dict(con_req.url.params)["underlying_symbols"] == "SPY"
    n = len(fake.requests)
    src.chain("SPY", TODAY, TODAY + timedelta(days=30), "call", 95, 105)
    assert len(fake.requests) == n  # cached: no extra API call within the TTL


def test_opra_without_subscription_is_reported_not_hidden():
    fake = FakeOptions(NOW)
    fake.status["/v1beta1/options"] = 403
    with pytest.raises(SubscriptionError, match="403"):
        source(fake).chain("SPY", TODAY, TODAY + timedelta(days=10))


# ------------------------------------------------------------------ spreads

def test_spread_metrics_use_actual_quotes():
    fake = FakeOptions(NOW)
    sym = fake.symbol(1, 100)
    fake.patch(sym, quote={"bp": 2.00, "ap": 2.10})
    r = row(run(fake), sym)
    assert (r["mid"], r["spread"], r["spread_pct"]) == (2.05, 0.1, 4.88)
    assert r["breakeven"] == 102.1  # strike + ask, not derived from the stock price
    assert r["premium_per_contract"] == 210.0 and r["spread_cost_per_contract"] == 10.0
    assert "wide_spread" not in ids(r)


def test_wide_spread_is_flagged_and_blocks():
    fake = FakeOptions(NOW)
    sym = fake.symbol(1, 100)
    fake.patch(sym, quote={"bp": 1.80, "ap": 2.40})
    r = row(run(fake), sym)
    assert "wide_spread" in blocks(r) and not r["eligible"]
    detail = next(c["detail"] for c in r["checks"] if c["id"] == "wide_spread")
    assert "Very wide" in detail and "28.6%" in detail


def test_penny_spread_floor_crossed_and_zero_bid():
    fake = FakeOptions(NOW)
    a, b, c = fake.symbol(1, 104), fake.symbol(1, 103), fake.symbol(1, 102)
    fake.patch(a, quote={"bp": 0.40, "ap": 0.44})  # 9.5% but only 0.04: never "wide"
    fake.patch(b, quote={"bp": 1.50, "ap": 1.40})
    fake.patch(c, quote={"bp": 0.0, "ap": 0.30})
    res = run(fake)
    assert "wide_spread" not in ids(row(res, a))
    assert "crossed_quote" in blocks(row(res, b)) and row(res, b)["mid"] is None
    assert "zero_bid" in blocks(row(res, c))


def test_missing_quote_produces_no_prices_at_all():
    fake = FakeOptions(NOW)
    sym = fake.symbol(1, 101)
    fake.patch(sym, quote={"bp": None, "ap": None})
    r = row(run(fake), sym)
    assert "no_quote" in blocks(r)
    assert r["mid"] is r["spread"] is r["breakeven"] is r["premium_per_contract"] is None
    assert r["sensitivity"]["at_stock_target1"] is None


# ------------------------------------------------------------------ liquidity filters

def test_volume_and_open_interest_minimums():
    fake = FakeOptions(NOW)
    lowv, lowoi, nooi, oldoi = (fake.symbol(1, k) for k in (99, 100, 101, 102))
    fake.patch(lowv, bar={"v": 40})
    fake.contract_rows[lowoi]["open_interest"] = "120"
    fake.contract_rows[nooi]["open_interest"] = None
    fake.contract_rows[oldoi]["open_interest_date"] = "2026-02-20"
    res = run(fake)
    assert "low_volume" in blocks(row(res, lowv))
    assert "low_open_interest" in blocks(row(res, lowoi))
    assert "low_open_interest" in blocks(row(res, nooi)) and row(res, nooi)["open_interest"] is None
    old = row(res, oldoi)
    assert "stale_open_interest" in ids(old) and "stale_open_interest" not in blocks(old)


def test_volume_from_a_previous_session_is_not_todays_activity():
    fake = FakeOptions(NOW)
    sym = fake.symbol(1, 100)
    fake.patch(sym, bar={"t": "2026-03-09T08:00:00Z", "v": 5000})
    r = row(run(fake), sym)
    assert "low_volume" in blocks(r)
    assert "previous session 5000" in next(c["detail"] for c in r["checks"]
                                           if c["id"] == "low_volume")


def test_swing_thresholds_differ_from_day():
    fake = FakeOptions(NOW)
    sym = fake.symbol(2, 100)  # 31 DTE
    fake.patch(sym, bar={"v": 60})
    day = run(fake, mode="day", expiration=fake.expirations[2].isoformat())
    swing = run(fake, mode="swing")
    assert "low_volume" in blocks(row(day, sym)) and "outside_dte_window" in blocks(row(day, sym))
    assert "low_volume" not in ids(row(swing, sym))


# ------------------------------------------------------------------ staleness

def test_stale_and_untimestamped_quotes_block():
    fake = FakeOptions(NOW)
    old, nots = fake.symbol(1, 100), fake.symbol(1, 101)
    fake.patch(old, quote={"t": "2026-03-10T14:30:00Z"})  # 30 minutes before NOW
    fake.patch(nots, quote={"t": None})
    res = run(fake)
    assert "stale_quote" in blocks(row(res, old)) and row(res, old)["quote_age_seconds"] == 1800
    assert "stale_quote" in blocks(row(res, nots))


def test_market_closed_never_recommends():
    res = run(market_open=False)
    assert res["recommendation"]["contract"] is None
    assert any("market is closed" in r for r in res["recommendation"]["reasons"])


def test_stale_underlying_never_recommends():
    res = run(underlying={"price": 100.0, "stale": True, "reason": "last trade 40 min old"})
    assert res["recommendation"]["contract"] is None
    assert any("stale" in r for r in res["recommendation"]["reasons"])


# ------------------------------------------------------------------ missing / bad Greeks

def test_missing_greeks_block_and_are_not_estimated():
    fake = FakeOptions(NOW)
    none, partial, noiv = fake.symbol(1, 100), fake.symbol(1, 101), fake.symbol(1, 99)
    fake.patch(none, greeks=None)
    fake.snapshots[partial]["greeks"] = {"delta": 0.45, "gamma": 0.06, "rho": 0.01,
                                         "vega": 0.09}  # theta absent
    fake.patch(noiv, iv=None)
    res = run(fake)
    r = row(res, none)
    assert "missing_greeks" in blocks(r) and r["delta"] is None
    assert r["sensitivity"]["per_1_usd_move"] is None
    assert r["sensitivity"]["at_stock_stop"] is None and r["score"] is None
    p = row(res, partial)
    assert "missing_greeks" in blocks(p) and "theta" in next(
        c["detail"] for c in p["checks"] if c["id"] == "missing_greeks")
    assert p["sensitivity"]["theta_per_day_per_contract"] is None
    assert "missing_iv" in blocks(row(res, noiv))
    assert any("no Greeks" in x for x in res["data_limitations"])


def test_inconsistent_greeks_block():
    fake = FakeOptions(NOW)
    sym = fake.symbol(1, 100)
    fake.patch(sym, greeks={"delta": -0.4, "gamma": -0.01, "theta": -0.04, "vega": 0.09,
                            "rho": 0.0})
    assert "inconsistent_greeks" in blocks(row(run(fake), sym))


def test_missing_snapshot_is_listed_without_invented_data():
    fake = FakeOptions(NOW)
    sym = fake.symbol(1, 100)
    fake.patch(sym, drop=True)
    res = run(fake)
    r = row(res, sym)
    assert not r["has_data"] and r["bid"] is r["ask"] is r["iv"] is r["delta"] is None
    assert any("had no snapshot" in x for x in res["data_limitations"])


# ------------------------------------------------------------------ selection and gates

def test_long_setups_use_calls_and_short_setups_use_puts():
    long_ = run(direction="LONG")
    short = run(direction="SHORT")
    assert long_["option_type"] == "call" and {r["type"] for r in long_["contracts"]} == {"call"}
    assert short["option_type"] == "put" and {r["type"] for r in short["contracts"]} == {"put"}
    assert short["recommendation"]["contract"]["delta"] < 0


def test_candidate_only_when_every_check_passes():
    res = run()
    top = res["recommendation"]["contract"]
    assert top is not None and top["eligible"] and not blocks(top)
    assert top == res["candidates"][0]
    scores = [c["score"]["total"] for c in res["candidates"]]
    assert scores == sorted(scores, reverse=True) and all(0 <= s <= 100 for s in scores)
    lo, hi = CFG.delta_band["day"]
    assert lo <= abs(top["delta"]) <= hi and 0 <= top["dte"] <= 14
    assert "not advice" in res["recommendation"]["label"]


def test_indicative_feed_is_explorable_but_never_recommended():
    res = run(feed="indicative")
    assert res["candidates"] and res["recommendation"]["contract"] is None
    assert any("not actual OPRA quotes" in r for r in res["recommendation"]["reasons"])
    assert "Algo Trader Plus" in " ".join(res["recommendation"]["reasons"])


def test_non_qualifying_setup_is_never_recommended():
    res = run(status="NO_TRADE")
    assert res["setup"]["qualifies"] is False and res["recommendation"]["contract"] is None
    assert any("NO_TRADE" in r for r in res["recommendation"]["reasons"])


def test_no_recommendation_when_liquidity_is_insufficient_everywhere():
    fake = FakeOptions(NOW)
    for sym in fake.snapshots:
        fake.patch(sym, bar={"v": 3})
    res = run(fake)
    assert res["eligible_count"] == 0 and res["recommendation"]["contract"] is None
    assert res["blocked_by"]["low_volume"] == res["contracts_total"]


def test_dte_window_delta_band_and_expirations():
    res = run()
    exps = {e["date"]: e for e in res["expirations"]}
    assert [e["dte"] for e in res["expirations"]] == [3, 10, 31, 59]
    assert exps["2026-03-20"]["monthly"] and exps["2026-03-13"]["short_dated"]
    assert [e["in_window"] for e in res["expirations"]] == [True, True, False, False]
    deep = row(res, "SPY260320C00094000")  # delta 0.98
    assert "delta_out_of_band" in blocks(deep)
    picked = run(expiration="2026-04-10")
    assert {r["expiration"] for r in picked["contracts"]} == {"2026-04-10"}
    assert all("outside_dte_window" in blocks(r) for r in picked["contracts"])


def test_short_dated_and_0dte_warnings():
    fake = FakeOptions(NOW, expirations=[TODAY, TODAY + timedelta(days=3)])
    res = run(fake)
    assert "expires_today" in ids(row(res, fake.symbol(0, 100)))
    assert "short_dated" in ids(row(res, fake.symbol(1, 100)))
    assert any("0DTE" in n for n in res["risk_notes"]["short_dated"])


# ------------------------------------------------------------------ stock vs option risk

def test_separate_stock_and_option_risk_and_stop_is_not_a_loss_limit():
    res = run()
    stock, opt, top = res["stock_risk"], res["option_risk"], res["recommendation"]["contract"]
    assert (stock["stop"], stock["target1"], stock["risk_per_share"]) == (98.0, 104.0, 2.2)
    assert opt["max_loss_per_contract"] == top["premium_per_contract"] == top["ask"] * 100
    est = top["sensitivity"]["at_stock_stop"]
    ds = 98.0 - 100.2  # the plan's risk distance (entry -> stop), applied from now
    mid = top["mid"]
    # small move: delta stays inside [0, 1], so the capped integral equals delta+gamma Taylor
    expect = max(0.0, mid + top["delta"] * ds + 0.5 * top["gamma"] * ds * ds)
    assert est["move"] == -2.2 and abs(est["value"] - expect) < 0.011
    assert est["pnl_from_ask_per_contract"] < 0 and est["reliability"] == "moderate"
    assert -est["pnl_from_ask_per_contract"] < opt["max_loss_per_contract"]
    assert any("does not limit the option's loss" in n for n in res["risk_notes"]["stop_vs_option"])
    assert any("full premium" in n for n in res["risk_notes"]["stop_vs_option"])
    assert top["sensitivity"]["per_1_usd_move"] == round(top["delta"] * 100, 2)
    assert top["sensitivity"]["theta_per_day_per_contract"] == -4.0


def test_large_move_estimates_are_capped_and_marked_low_reliability():
    # A +30 move on a 100 stock: uncapped delta+gamma would add 0.5*0.06*900 = 27 per share.
    res = run(plan=plan_(t1=130.0, t2=140.0))
    top = res["candidates"][0]
    est = top["sensitivity"]["at_stock_target1"]
    ds = 130.0 - 100.2
    assert est["reliability"] == "low"
    assert est["value"] - top["mid"] <= ds + 1e-9  # never gains more than the stock moved
    assert est["value"] >= max(0.0, 100.0 + ds - top["strike"]) - 0.01  # >= intrinsic
    assert top["sensitivity"]["estimated_option_rr"] is None  # no ratio from shaky estimates
    assert top["score"]["points"]["payoff"] == 0


def test_watching_setup_below_entry_uses_plan_distances():
    # Price 100, entry trigger 103 not reached yet, stop 101.5 ABOVE the current price.
    res = run(status="WATCHING", plan=plan_(stop=101.5, t1=108.0, t2=110.0, entry=103.0))
    est = res["candidates"][0]["sensitivity"]["at_stock_stop"]
    assert est["move"] == -1.5 and est["pnl_from_ask_per_contract"] < 0  # a loss, not a gain


def test_breakeven_beyond_target_warning():
    res = run(plan=plan_(t1=100.5, t2=101.0))
    assert any("breakeven_beyond_target" in ids(r) for r in res["contracts"])


def test_no_setup_returns_no_contracts():
    res = analyze(symbol="SPY", mode="day", setup=None, plan=None,
                  underlying={"price": 100.0, "stale": False}, contracts={}, chain={},
                  feed=OPTIONS_FEEDS["opra"], market_open=True, now=NOW, cfg=CFG)
    assert res["contracts"] == [] and res["recommendation"]["contract"] is None


def test_config_validation():
    with pytest.raises(ValueError):
        OptionsConfig().with_overrides({"weights": {"spread": 50}})
    with pytest.raises(ValueError):
        OptionsConfig().with_overrides({"delta_band": {"day": [0.8, 0.2]}})
    with pytest.raises(ValueError):
        OptionsConfig().with_overrides({"nope": 1})
    assert OptionsConfig().with_overrides({"min_volume": {"day": 10}}).min_volume["swing"] == 25


# ------------------------------------------------------------------ API

def test_demo_mode_never_simulates_options(client):
    j = client.get("/api/options/SPY", params={"mode": "day"}).json()
    assert j["available"] is False and "OPTIONS DATA UNAVAILABLE" in j["reason"]
    assert "contracts" not in j
    cap = client.get("/api/options/capabilities").json()
    assert cap["configured"] is False and cap["order_execution"] is False
    assert set(cap["feeds"]) == {"indicative", "opra"}
    assert client.get("/api/options/SPY", params={"mode": "scalp"}).status_code == 400
    assert client.get("/api/options/SPY", params={"expiration": "13/03"}).status_code == 400


def test_api_with_alpaca_options(tmp_path):
    fake = FakeOptions(NOW, price=500.0, strikes=[500 + d for d in range(-30, 31, 2)])
    st = Settings(database_path=tmp_path / "o.db", scan_interval_seconds=0)
    svc = Service(st, market=demo_market(), db=Database(st.database_path),
                  options=source(fake, "indicative"))
    with TestClient(create_app(svc, run_scanner=False)) as c:
        j = c.get("/api/options/SPY", params={"mode": "swing"}).json()
        assert j["available"] is True and j["option_type"] in ("call", "put")
        assert j["setup"]["id"] and j["stock_risk"]["stop"] is not None
        assert j["recommendation"]["contract"] is None  # indicative feed + market closed
        assert j["feed"]["id"] == "indicative" and j["order_execution"] is False
        assert c.get("/api/options/capabilities").json()["configured"] is True
        assert c.get("/api/options/config").json()["min_volume"] == {"day": 100, "swing": 25}
        fake.status["/v2/options"] = 403
        svc.options._cache.clear()
        err = c.get("/api/options/SPY", params={"mode": "swing"}).json()
        assert err["available"] is False and err["code"] == "SUBSCRIPTION_NOT_PERMITTED"
        paths = [r.path for r in c.app.routes]
        assert not any("order" in p for p in paths)  # analysis only: no execution routes
