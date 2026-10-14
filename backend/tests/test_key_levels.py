"""Key-level engine tests.

Real data: AAPL daily 2015-2017 and GOOG daily 2004-2013 (tests/fixtures, see README there).
Reference values are computed here by small, deliberately separate implementations written
from the documented rules (brute-force loops, plain recursion), not by calling the engine's
helpers. Intraday rules use hand-built candles whose expected values are worked out by hand.
"""

import csv
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app.data.base import Bar
from app.levels import detect
from app.levels.engine import compute_key_levels
from app.levels.model import (CONFLUENCE_DAILY_ATR, SWING_K, ZONE_JOIN_ATR, ZONE_MAX_SPAN_ATR,
                              ZONE_MIN_REACTIONS, ZONE_MIN_WIDTH_ATR, strength_label)
from app.market.calendar import NY, MarketCalendar

FIX = Path(__file__).parent / "fixtures"
CAL = MarketCalendar()


# ------------------------------------------------------------------- fixtures

def midnight(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=NY).timestamp())


def load(name: str) -> list[Bar]:
    with open(FIX / name) as f:
        return [Bar(midnight(date.fromisoformat(r["date"])), float(r["open"]), float(r["high"]),
                    float(r["low"]), float(r["close"]), float(r["volume"]))
                for r in csv.DictReader(f)]


AAPL = load("aapl_daily_2015_2017.csv")
GOOG = load("goog_daily_2004_2013.csv")


def day_of(b: Bar) -> date:
    return datetime.fromtimestamp(b.time, NY).date()


def weekly(bars: list[Bar]) -> list[Bar]:
    """Reference weekly aggregation (Monday-stamped, like Alpaca)."""
    weeks: dict = {}
    for b in bars:
        weeks.setdefault(day_of(b).isocalendar()[:2], []).append(b)
    out = []
    for g in weeks.values():
        d = day_of(g[0])
        monday = d - timedelta(days=d.weekday())
        out.append(Bar(midnight(monday), g[0].open, max(x.high for x in g),
                       min(x.low for x in g), g[-1].close, sum(x.volume for x in g)))
    return out


def at(y, m, d, hh=17, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=NY)


def run(bars, as_of, mode="swing", **kw):
    frames = {"1Day": bars, "1Week": weekly(bars)}
    frames.update(kw.pop("extra", {}))
    return compute_key_levels("TEST", mode, frames, as_of, CAL, **kw)


def by_type(report, typ, tf=None):
    return [l for l in report["levels"] if l["type"] == typ and (tf is None or l["timeframe"] == tf)]


# ------------------------------------------------- reference implementations

def ref_ema(values, period):
    seed = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    e = seed
    for v in values[period:]:
        e = alpha * v + (1 - alpha) * e
    return e


def ref_atr(bars, period=14):
    trs = [max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
           for p, c in zip(bars, bars[1:])]
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def ref_swings(bars, k):
    """Brute force: compare every neighbour individually."""
    out = []
    for i in range(len(bars)):
        if i - k < 0 or i + k >= len(bars):
            continue
        hi = all(bars[i].high > bars[j].high for j in range(i - k, i)) and \
            all(bars[i].high >= bars[j].high for j in range(i + 1, i + k + 1))
        lo = all(bars[i].low < bars[j].low for j in range(i - k, i)) and \
            all(bars[i].low <= bars[j].low for j in range(i + 1, i + k + 1))
        if hi:
            out.append(("high", i, bars[i].high))
        if lo:
            out.append(("low", i, bars[i].low))
    return out


def ref_zones(bars, k):
    """Zones per the documented rule, written independently of detect.find_zones."""
    a = ref_atr(bars)
    pts = sorted(((p, i) for _, i, p in ref_swings(bars, k)))
    clusters, cur = [], []
    for p, i in pts:
        if cur and p - cur[-1][0] <= ZONE_JOIN_ATR * a and p - cur[0][0] <= ZONE_MAX_SPAN_ATR * a:
            cur.append((p, i))
        else:
            if cur:
                clusters.append(cur)
            cur = [(p, i)]
    if cur:
        clusters.append(cur)
    zones = []
    for c in clusters:
        if len(c) < ZONE_MIN_REACTIONS:
            continue
        lo, hi = c[0][0], c[-1][0]
        if hi - lo < ZONE_MIN_WIDTH_ATR * a:
            mid = (lo + hi) / 2
            lo, hi = mid - ZONE_MIN_WIDTH_ATR * a / 2, mid + ZONE_MIN_WIDTH_ATR * a / 2
        zones.append((round(lo, 6), round(hi, 6), len(c)))
    return zones


