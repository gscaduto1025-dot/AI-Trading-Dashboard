"""Hypothetical execution of one signal on the candles AFTER it was generated.

Rules (all costs adverse; every assumption is in BacktestConfig and reported):
- Entry: market order at the OPEN of the first execution candle starting at or after the
  moment the signal was visible (+ `entry_delay_bars`), plus half the spread and slippage.
  Day trades must enter in the signal's session (no overnight entries).
- No entry if that price is already through the stop, at/through T1, or outside the plan's
  entry zone (the live plan would not be taken there either).
- Stop (stop-market): fills at the stop, or at the open if the candle gaps through it,
  minus half spread and slippage.
- Target (resting limit): fills at the target only if the candle trades THROUGH it by
  one tick; a gap through the target still fills at the target (no gap windfall).
- Stop and target inside the same candle: the STOP is assumed to fill first (the
  conservative order, since intrabar sequencing is unknown). This also applies to the
  entry candle itself.
- Day trades exit at the session's last candle close; swing trades after `max_hold_bars`
  daily candles at the close; a series that ends (delisting / data end) exits at the last
  close. Those exits pay half spread and slippage.
- Commission per share on each side.
R multiples use the risk at the actual fill (|fill - stop|).
"""

from __future__ import annotations

from datetime import datetime

from ..data.base import Bar
from ..market.calendar import NY
from .config import BacktestConfig


def _day(ts: int):
    return datetime.fromtimestamp(ts, NY).date()


def _skip(sig: dict, reason: str, **extra) -> dict:
    return {"signal_id": sig["id"], "ticker": sig["ticker"], "strategy": sig["strategy"],
            "timeframe": sig["timeframe"], "direction": sig["direction"],
            "seen_at": sig["seen_at"], "reason": reason, **extra}


