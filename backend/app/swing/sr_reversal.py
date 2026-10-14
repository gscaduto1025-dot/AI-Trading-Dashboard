"""Swing strategy 4: Support / resistance reversal at daily or weekly zones.

LONG at support (SHORT at resistance mirrors):
 1. Zone: a daily zone, or (for daily candles) a weekly zone, known when the reversal
    candle r closed - built only from candles before r.
 2. Approach from above: the close before r was above the zone's top; candle r traded
    into the zone (low <= zone top) but did not close more than max_penetration_atr x ATR
    below the zone's bottom (a deeper close is a breakdown, not a test).
 3. Objective reversal candle at r - either
    * rejection (hammer): lower wick >= min_wick_to_body x body, lower wick >=
      min_wick_share of the range, close in the upper max_close_position of the range; or
    * bullish engulfing (allow_engulfing): previous candle bearish, r bullish, r's body
      engulfs the previous body.
 4. Confirmation: within confirm_window candles after r, a candle closes above r's high,
    with no intervening close below the zone bottom - penetration allowance.
 5. Entry trigger = r's high. Stop = r's low (the rejection wick, which reached into the zone)
    - stop_buffer_atr x ATR. A close below that low invalidates. One candle is never enough: it needs the reversal candle
    AND a later confirming close.
"""

from __future__ import annotations

from ..strategies.common import Condition, candle_metrics
from .base import LONG, SwingResult, SwingStrategy, accept, sgn, watch_plan, zone_ref
from .context import SwingContext, SwingTF, fmt_day


def reversal_pattern(d: SwingTF, r: int, direction: str, cfg) -> str | None:
    b, p = d.bars[r], d.bars[r - 1]
    m = candle_metrics(b)
    body = max(m["body"], 1e-9)
    if direction == LONG:
        wick, pos = m["lower"], m["close_pos"]
        hammer = wick >= cfg.min_wick_to_body * body and wick >= cfg.min_wick_share * m["range"] \
            and pos >= 1 - cfg.max_close_position
        engulf = p.close < p.open and b.close > b.open and b.close >= p.open and b.open <= p.close
    else:
        wick, pos = m["upper"], m["close_pos"]
        hammer = wick >= cfg.min_wick_to_body * body and wick >= cfg.min_wick_share * m["range"] \
            and pos <= cfg.max_close_position
        engulf = p.close > p.open and b.close < b.open and b.close <= p.open and b.open >= p.close
    if hammer:
        return ("hammer / lower-wick rejection" if direction == LONG
                else "shooting star / upper-wick rejection")
    if engulf and cfg.allow_engulfing:
        return "bullish engulfing" if direction == LONG else "bearish engulfing"
    return None