# -------------------------------------------------------- real-data checks

def test_fixtures_match_the_nyse_calendar():
    for bars in (AAPL, GOOG):
        days = {day_of(b) for b in bars}
        expected = {sd.day for sd in CAL.trading_days(min(days), max(days))}
        assert days == expected  # no missing sessions, no extra ones


def test_fixture_spot_checks_against_public_record():
    y2015 = [b for b in AAPL if day_of(b).year == 2015]
    assert max(y2015, key=lambda b: b.close).close == pytest.approx(133.00)
    top = max(y2015, key=lambda b: b.high)
    assert (day_of(top), top.high) == (date(2015, 4, 28), pytest.approx(134.54, abs=0.01))
    ipo = GOOG[0]
    assert (day_of(ipo), ipo.high, ipo.low) == (date(2004, 8, 19), 104.06, 95.96)


@pytest.mark.parametrize("as_of,prev_day,prev_week", [
    (at(2016, 3, 16, 12), date(2016, 3, 15), (date(2016, 3, 7), date(2016, 3, 11))),
    # Tuesday after MLK day: previous session is Friday; previous week Jan 11-15.
    (at(2016, 1, 19, 11), date(2016, 1, 15), (date(2016, 1, 11), date(2016, 1, 15))),
    # Week containing the July 3 (observed) holiday has 4 sessions.
    (at(2015, 7, 8, 15), date(2015, 7, 7), (date(2015, 6, 29), date(2015, 7, 2))),
    # After the close the next session becomes "current": PDH is today's high.
    (at(2016, 6, 3, 20, 30), date(2016, 6, 3), (date(2016, 5, 31), date(2016, 6, 3))),
])
def test_previous_day_and_week_levels_match_reference(as_of, prev_day, prev_week):
    r = run(AAPL, as_of)
    rows = {day_of(b): b for b in AAPL}
    assert by_type(r, "PDH")[0]["price"] == pytest.approx(rows[prev_day].high)
    assert by_type(r, "PDL")[0]["price"] == pytest.approx(rows[prev_day].low)
    week = [b for d, b in rows.items() if prev_week[0] <= d <= prev_week[1]]
    assert by_type(r, "PWH")[0]["price"] == pytest.approx(max(b.high for b in week))
    assert by_type(r, "PWL")[0]["price"] == pytest.approx(min(b.low for b in week))
    assert r["previous_session"] == prev_day.isoformat()


@pytest.mark.parametrize("bars,as_of", [(AAPL, at(2016, 9, 30)), (GOOG, at(2012, 6, 29)),
                                        (GOOG, at(2008, 11, 21))])
def test_ema_and_atr_match_reference(bars, as_of):
    r = run(bars, as_of)
    hist = [b for b in bars if b.time < as_of.timestamp()]  # daily candles closed by as_of
    closes = [b.close for b in hist]
    for p in (9, 20, 50, 200):
        lv = by_type(r, f"EMA{p}", "1Day")[0]
        assert lv["price"] == pytest.approx(ref_ema(closes, p), abs=5e-5)  # output: 4 dp
        assert lv["valid"] == (len(closes) >= 3 * p)
    # ATR is reported for the zone lookback window (last 500 daily candles).
    assert r["atr"]["1Day"] == pytest.approx(ref_atr(hist[-500:]), abs=5e-5)


def test_swings_match_bruteforce_reference():
    for bars in (AAPL, GOOG[-600:]):
        k = SWING_K["1Day"]
        got = detect.find_swings(bars, k, "1Day", lambda b: b.time + 86400)
        assert [(s.kind, s.index, s.price) for s in got] == ref_swings(bars, k)
        assert all(s.confirmed_at == bars[s.index + k].time + 86400 for s in got)


def test_zones_match_reference_clustering():
    for bars in (AAPL[:300], AAPL, GOOG[-500:]):
        k = SWING_K["1Day"]
        swings = detect.find_swings(bars, k, "1Day", lambda b: b.time + 86400)
        got = sorted((round(z.low, 6), round(z.high, 6), len(z.pivots))
                     for z in detect.find_zones(bars, swings, "1Day"))
        assert got == sorted(ref_zones(bars, k))


