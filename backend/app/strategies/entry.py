"""Current entry eligibility of a TRIGGERED setup: one set of rules for the live scanner
and for backtests.

A trigger is a historical fact (the pattern completed on a closed candle). Whether it is a
fresh entry at a given moment and price is a separate question answered here, identically
for the live scanner (latest trade, wall clock) and for backtests (the replayed decision
time and the execution candle's open), so a backtest never takes a trade that the live
scanner would not have shown as CURRENTLY_ACTIONABLE with the same information.
"""

from __future__ import annotations

from datetime import datetime

from ..market.calendar import NY, MarketCalendar
from ..market.timeframes import spec
from .volume import (FULL, LIMITED, NOT_REQUIRED, UNAVAILABLE, UNVERIFIED,
                     volume_confirmation)

# Checks that can never pass again once failed (the entry is gone for good).
PERMANENT = ("Entry window open", "Trigger at most", "Stop not reached", "T1 not reached",
             "Not beyond the invalidation level")


def _num(plan: dict | None, k: str):
    return plan[k]["price"] if plan and plan.get(k) else None


def permanently_failed(checks: list[dict]) -> list[str]:
    return [c["name"] for c in checks if not c["passed"]
            and c["name"].startswith(PERMANENT)]


CURRENTLY_ACTIONABLE, HISTORICALLY_TRIGGERED = "CURRENTLY_ACTIONABLE", "HISTORICALLY_TRIGGERED"


def entry_env_at(now: datetime, cal: MarketCalendar, gates, planning) -> dict:
    """Facts about the moment `now` that decide whether a trigger is still a fresh entry:
    whether the day-trading entry window is open, and the configured entry age limits.
    Pure: the live scanner passes the wall clock, backtests the replayed decision time."""
    g = gates
    sess = cal.last_session_started(now, "regular")
    t = now.timestamp()
    open_from = sess.open.timestamp() + g.entry_start_minutes * 60
    open_until = sess.close.timestamp() - g.stop_entries_minutes_before_close * 60
    if sess.open.timestamp() <= t < sess.close.timestamp():
        window = open_from <= t < open_until
        detail = (f"regular session, entries allowed {datetime.fromtimestamp(open_from, NY):%H:%M}"
                  f"-{datetime.fromtimestamp(open_until, NY):%H:%M} ET")
    else:
        window, detail = False, "regular session closed (day trades are flat at the close)"
    return {"now": int(t), "day_window": window, "day_window_detail": detail,
            "max_age": dict(planning.max_entry_age_candles)}


def _check(name: str, passed: bool, detail: str) -> dict:
    return {"name": name, "passed": passed, "detail": detail}


def entry_checks(s: dict, mode: str, live: dict | None, env: dict | None) -> list[dict]:
    """Recheck a TRIGGERED setup's entry conditions *now*. A trigger was confirmed on a
    completed candle, possibly long ago; it is a fresh entry only if every check passes:
      * the entry window is open (day trades: regular session, inside the strategy's
        entry window);
      * the trigger is recent: confirmed at most max_entry_age_candles candles of its
        timeframe ago;
      * the latest trade is inside the plan's entry zone (beyond the trigger level, not
        chasing past the zone) - a favourable reward/risk elsewhere is not an entry;
      * the stop (incl. today's range for swing setups), the invalidation level and T1 have
        not been reached, and reward/risk from the live price meets the minimum;
      * volume-dependent confirmation is not UNVERIFIED or UNAVAILABLE (IEX proxy).
    Returns every check with its result, so the reasons are visible either way."""
    plan = s.get("plan") or {}
    env = env or {}
    tf = s["timeframe"]
    sg = 1 if s["direction"] == "LONG" else -1
    out = []
    if mode == "day":
        out.append(_check("Entry window open", bool(env.get("day_window")),
                          env.get("day_window_detail") or "session state unknown"))
    max_age = (env.get("max_age") or {}).get(tf)
    conf = s.get("confirmed_at")
    if max_age is not None and conf:
        if spec(tf).intraday:
            step = spec(tf).minutes * 60
            age = max(0, (env.get("now", conf) - conf)) / step
            out.append(_check(f"Trigger at most {max_age} {tf} candles old", age < max_age,
                              f"confirmed {datetime.fromtimestamp(conf, NY):%Y-%m-%d %H:%M} ET, "
                              f"{age:.1f} candles ago"))
        else:
            bars = (s.get("outcome") or {}).get("bars_after_trigger")
            ok = bars is not None and bars < max_age
            out.append(_check(f"Trigger at most {max_age} completed {tf} candles old", ok,
                              f"{bars} completed {tf} candle(s) since the trigger"
                              if bars is not None else "age unknown (target already reached)"))
    vc = s.get("volume_confirmation") or volume_confirmation(s.get("confirmation"))
    out.append(_check("Volume confirmation verified",
                      vc in (FULL, LIMITED, NOT_REQUIRED),
                      {FULL: "consolidated volume", LIMITED: "LIMITED: IEX-only volume accepted "
                       "by partial_volume_policy=limited", NOT_REQUIRED: "signal does not depend "
                       "on volume", UNVERIFIED: "UNVERIFIED: only IEX volume is available for the "
                       "signal candle (consolidated volume arrives 15 minutes later)",
                       UNAVAILABLE: "UNAVAILABLE: the feed reported no volume"}.get(vc, vc)))
    stop, t1 = _num(plan, "stop"), _num(plan, "target1")
    zone = plan.get("entry_zone")
    if live is None:
        out.append(_check("Fresh live price", False, "no fresh last trade to recheck against"))
        return out
    price = live["price"]
    if zone:
        lo, hi = zone["low"], zone["high"]
        inside = lo - 1e-9 <= price <= hi + 1e-9
        where = ("inside" if inside else
                 ("back through the trigger" if sg * (price - (lo if sg > 0 else hi)) < 0
                  else "beyond the zone (chasing)"))
        out.append(_check("Live price inside the entry zone", inside,
                          f"{price:.2f} vs zone {lo:.2f}-{hi:.2f}: {where}"))
    if stop is not None:
        hit = sg * (price - stop) <= 0
        detail = f"live {price:.2f} vs stop {stop:.2f}"
        if not hit and mode == "swing":
            # Today's session is not a completed candle yet, so the engine has not seen it.
            adverse = live.get("day_low") if sg > 0 else live.get("day_high")
            if adverse is not None and sg * (adverse - stop) <= 0:
                hit = True
                detail = (f"stop {stop:.2f} reached intraday today ("
                          f"{'low' if sg > 0 else 'high'} {adverse:.2f})")
        out.append(_check("Stop not reached", not hit, detail))
    inv = s.get("invalidation_price")
    if inv is not None:
        out.append(_check("Not beyond the invalidation level", sg * (price - inv) >= 0,
                          f"live {price:.2f} vs {inv:.2f} (cancelled if the {tf} candle closes "
                          f"beyond it)"))
    if t1 is not None:
        out.append(_check("T1 not reached", sg * (price - t1) < 0,
                          f"live {price:.2f} vs T1 {t1:.2f}"))
    if stop is not None and t1 is not None and sg * (price - stop) > 0 and sg * (t1 - price) > 0:
        rr = sg * (t1 - price) / (sg * (price - stop))
        min_rr = plan.get("min_rr")
        out.append(_check("Reward/risk from the live price", min_rr is None or rr >= min_rr - 1e-9,
                          f"{rr:.2f}:1 vs minimum {min_rr:g}:1" if min_rr is not None
                          else f"{rr:.2f}:1"))
    return out
