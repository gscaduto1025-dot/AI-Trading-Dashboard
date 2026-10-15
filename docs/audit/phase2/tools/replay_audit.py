"""Phase 2 audit: real-data replay checks through the live code path (Service ->
MarketDataService -> strategies/swing -> planning).

1. Look-ahead: every evaluation at time T is run twice, once with the real candles that
   end after T and once with those candles replaced by absurd values. Results must be
   byte-identical.
2. Plan invariants on every setup: stop/target sides, R:R arithmetic, quality range,
   TRIGGERED setups meet the planning minimum R:R, NO_TRADE has reasons.
3. Candle timestamps from Alpaca: strictly increasing, no duplicates, grid-aligned.
4. Coverage: status counts per strategy.

All evaluation times are before 2026-05-01 (the sealed validation period is untouched).
Read-only Alpaca market data; no orders.
"""

from __future__ import annotations

import collections
import json
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[4] / "backend"))

from app.config import load_settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.main import Service, build_market  # noqa: E402
from app.market.service import MarketDataService  # noqa: E402

NY = ZoneInfo("America/New_York")
SEALED = datetime(2026, 5, 1, tzinfo=NY)
OUT = sys.argv[1]

TF_SECONDS = {"1Min": 60, "5Min": 300, "15Min": 900, "30Min": 1800, "1Hour": 3600,
              "2Hour": 7200, "4Hour": 14400}


CAL = None  # exchange calendar (session closes, early closes, holidays), set in main()


def bar_end(b, tf: str) -> int:
    """When a candle is final. Intraday: start + length. Daily: that session's actual close
    (early closes included). Weekly: the close of the week's last trading session."""
    if tf in TF_SECONDS:
        return b.time + TF_SECONDS[tf]
    d = datetime.fromtimestamp(b.time, NY)
    if tf == "1Day":
        sd = CAL.day(d.date())
        return int((sd.close if sd else d.replace(hour=16, minute=0, second=0)).timestamp())
    if tf == "1Week":
        days = [d.date() + timedelta(days=k) for k in range(7)]
        last = [x for x in days if CAL.is_trading_day(x)][-1]
        return int(CAL.day(last).close.timestamp())
    if tf == "1Month":
        return int((d + timedelta(days=31)).timestamp())
    raise ValueError(tf)


class CachingSource:
    """Shares one download between the real and the poisoned run; records every series."""

    def __init__(self, inner):
        self.inner = inner
        self.cache: dict = {}
        self.calls = 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def fetch_bars(self, symbol, timeframe, start, end, max_items=50_000, feed=None):
        key = (symbol, timeframe, start.isoformat(), end.isoformat(), max_items, feed)
        if key not in self.cache:
            self.calls += 1
            kw = {"feed": feed} if feed else {}
            self.cache[key] = self.inner.fetch_bars(symbol, timeframe, start, end, max_items, **kw)
        return list(self.cache[key])


class PoisonSource:
    """Returns the same past, but every candle that is not complete at T is absurd."""

    def __init__(self, inner: CachingSource, cutoff_ts: int):
        self.inner = inner
        self.cutoff = cutoff_ts
        self.poisoned = 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def fetch_bars(self, symbol, timeframe, start, end, max_items=50_000, feed=None):
        out = []
        for b in self.inner.fetch_bars(symbol, timeframe, start, end, max_items, feed):
            if bar_end(b, timeframe) > self.cutoff:
                self.poisoned += 1
                b = replace(b, open=b.open * 3, high=b.high * 3.5, low=b.low * 0.2,
                            close=b.close * 0.3, volume=b.volume * 50 + 1)
            out.append(b)
        return out


def service(source, db_path, now) -> Service:
    s = load_settings()
    base = build_market(s)
    market = MarketDataService(source, symbols=s.watchlist, now_fn=lambda: now)
    del base
    return Service(s, market=market, db=Database(db_path), with_universe=False)


def evaluate(svc: Service, as_of: datetime) -> tuple[list[dict], list[dict], dict]:
    day = svc.strategy_setups(as_of=as_of, record=False)
    swing = svc.swing_setups(as_of=as_of, record=False)
    return day["setups"], swing["setups"], {"day": day.get("symbols"), "swing": swing.get("symbols")}