def test_known_2015_aapl_top_is_found_as_resistance():
    r = run(AAPL, at(2015, 8, 3))
    zones = [l for l in by_type(r, "RESISTANCE_ZONE") if l["low"] <= 134.54 <= l["high"] + 1e-9]
    assert zones, "expected a resistance zone around the 2015 highs"
    z = zones[0]
    assert z["low"] == pytest.approx(132.97, abs=0.01) and z["high"] == pytest.approx(134.54, abs=0.01)
    assert sum(z["evidence"]["details"]["reactions_by_timeframe"].values()) >= 3
    assert "1Week" in z["timeframes"] and z["valid"]
    assert z["strength"]["label"] in ("moderate", "strong")


# ----------------------------------------------------------- point in time

@pytest.mark.parametrize("bars,as_of", [(AAPL, at(2015, 8, 3)), (AAPL, at(2016, 5, 12, 11)),
                                        (GOOG, at(2008, 10, 10)), (GOOG, at(2011, 3, 4, 20, 30))])
def test_no_lookahead_future_candles_change_nothing(bars, as_of):
    past = [b for b in bars if day_of(b) <= as_of.date()]  # nothing after as_of's date
    full = run(bars, as_of)
    truncated = run(past, as_of)
    assert json.dumps(full, sort_keys=True) == json.dumps(truncated, sort_keys=True)


def test_swing_is_not_reported_before_it_is_confirmed():
    # 2015-04-28 high 134.54 needs 3 more daily closes: 04-29, 04-30, 05-01 (16:00 ET).
    before = run(AAPL, at(2015, 5, 1, 15, 59))
    after = run(AAPL, at(2015, 5, 1, 16, 0))
    price = lambda r: [l["price"] for l in by_type(r, "SWING_HIGH", "1Day")]  # noqa: E731
    assert pytest.approx(134.54, abs=0.01) not in price(before)
    assert pytest.approx(134.54, abs=0.01) in price(after)


def test_forming_daily_candle_is_ignored():
    # At 11:00 on 2016-05-12 that day's candle has not closed: PDH must come from 05-11.
    r = run(AAPL, at(2016, 5, 12, 11))
    rows = {day_of(b): b for b in AAPL}
    assert by_type(r, "PDH")[0]["price"] == pytest.approx(rows[date(2016, 5, 11)].high)
    assert r["timeframes_used"]["1Day"]["to"].startswith("2016-05-11")


# ------------------------------------------------------------------ splits

def test_unadjusted_split_is_detected_and_history_truncated():
    split = date(2016, 1, 4)
    raw = [Bar(b.time, b.open * 4, b.high * 4, b.low * 4, b.close * 4, b.volume / 4)
           if day_of(b) < split else b for b in AAPL]  # real AAPL with a synthetic 4:1 split
    found = detect.detect_unadjusted_splits(raw)
    assert [(s["date"], s["ratio"]) for s in found] == [("2016-01-04", "4-for-1")]
    assert detect.detect_unadjusted_splits(AAPL) == []
    when = at(2016, 9, 30)
    guarded = run(raw, when)
    assert guarded["warnings"] and "split" in guarded["warnings"][0]
    post_only = run([b for b in AAPL if day_of(b) >= split], when)
    assert [l for l in guarded["levels"]] == [l for l in post_only["levels"]]
    # No level may sit at pre-split (4x) prices.
    assert max(l["high"] for l in guarded["levels"]) < 200


def test_raw_adjustment_warns():
    r = run(AAPL, at(2016, 9, 30), adjustment="raw")
    assert any("unadjusted" in w for w in r["warnings"])


# --------------------------------------------- intraday (hand-built) fixtures

MON, TUE = date(2026, 3, 9), date(2026, 3, 10)


def five_min(day: date, rows) -> list[Bar]:
    t0 = int(datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY).timestamp())
    return [Bar(t0 + i * 300, *r) for i, r in enumerate(rows)]


