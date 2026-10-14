"""Swing strategy 1: Daily breakout (also run on weekly candles).

LONG (SHORT mirrors with support):
 1. Established resistance: a support/resistance zone built only from candles before the
    trigger candle (>= 2 confirmed swing reactions), whose first reaction is at least
    min_zone_age candles old.
 2. Completed-candle breakout: the candle closes above zone.high + breakout_buffer_atr x ATR
    after the previous close was at or below zone.high.
 3. Volume: relative volume (vs the prior 20 candles) >= min_rel_volume when volume exists.
 4. Higher-timeframe structure (require_htf_not_opposed): the weekly context (weekly
    structure + close vs EMA20/50; for weekly candles, close vs a rising/falling EMA50) is
    not bearish.
 5. Entry trigger = zone.high; entry = the breakout close (must be within max_extension_atr
    of the trigger). Stop = zone.low - stop_buffer_atr x ATR. A close back below zone.low
    invalidates. Targets: next zones/swing highs known at the time, or measured R multiples.
"""

from __future__ import annotations

from ..strategies.common import Condition
from .base import (LONG, SwingResult, SwingStrategy, accept, sgn, watch_plan, zone_ref)
from .context import SwingContext, SwingTF, fmt_day


class DailyBreakout(SwingStrategy):
    id = "swing_breakout"
    name = "Daily breakout"
    config_key = "daily_breakout"

    def evaluate(self, ctx: SwingContext, d: SwingTF, direction: str, start: int) -> SwingResult:
        cfg = ctx.config.daily_breakout
        s = sgn(direction)
        res = SwingResult()
        bars = d.bars
        n = len(bars)
        age_min = cfg.min_zone_age[d.tf]
        word = "resistance" if direction == LONG else "support"
        weak: list[str] = []
        for i in range(max(start, 21), n):
            b, p = bars[i], bars[i - 1]
            atr = d.atr_before(i)
            edge = (lambda z: z.high) if direction == LONG else (lambda z: z.low)
            far = (lambda z: z.low) if direction == LONG else (lambda z: z.high)
            broken = [z for z in d.zones_at(i)
                      if s * (p.close - edge(z)) <= 0
                      and s * (b.close - (edge(z) + s * cfg.breakout_buffer_atr * atr)) > 0
                      and min(pv.index for pv in z.pivots) <= i - age_min]
            if not broken:
                continue
            z = max(broken, key=lambda z: s * edge(z))  # the most significant level cleared
            rv = d.relvol(i)
            vol_ok = None if not d.volume_available or rv is None else rv >= cfg.min_rel_volume
            if vol_ok is False:
                weak.append(f"{fmt_day(b.time)} close beyond {word} {edge(z):.2f} on relative "
                            f"volume {rv:.2f} < {cfg.min_rel_volume}")
                continue
            htf = ctx.htf(d.tf, i)
            opposed = htf["label"] == ("bearish" if direction == LONG else "bullish")
            if cfg.require_htf_not_opposed and opposed:
                weak.append(f"{fmt_day(b.time)} breakout against the higher timeframe "
                            f"({htf['detail']})")
                continue
            sup = ctx.supporting(d.end[i])
            first = min(z.pivots, key=lambda pv: pv.index)
            conds = [
                Condition(f"Established {word} zone", True, True,
                          f"{z.low:.2f}-{z.high:.2f}, {len(z.pivots)} reactions since "
                          f"{fmt_day(first.time)} (>= {age_min} candles before)"),
                Condition("Completed candle closed beyond the zone", True, True,
                          f"{fmt_day(b.time)} close {b.close:.2f} vs {edge(z):.2f} + "
                          f"{cfg.breakout_buffer_atr} ATR", b.time),
                Condition(f"Relative volume >= {cfg.min_rel_volume}", vol_ok, True,
                          "volume unavailable on this feed" if vol_ok is None
                          else f"{rv:.2f}x 20-candle average", b.time),
                Condition("Higher-timeframe structure not opposed", not opposed,
                          cfg.require_htf_not_opposed, htf["detail"], b.time),
                Condition("4-hour trend aligned (supporting)",
                          sup.get("4Hour") == ("bullish" if direction == LONG else "bearish")
                          if "4Hour" in sup else None, False, f"4H {sup.get('4Hour', 'n/a')}, "
                          f"1H {sup.get('1Hour', 'n/a')}"),
            ]
            stop = far(z) - s * ctx.config.targets.stop_buffer_atr * atr
            res.triggers.append(accept(
                ctx, d, i, direction, edge(z), stop, conds,
                [{"time": b.time, "kind": "breakout close", "price": b.close}] +
                [{"time": pv.time, "kind": f"zone reaction ({pv.kind})", "price": pv.price}
                 for pv in z.pivots[-4:]],
                [zone_ref(z, d.tf)],
                [f"A close back {'below' if direction == LONG else 'above'} {far(z):.2f} "
                 f"(through the zone) invalidates the breakout."], far(z)))
        # Developing: price approaching an established level.
        if n > 21:
            c = bars[-1].close
            atr = d.atr_before(n)
            near = []
            for z in d.zones_at(n):
                e = z.high if direction == LONG else z.low
                f = z.low if direction == LONG else z.high
                if s * (c - e) <= 0 and s * (f - c) <= cfg.watch_distance_atr * atr \
                        and min(pv.index for pv in z.pivots) <= n - age_min:
                    near.append(z)
            if near:
                z = min(near, key=lambda z: abs((z.high if direction == LONG else z.low) - c))
                e = z.high if direction == LONG else z.low
                f = z.low if direction == LONG else z.high
                htf = ctx.htf(d.tf, n - 1)
                conds = [Condition(f"Established {word} zone", True, True,
                                   f"{z.low:.2f}-{z.high:.2f}, {len(z.pivots)} reactions"),
                         Condition("Completed candle closed beyond the zone", False, True,
                                   f"pending: close {'above' if direction == LONG else 'below'} "
                                   f"{e + s * cfg.breakout_buffer_atr * atr:.2f}"),
                         Condition("Higher-timeframe structure not opposed",
                                   htf["label"] != ("bearish" if direction == LONG else "bullish"),
                                   cfg.require_htf_not_opposed, htf["detail"])]
                res.watch = watch_plan(
                    ctx, d, direction, e, f - s * ctx.config.targets.stop_buffer_atr * atr, conds,
                    [f"Price {c:.2f} is approaching {word} {z.low:.2f}-{z.high:.2f}; waiting for a "
                     f"completed {'daily' if d.tf == '1Day' else 'weekly'} close beyond "
                     f"{e:.2f} with volume."],
                    [zone_ref(z, d.tf)], [f"A close back through {f:.2f} after a breakout."])
        if not res.triggers and not res.watch:
            res.reasons.append(f"No completed close beyond an established {word} zone.")
        res.reasons += weak[-2:]
        return res