def canon(x) -> str:
    return json.dumps(x, sort_keys=True, default=str)


def plan_checks(s: dict, min_rr: float) -> list[str]:
    errs = []
    p = s.get("plan") or {}
    st = s["status"]
    if st == "NO_TRADE" and not (s.get("reasons") or p.get("checks")):
        errs.append("NO_TRADE without reasons")
    px = lambda v: v.get("price") if isinstance(v, dict) else v  # noqa: E731
    entry, stop, t1, t2 = (px(p.get(k)) for k in ("entry", "stop", "target1", "target2"))
    # summary fields on the setup must equal the plan
    for k, v in (("entry", entry), ("stop", stop), ("target1", t1), ("target2", t2)):
        if s.get(k) is not None and v is not None and abs(s[k] - v) > 1e-6:
            errs.append(f"setup.{k} {s[k]} != plan {v}")
    if None in (entry, stop, t1):
        if st == "TRIGGERED":
            errs.append("TRIGGERED without complete prices")
        return errs
    sgn = 1 if s["direction"] == "LONG" else -1
    if not sgn * (entry - stop) > 0:
        errs.append(f"stop on wrong side: entry {entry} stop {stop}")
    if not sgn * (t1 - entry) > 0:
        errs.append(f"target1 on wrong side: entry {entry} t1 {t1}")
    if t2 is not None and not sgn * (t2 - t1) >= 0:
        errs.append(f"target2 before target1: t1 {t1} t2 {t2}")
    rr = p.get("risk_reward_t1")
    if rr is not None and sgn * (entry - stop) > 0:
        calc = (sgn * (t1 - entry)) / (sgn * (entry - stop))
        if abs(calc - rr) > 0.011 + 0.006 * calc:
            errs.append(f"R:R {rr} != computed {calc:.3f}")
    if sgn * (entry - stop) > 0:
        risk, rew1 = sgn * (entry - stop), sgn * (t1 - entry)
        if p.get("risk_per_share") is not None and abs(p["risk_per_share"] - risk) > 1e-3:
            errs.append(f"risk_per_share {p['risk_per_share']} != {risk:.4f}")
        if p.get("reward_per_share_t1") is not None and abs(p["reward_per_share_t1"] - rew1) > 1e-3:
            errs.append(f"reward_per_share_t1 {p['reward_per_share_t1']} != {rew1:.4f}")
        rr2 = p.get("risk_reward_t2")
        if rr2 is not None and t2 is not None:
            c2 = sgn * (t2 - entry) / risk
            if abs(c2 - rr2) > 0.011 + 0.006 * c2:
                errs.append(f"R:R T2 {rr2} != computed {c2:.3f}")
        wf = p.get("risk_reward_worst_fill")
        if wf is not None and rr is not None and wf > rr + 1e-9:
            errs.append(f"worst-fill R:R {wf} > planned R:R {rr}")
    q = (p.get("quality") or {}).get("score") if isinstance(p.get("quality"), dict) else p.get("quality")
    if q is not None and not 0 <= q <= 100:
        errs.append(f"quality {q} outside 0-100")
    need = p.get("min_rr") if p.get("min_rr") is not None else min_rr
    if st == "TRIGGERED" and rr is not None and rr < need - 1e-9:
        errs.append(f"TRIGGERED with R:R {rr} < minimum {need}")
    return errs


def timestamp_checks(cache: dict) -> dict:
    res = {"series": 0, "bars": 0, "unsorted": 0, "duplicates": 0, "misaligned": 0}
    for (sym, tf, *_), bars in cache.items():
        res["series"] += 1
        res["bars"] += len(bars)
        ts = [b.time for b in bars]
        res["unsorted"] += sum(1 for a, b in zip(ts, ts[1:]) if b < a)
        res["duplicates"] += len(ts) - len(set(ts))
        if tf in TF_SECONDS and TF_SECONDS[tf] <= 3600:
            res["misaligned"] += sum(1 for t in ts if t % TF_SECONDS[tf])
    return res