# Monday: a steady climb. bar i: open 100 + 0.1 i, close open + 0.05, high close + 0.02,
# low open - 0.02. => PDH = 100 + 7.7 + 0.07 = 107.77 (last bar), PDL = 99.98 (first bar).
MON_BARS = five_min(MON, [(100 + 0.1 * i, 100 + 0.1 * i + 0.07, 100 + 0.1 * i - 0.02,
                           100 + 0.1 * i + 0.05, 1000) for i in range(78)])
# Tuesday's opening range (09:30-09:55) => session open 108.0, ORH 108.9, ORL 107.3.
OR_ROWS = [(108.0, 108.5, 107.6, 108.2, 3000), (108.2, 108.9, 108.0, 108.7, 2000),
           (108.7, 108.8, 108.1, 108.3, 1000), (108.3, 108.4, 107.4, 107.5, 2000),
           (107.5, 107.9, 107.3, 107.8, 1000), (107.8, 108.0, 107.6, 107.9, 1000)]
# 10:00 closes 109.6 above ORH, 10:05 confirms at 109.8, 10:10 falls back to 108.5 (fails).
POST_ROWS = [(107.9, 109.7, 107.9, 109.6, 4000), (109.6, 109.9, 109.4, 109.8, 3000),
             (109.8, 109.8, 108.4, 108.5, 3000)]
TUE_BARS = five_min(TUE, OR_ROWS + POST_ROWS)


def daily_history() -> list[Bar]:
    """40 quiet sessions before Monday, with a known previous week (Mar 2-6)."""
    days = CAL.trading_days_back(date(2026, 3, 6), 40)
    out = []
    for i, sd in enumerate(days):
        hi = 101.0 + (0.5 if sd.day == date(2026, 3, 4) else 0)  # PWH 101.5 on Mar 4
        lo = 98.0 - (0.75 if sd.day == date(2026, 3, 3) else 0)  # PWL 97.25 on Mar 3
        out.append(Bar(midnight(sd.day), 99.5, hi, lo, 100.0 + (i % 3) * 0.1, 1e6))
    out.append(Bar(midnight(MON), 100.0, 107.77, 99.98, 107.75, 78000))
    return out


def run_intraday(as_of):
    daily = daily_history()
    frames = {"5Min": MON_BARS + TUE_BARS, "1Day": daily, "1Week": weekly(daily)}
    return compute_key_levels("TEST", "day", frames, as_of, CAL, chart_tf="5Min",
                              chart_bars=MON_BARS + TUE_BARS)


def test_intraday_reference_levels_by_hand():
    r = run_intraday(datetime(2026, 3, 10, 10, 0, tzinfo=NY))
    assert by_type(r, "PDH")[0]["price"] == pytest.approx(107.77)
    assert by_type(r, "PDL")[0]["price"] == pytest.approx(99.98)
    assert "5-minute regular-session candles" in by_type(r, "PDH")[0]["evidence"]["summary"]
    assert by_type(r, "PWH")[0]["price"] == pytest.approx(101.5)
    assert by_type(r, "PWL")[0]["price"] == pytest.approx(97.25)
    assert by_type(r, "SESSION_OPEN")[0]["price"] == pytest.approx(108.0)
    assert by_type(r, "ORH")[0]["price"] == pytest.approx(108.9)
    assert by_type(r, "ORL")[0]["price"] == pytest.approx(107.3)
    # VWAP over the 6 OR candles: sum(tp*v)/sum(v) = 1,080,800 / 10,000 = 108.08
    vwap = by_type(r, "VWAP")[0]
    assert vwap["price"] == pytest.approx(108.08, abs=1e-9) and vwap["valid"]
    assert by_type(r, "PDH")[0]["status"].startswith("traded through")


def test_opening_range_not_reported_until_complete():
    r = run_intraday(datetime(2026, 3, 10, 9, 59, tzinfo=NY))
    assert not by_type(r, "ORH") and not by_type(r, "ORL")
    assert any("Opening range" in n for n in r["notes"])
    assert by_type(r, "SESSION_OPEN")  # the 09:30 candle closed at 09:35