class SRReversal(SwingStrategy):
    id = "swing_sr_reversal"
    name = "Support/resistance reversal"
    config_key = "sr_reversal"

    def _zones(self, ctx: SwingContext, d: SwingTF, r: int):
        out = [(z, d.tf) for z in d.zones_at(r)]
        if d.tf == "1Day":
            out += [(z, "1Week") for z in ctx.weekly_zones_for_daily(r)]
        return out

    def _tested_zone(self, ctx, d, r, direction, cfg):
        """The zone candle r tested after approaching from outside, if any."""
        b, p = d.bars[r], d.bars[r - 1]
        atr = d.atr_before(r)
        pen = cfg.max_penetration_atr * atr
        hits = []
        for z, tf in self._zones(ctx, d, r):
            if direction == LONG and p.close > z.high and b.low <= z.high and b.close >= z.low - pen:
                hits.append((z, tf))
            if direction != LONG and p.close < z.low and b.high >= z.low and b.close <= z.high + pen:
                hits.append((z, tf))
        if not hits:
            return None
        # prefer weekly zones, then the zone with the most reactions
        return max(hits, key=lambda h: (h[1] == "1Week", len(h[0].pivots)))

    def evaluate(self, ctx: SwingContext, d: SwingTF, direction: str, start: int) -> SwingResult:
        cfg = ctx.config.sr_reversal
        s = sgn(direction)
        res = SwingResult()
        bars = d.bars
        n = len(bars)
        window = cfg.confirm_window[d.tf]
        word = "support" if direction == LONG else "resistance"
        pending = None
        for r in range(max(start - window, 21), n):
            pattern = reversal_pattern(d, r, direction, cfg)
            if not pattern:
                continue
            hit = self._tested_zone(ctx, d, r, direction, cfg)
            if not hit:
                continue
            z, ztf = hit
            rb = bars[r]
            atr_r = d.atr_before(r)
            pen = cfg.max_penetration_atr * atr_r
            trig = rb.high if direction == LONG else rb.low
            ext = rb.low if direction == LONG else rb.high  # the rejection wick's extreme
            confirmed = None
            for j in range(r + 1, min(r + 1 + window, n)):
                cj = bars[j].close
                if s * (cj - ((z.low - pen) if direction == LONG else (z.high + pen))) < 0:
                    break  # closed through the zone: failed
                if s * (cj - trig) > 0:
                    confirmed = j
                    break
            if confirmed is None:
                if r + window >= n:  # window still open at the latest candle
                    pending = (r, z, ztf, pattern, trig, ext)
                continue
            if confirmed < start:
                continue
            i = confirmed
            b = bars[i]
            htf = ctx.htf(d.tf, i)
            sup = ctx.supporting(d.end[i])
            conds = [
                Condition(f"{'Weekly' if ztf == '1Week' else 'Daily'} {word} zone", True, True,
                          f"{z.low:.2f}-{z.high:.2f}, {len(z.pivots)} reactions"),
                Condition(f"Approached from {'above' if direction == LONG else 'below'} and "
                          f"tested the zone", True, True,
                          f"{fmt_day(rb.time)} {'low' if direction == LONG else 'high'} "
                          f"{(rb.low if direction == LONG else rb.high):.2f}", rb.time),
                Condition(f"Reversal candle ({pattern})", True, True,
                          f"{fmt_day(rb.time)} O {rb.open:.2f} H {rb.high:.2f} L {rb.low:.2f} "
                          f"C {rb.close:.2f}", rb.time),
                Condition(f"No close more than {cfg.max_penetration_atr} ATR through the zone",
                          True, True, "zone held on a closing basis"),
                Condition(f"Confirmation close beyond the reversal candle within {window}", True,
                          True, f"{fmt_day(b.time)} close {b.close:.2f} vs {trig:.2f}", b.time),
                Condition("Higher timeframe not opposed (supporting)",
                          htf["label"] != ("bearish" if direction == LONG else "bullish"), False,
                          htf["detail"]),
                Condition("4-hour trend aligned (supporting)",
                          sup.get("4Hour") == ("bullish" if direction == LONG else "bearish")
                          if "4Hour" in sup else None, False,
                          f"4H {sup.get('4Hour', 'n/a')}, 1H {sup.get('1Hour', 'n/a')}"),
            ]
            res.triggers.append(accept(
                ctx, d, i, direction, trig, ext, conds,
                [{"time": rb.time, "kind": f"reversal candle ({pattern})",
                  "price": rb.low if direction == LONG else rb.high},
                 {"time": b.time, "kind": "confirmation close", "price": b.close}] +
                [{"time": pv.time, "kind": f"zone reaction ({pv.kind})", "price": pv.price}
                 for pv in z.pivots[-3:]],
                [zone_ref(z, ztf)],
                [f"A close {'below' if direction == LONG else 'above'} {ext:.2f} "
                 f"(the reversal candle's {'low' if direction == LONG else 'high'}) "
                 f"invalidates the reversal."], ext,
                trigger_basis=f"{fmt_day(b.time)} close {'above' if direction == LONG else 'below'} the "
                              f"{pattern} candle's {'high' if direction == LONG else 'low'} {trig:.2f} "
                              f"({fmt_day(rb.time)})",
                anchor_basis=f"Rejection wick {'low' if direction == LONG else 'high'} {ext:.2f} "
                             f"({fmt_day(rb.time)}) inside the {word} zone", strategy_id=self.id))
        if pending:
            r, z, ztf, pattern, trig, ext = pending
            res.watch = watch_plan(
                ctx, d, direction, trig, ext,
                [Condition(f"{'Weekly' if ztf == '1Week' else 'Daily'} {word} zone", True, True,
                           f"{z.low:.2f}-{z.high:.2f}, {len(z.pivots)} reactions"),
                 Condition(f"Reversal candle ({pattern})", True, True, fmt_day(bars[r].time)),
                 Condition("Confirmation close beyond the reversal candle", False, True,
                           f"pending: close {'above' if direction == LONG else 'below'} "
                           f"{trig:.2f} within {window} candle(s) of {fmt_day(bars[r].time)}")],
                [f"{pattern.capitalize()} at {word} {z.low:.2f}-{z.high:.2f} on "
                 f"{fmt_day(bars[r].time)}; waiting for a confirming close beyond {trig:.2f}."],
                [zone_ref(z, ztf)],
                [f"A close {'below' if direction == LONG else 'above'} {ext:.2f}."],
                trigger_basis=f"Close {'above' if direction == LONG else 'below'} the {pattern} candle's "
                              f"{'high' if direction == LONG else 'low'} {trig:.2f} within {window} candle(s)",
                anchor_basis=f"Rejection wick {'low' if direction == LONG else 'high'} {ext:.2f}",
                strategy_id=self.id)
        if not res.triggers and not res.watch:
            c = bars[-1].close if bars else 0
            atr = d.atr_before(n) if n else 0
            near = [z for z, _ in (self._zones(ctx, d, n) if n > 21 else [])
                    if 0 <= s * (c - (z.high if direction == LONG else z.low))
                    <= cfg.watch_distance_atr * atr]
            if near:
                z = near[0]
                res.reasons.append(f"Price {c:.2f} is near {word} {z.low:.2f}-{z.high:.2f}, but no "
                                   f"reversal candle has formed; nothing to act on yet.")
            else:
                res.reasons.append(f"No confirmed reversal at a daily or weekly {word} zone.")
        return res