# Evaluation moments (New York time), all before the sealed period. Conditions noted.
MOMENTS = [
    (datetime(2025, 3, 12, 10, 47, tzinfo=NY), "normal session, morning"),
    (datetime(2025, 6, 18, 13, 22, tzinfo=NY), "normal session, FOMC day"),
    (datetime(2025, 9, 23, 15, 5, tzinfo=NY), "normal session, late"),
    (datetime(2025, 4, 7, 10, 15, tzinfo=NY), "high volatility (tariff sell-off)"),
    (datetime(2025, 4, 9, 14, 0, tzinfo=NY), "high volatility (+9.5% rally day)"),
    (datetime(2024, 8, 5, 11, 30, tzinfo=NY), "high volatility (Aug-2024 VIX spike)"),
    (datetime(2024, 11, 29, 12, 15, tzinfo=NY), "low volume, early close 13:00"),
    (datetime(2024, 12, 24, 11, 40, tzinfo=NY), "low volume, Christmas Eve early close"),
    (datetime(2025, 1, 10, 9, 10, tzinfo=NY), "pre-market (before the open)"),
    (datetime(2025, 7, 3, 16, 30, tzinfo=NY), "after an early close"),
    (datetime(2025, 12, 26, 13, 5, tzinfo=NY), "low volume, day after Christmas"),
    (datetime(2026, 2, 17, 11, 0, tzinfo=NY), "normal session, recent"),
]


def main() -> None:
    settings = load_settings()
    assert settings.alpaca_enabled, "needs Alpaca keys"
    real = build_market(settings).source
    min_rr = None
    report = {"moments": [], "plan_errors": [], "coverage": {}, "lookahead_failures": []}
    coverage: dict = collections.defaultdict(collections.Counter)
    now = datetime.now(NY)
    for as_of, label in MOMENTS:
        assert as_of < SEALED
        t0 = time.time()
        cache = CachingSource(real)
        a = service(cache, ":memory:", now)
        global CAL
        CAL = a.market.calendar
        if min_rr is None:
            min_rr = a.planning_config.min_rr
        day_a, swing_a, sym_a = evaluate(a, as_of)
        poison = PoisonSource(cache, int(as_of.timestamp()))
        b = service(poison, ":memory:", now)
        day_b, swing_b, sym_b = evaluate(b, as_of)
        same = canon([day_a, swing_a]) == canon([day_b, swing_b])
        if not same:
            diff = [x["id"] for x, y in zip(day_a + swing_a, day_b + swing_b) if canon(x) != canon(y)]
            report["lookahead_failures"].append({"as_of": as_of.isoformat(), "label": label,
                                                 "differing": diff[:10],
                                                 "counts": [len(day_a), len(day_b), len(swing_a), len(swing_b)]})
        n_err = 0
        for mode, setups in (("day", day_a), ("swing", swing_a)):
            for s in setups:
                coverage[f"{mode}:{s['strategy']}"][s["status"]] += 1
                for e in plan_checks(s, min_rr):
                    n_err += 1
                    report["plan_errors"].append({"as_of": as_of.isoformat(), "id": s["id"],
                                                  "ticker": s["ticker"], "strategy": s["strategy"],
                                                  "status": s["status"], "error": e})
        gates = collections.Counter(r for x in day_a if x["status"] == "NO_TRADE"
                                    for r in (x.get("reasons") or [])[:1])
        m = {"as_of": as_of.isoformat(), "label": label,
             "day_setups": len(day_a), "swing_setups": len(swing_a),
             "day_status": dict(collections.Counter(x["status"] for x in day_a)),
             "swing_status": dict(collections.Counter(x["status"] for x in swing_a)),
             "future_candles_poisoned": poison.poisoned, "identical_with_poisoned_future": same,
             "plan_errors": n_err, "api_series": cache.calls,
             "timestamps": timestamp_checks(cache.cache),
             "top_day_no_trade_reasons": gates.most_common(3),
             "seconds": round(time.time() - t0, 1)}
        report["moments"].append(m)
        print(json.dumps(m, default=str), flush=True)
    report["coverage"] = {k: dict(v) for k, v in sorted(coverage.items())}
    report["min_rr"] = min_rr
    with open(OUT, "w") as f:
        json.dump(report, f, indent=1, default=str)
    print("coverage", json.dumps(report["coverage"]))
    print("lookahead failures", len(report["lookahead_failures"]), "plan errors", len(report["plan_errors"]))


if __name__ == "__main__":
    main()