def test_breakout_confirmation_and_failure_timing():
    def orh_breakouts(r):
        return [l for l in by_type(r, "BREAKOUT") if "ORH" in l["label"]]

    # 10:05 candle still forming at 10:09 -> no confirmation yet.
    assert orh_breakouts(run_intraday(datetime(2026, 3, 10, 10, 9, tzinfo=NY))) == []
    confirmed = orh_breakouts(run_intraday(datetime(2026, 3, 10, 10, 10, tzinfo=NY)))
    assert len(confirmed) == 1
    bo = confirmed[0]
    assert bo["price"] == pytest.approx(108.9) and bo["valid"] and bo["status"] == "holding"
    assert bo["formed_at"].startswith("2026-03-10T10:10")
    failed = orh_breakouts(run_intraday(datetime(2026, 3, 10, 10, 15, tzinfo=NY)))[0]
    assert not failed["valid"] and failed["status"].startswith("failed")


def test_liquidity_equal_highs_untaken_then_swept():
    days = CAL.trading_days_back(date(2026, 3, 6), 60)
    bars = []
    for i, sd in enumerate(days):
        base = 100 + 3 * math.sin(i / 4)
        hi = base + 1
        if i in (30, 40):
            hi = 106.0 + (0.02 if i == 40 else 0)  # two equal swing highs at ~106
        bars.append(Bar(midnight(sd.day), base, hi, base - 1, base + 0.2, 1e6))
    r = run(bars, at(2026, 3, 6, 17))
    pools = [l for l in by_type(r, "LIQUIDITY") if "Buy-side" in l["label"]]
    assert pools and pools[0]["low"] == pytest.approx(106.02) and pools[0]["valid"]
    swept = bars + [Bar(midnight(date(2026, 3, 9)), 104, 106.5, 103, 105.0, 2e6)]
    r2 = run(swept, at(2026, 3, 9, 17))
    pool = [l for l in by_type(r2, "LIQUIDITY") if l["low"] == pytest.approx(106.02)][0]
    assert not pool["valid"] and "swept" in pool["status"] and "rejected" in pool["status"]


# ------------------------------------------------------------------ output

REQUIRED_FIELDS = {"ticker", "type", "price", "low", "high", "timeframe", "calculated_at",
                   "evidence", "strength", "valid", "status", "formed_at", "side"}


def test_every_level_has_required_fields_and_transparent_score():
    r = run(AAPL, at(2016, 9, 30))
    tol = CONFLUENCE_DAILY_ATR * r["atr"]["1Day"]
    assert tol > 0
    for l in r["levels"]:
        assert REQUIRED_FIELDS <= set(l)
        assert l["ticker"] == "TEST" and l["low"] <= l["price"] <= l["high"]
        assert l["evidence"]["summary"]
        s = l["strength"]
        assert s["score"] == round(sum(s["components"].values()))
        assert s["label"] == strength_label(s["score"])
        assert "not a probability" in s["note"]
        assert l["calculated_at"].startswith("2016-09-30T17:00")


def test_api_levels_endpoint_demo_day_mode(client):
    r = client.get("/api/levels/SPY", params={"mode": "day", "timeframe": "5Min"})
    assert r.status_code == 200
    j = r.json()
    types = {l["type"] for l in j["levels"]}
    for t in ("PDH", "PDL", "PWH", "PWL", "SESSION_OPEN", "ORH", "ORL", "SWING_HIGH",
              "SWING_LOW", "VWAP", "EMA9", "EMA20", "EMA50", "EMA200"):
        assert t in types, t
    assert types & {"SUPPORT_ZONE", "RESISTANCE_ZONE"}
    assert {"ema9", "ema20", "ema50", "ema200", "vwap", "swings"} <= set(j["indicators"])
    assert j["is_demo"] is True
    replay = client.get("/api/levels/SPY", params={"mode": "swing",
                                                   "as_of": "2026-03-05T12:00:00"}).json()
    assert replay["replay"] is True and replay["calculated_at"].startswith("2026-03-05T12:00")
    assert replay["previous_session"] == "2026-03-04"


def test_analysis_uses_key_levels(client):
    a = client.get("/api/analysis/SPY", params={"mode": "swing"}).json()
    assert a["key_levels"] and a["key_levels_error"] is None


def test_level_ids_are_unique(client):
    for sym in ("SPY", "NVDA", "TSLA"):
        for mode in ("day", "swing"):
            levels = client.get(f"/api/levels/{sym}", params={"mode": mode}).json()["levels"]
            ids = [l["id"] for l in levels]
            assert len(ids) == len(set(ids)), (sym, mode)