def simulate(sig: dict, bars: list[Bar], cfg: BacktestConfig, mode: str) -> tuple[dict | None, dict | None]:
    """(trade, None) or (None, skipped). `bars` = execution candles starting at or after
    the signal candle's close (`confirmed_at`); nothing earlier is ever used. The first one
    opens with the first trade after the signal; the seconds between the close and the
    order are covered by `slippage_bps` (use `entry_delay_bars` for whole-candle delays)."""
    if bars and bars[0].time < sig["confirmed_at"]:  # timing guard: nothing before the signal
        raise ValueError("execution candles must start at or after the signal candle's close")
    long = sig["direction"] == "LONG"
    sgn = 1 if long else -1
    stop, t1, t2 = sig["stop"], sig["target1"], sig["target2"]
    if stop is None or t1 is None:
        return None, _skip(sig, "plan has no stop or target")
    k = cfg.entry_delay_bars
    if len(bars) <= k:
        return None, _skip(sig, "no candles after the signal (data end)")
    eb = bars[k]
    if mode == "day" and _day(eb.time) != _day(sig["confirmed_at"]):
        return None, _skip(sig, "signal at the session close: no intraday entry possible")
    hs, sl = cfg.half_spread_bps[mode] / 1e4, cfg.slippage_bps[mode] / 1e4
    raw = eb.open
    if (raw - stop) * sgn <= 0:
        return None, _skip(sig, "opened through the stop", open=raw)
    if (raw - t1) * sgn >= 0:
        return None, _skip(sig, "opened at/through T1 (reward gone)", open=raw)
    if cfg.respect_entry_zone and sig.get("entry_zone"):
        lo, hi = sorted(sig["entry_zone"])
        if not lo <= raw <= hi:
            return None, _skip(sig, "opened outside the plan's entry zone (no chasing)",
                               open=raw, zone=[lo, hi])
    fill = raw * (1 + sgn * (hs + sl))
    risk = (fill - stop) * sgn
    if risk <= 0:
        return None, _skip(sig, "costs pushed the fill through the stop", open=raw)

    # legs: (fraction, target)
    if cfg.exit_policy == "t1":
        legs = [[1.0, t1, None]]
    elif cfg.exit_policy == "t2":
        legs = [[1.0, t2 or t1, None]]
    else:
        legs = [[0.5, t1, None], [0.5, t2 or t1, None]]
    # each leg: [fraction, target, (net exit price, reason, time, exit price before costs)]
    session = _day(eb.time)
    mae = mfe = 0.0
    held = 0
    last = eb
    j = k
    for j in range(k, len(bars)):
        b = bars[j]
        if mode == "day" and _day(b.time) != session:
            j -= 1
            break  # session ended (handled below as an EOD exit at the previous close)
        last = b
        held += 1
        first = j == k
        open_legs = [g for g in legs if g[2] is None]
        # adverse / favourable excursion (in price) during the trade
        mae = max(mae, (fill - (b.low if long else b.high)) * sgn)
        mfe = max(mfe, ((b.high if long else b.low) - fill) * sgn)
        # 1) gap at the open (not on the entry candle: we bought that open)
        if not first and (b.open - stop) * sgn <= 0:
            px = b.open * (1 - sgn * (hs + sl))
            for g in open_legs:
                g[2] = (px, "stop (gap)", b.time, b.open)
            break
        hit_stop = ((b.low if long else b.high) - stop) * sgn <= 0
        if hit_stop:  # conservative: the stop fills first even if a target also traded
            px = stop * (1 - sgn * (hs + sl))
            both = any(((b.high if long else b.low) - (g[1] + sgn * cfg.tick)) * sgn >= 0
                       for g in open_legs)
            for g in open_legs:
                g[2] = (px, "stop (same candle as target)" if both else "stop", b.time, stop)
            break
        for g in open_legs:
            through = ((b.high if long else b.low) - (g[1] + sgn * cfg.tick)) * sgn >= 0
            gapped = not first and (b.open - g[1]) * sgn >= 0
            if through or gapped:
                g[2] = (g[1], "target1" if g[1] == t1 else "target2", b.time, g[1])
        if all(g[2] for g in legs):
            break
        if mode == "swing" and cfg.max_hold_bars["swing"] and held >= cfg.max_hold_bars["swing"]:
            px = b.close * (1 - sgn * (hs + sl))
            for g in legs:
                if g[2] is None:
                    g[2] = (px, "time exit", b.time, b.close)
            break
    more = j + 1 < len(bars) if bars else False  # a later candle exists (next session)
    for g in legs:  # still open: session close (day) or end of data
        if g[2] is None:
            px = last.close * (1 - sgn * (hs + sl))
            g[2] = (px, "session close" if mode == "day" and more else "data end", last.time,
                    last.close)

    comm = 2 * cfg.commission_per_share
    exit_px = sum(g[0] * g[2][0] for g in legs)
    pnl = sum(f * (px - fill) * sgn for f, _, (px, _, _, _) in legs) - comm
    gross = sum(f * (rx - raw) * sgn for f, _, (_, _, _, rx) in legs)  # before any cost
    exit_time = max(g[2][2] for g in legs)
    reasons = sorted({g[2][1] for g in legs})
    return {
        "id": sig["id"], "ticker": sig["ticker"], "mode": mode, "strategy": sig["strategy"],
        "strategy_name": sig["strategy_name"], "timeframe": sig["timeframe"],
        "direction": sig["direction"], "signal_time": sig["confirmed_at"],
        "entry_time": eb.time, "exit_time": exit_time,
        "plan_entry": sig["plan_entry"], "open": raw, "entry": round(fill, 4),
        "stop": stop, "target1": t1, "target2": t2, "exit": round(exit_px, 4),
        "exit_reason": " + ".join(reasons), "legs": [
            {"fraction": f, "target": tg, "exit": round(px, 4), "reason": r, "time": ts}
            for f, tg, (px, r, ts, _) in legs],
        "risk_per_share": round(risk, 4), "pnl_per_share": round(pnl, 4),
        "r": round(pnl / risk, 4), "gross_r": round(gross / risk, 4),
        "cost_per_share": round(gross - pnl, 4),
        "return_pct": round(100 * pnl / fill, 4),
        "mae_r": round(-mae / risk, 3), "mfe_r": round(mfe / risk, 3),
        "bars_held": held, "planned_rr": sig["planned_rr"], "quality": sig["quality"],
    }, None
