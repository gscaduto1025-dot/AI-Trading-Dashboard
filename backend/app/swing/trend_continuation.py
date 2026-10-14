"""Swing strategy 2: Trend continuation.

LONG (SHORT mirrors):
 1. Established trend at the trigger candle: EMA20 > EMA50 > EMA200, close > EMA200, and
    confirmed higher highs AND higher lows (last two swing highs and lows confirmed before
    the candle). Needs min_daily_history daily candles so EMA200 is meaningful. On weekly
    candles with fewer than 200 weeks of history the stack check falls back to EMA20 > EMA50
    with close > EMA50 (stated in the evidence).
 2. Pullback: within pullback_lookback candles, a low came within touch_tolerance_atr x ATR
    of EMA20, EMA50 or a support zone's top (zones known at that candle), without closing
    more than max_close_violation_atr x ATR below it, after retracing at least
    min_pullback_atr x ATR from the highest high of the prior 20 candles.
 3. Confirmation: a bullish candle (close > open) closing above the prior candle's high.
 4. Entry trigger = prior candle high; stop = pullback low - stop_buffer_atr x ATR;
    a close below the pullback low invalidates. The prior swing high is a target candidate.
"""

from __future__ import annotations

from ..strategies.common import Condition
from .base import LONG, SwingResult, SwingStrategy, accept, sgn, watch_plan
from .context import SwingContext, SwingTF, fmt_day


class TrendContinuation(SwingStrategy):
    id = "swing_trend_continuation"
    name = "Trend continuation"
    config_key = "trend_continuation"

    def _trend_ok(self, d: SwingTF, i: int, direction: str) -> tuple[bool, str, dict]:
        s = sgn(direction)
        e20, e50, e200 = d.ema20[i], d.ema50[i], d.ema200[i]
        st = d.structure_at(i)
        if e20 is None or e50 is None:
            return False, "EMA50 not available yet", st
        if e200 is None:
            if d.tf != "1Week":
                return False, "EMA200 not available yet", st
            # < 200 weekly candles: fall back to EMA20 vs EMA50 with close beyond EMA50
            stacked = s * (e20 - e50) > 0 and s * (d.bars[i].close - e50) > 0
        else:
            stacked = s * (e20 - e50) > 0 and s * (e50 - e200) > 0 and \
                s * (d.bars[i].close - e200) > 0
        structure = st.get("trend") == ("up" if direction == LONG else "down")
        why = []
        if not stacked:
            why.append(f"EMA stack {d.ema_stack(i)}")
        if not structure:
            why.append(f"structure {st['label']}")
        return stacked and structure, "; ".join(why), st

    def _levels(self, d: SwingTF, t: int, direction: str):
        out = []
        if d.ema20[t] is not None:
            out.append(("EMA20", d.ema20[t]))
        if d.ema50[t] is not None:
            out.append(("EMA50", d.ema50[t]))
        for z in d.zones_at(t):
            if direction == LONG and z.high < d.bars[t - 1].close:
                out.append((f"support zone {z.low:.2f}-{z.high:.2f}", z.high))
            if direction != LONG and z.low > d.bars[t - 1].close:
                out.append((f"resistance zone {z.low:.2f}-{z.high:.2f}", z.low))
        return out

    def evaluate(self, ctx: SwingContext, d: SwingTF, direction: str, start: int) -> SwingResult:
        cfg = ctx.config.trend_continuation
        s = sgn(direction)
        res = SwingResult()
        bars = d.bars
        n = len(bars)
        if d.tf == "1Day" and n < ctx.config.gates.min_daily_history:
            res.reasons.append(f"Only {n} daily candles; {ctx.config.gates.min_daily_history} "
                               f"needed for a reliable EMA200 trend.")
            return res
        lookback = cfg.pullback_lookback[d.tf]
        used: set[int] = set()
        last_reason = None
        for i in range(max(start, 25), n):
            b, prev = bars[i], bars[i - 1]
            ok, why, st = self._trend_ok(d, i, direction)
            if not ok:
                last_reason = f"No established trend at {fmt_day(b.time)}: {why}."
                continue
            confirm = (b.close > b.open and b.close > prev.high) if direction == LONG else \
                (b.close < b.open and b.close < prev.low)
            if not confirm:
                continue
            atr = d.atr_before(i)
            touch = None
            for t in range(i - 1, max(i - 1 - lookback, 1), -1):
                tb = bars[t]
                probe = tb.low if direction == LONG else tb.high
                for name, px in self._levels(d, t, direction):
                    if s * (probe - px) <= cfg.touch_tolerance_atr * atr and \
                            s * (tb.close - px) >= -cfg.max_close_violation_atr * atr:
                        touch = (t, name, px)
                        break
                if touch:
                    break
            if not touch or touch[0] in used:
                continue
            t, name, px = touch
            window = bars[max(0, t - 20):t]
            extreme = (max(x.high for x in window) if direction == LONG
                       else min(x.low for x in window)) if window else bars[t].close
            depth = s * (extreme - (bars[t].low if direction == LONG else bars[t].high))
            if depth < cfg.min_pullback_atr * atr:
                last_reason = (f"Pullback into {name} on {fmt_day(bars[t].time)} was only "
                               f"{depth / atr:.2f} ATR (< {cfg.min_pullback_atr}).")
                continue
            pull_ext = min(x.low for x in bars[t:i + 1]) if direction == LONG else \
                max(x.high for x in bars[t:i + 1])
            trigger = prev.high if direction == LONG else prev.low
            sup = ctx.supporting(d.end[i])
            conds = [
                Condition(f"EMA20 {'>' if direction == LONG else '<'} EMA50 "
                          f"{'>' if direction == LONG else '<'} EMA200 and close beyond EMA200",
                          True, True, d.ema_stack(i), b.time),
                Condition(f"Confirmed {'higher highs and higher lows' if direction == LONG else 'lower highs and lower lows'}",
                          True, True, f"highs {st['highs']}, lows {st['lows']}", b.time),
                Condition(f"Pullback >= {cfg.min_pullback_atr} ATR into {name}", True, True,
                          f"{fmt_day(bars[t].time)} {'low' if direction == LONG else 'high'} "
                          f"{(bars[t].low if direction == LONG else bars[t].high):.2f} vs {px:.2f}; "
                          f"retraced {depth / atr:.2f} ATR", bars[t].time),
                Condition("Confirmation candle", True, True,
                          f"{fmt_day(b.time)} {'bullish' if direction == LONG else 'bearish'} close "
                          f"{b.close:.2f} beyond prior {'high' if direction == LONG else 'low'} "
                          f"{trigger:.2f}", b.time),
                Condition("4-hour trend aligned (supporting)",
                          sup.get("4Hour") == ("bullish" if direction == LONG else "bearish")
                          if "4Hour" in sup else None, False,
                          f"4H {sup.get('4Hour', 'n/a')}, 1H {sup.get('1Hour', 'n/a')}"),
            ]
            res.triggers.append(accept(
                ctx, d, i, direction, trigger, pull_ext, conds,
                [{"time": bars[t].time, "kind": f"pullback to {name}", "price": px},
                 {"time": b.time, "kind": "confirmation close", "price": b.close}],
                [{"label": name, "type": "DYNAMIC" if name.startswith("EMA") else "ZONE",
                  "low": px, "high": px, "timeframe": d.tf, "strength": None}],
                [f"A close {'below' if direction == LONG else 'above'} the pullback "
                 f"{'low' if direction == LONG else 'high'} {pull_ext:.2f} ends the setup."],
                pull_ext, [(extreme, f"prior swing {'high' if direction == LONG else 'low'}")],
                trigger_basis=f"{fmt_day(b.time)} {'bullish' if direction == LONG else 'bearish'} close "
                              f"{'above' if direction == LONG else 'below'} the prior candle "
                              f"{'high' if direction == LONG else 'low'} {trigger:.2f}",
                anchor_basis=f"Pullback {'low' if direction == LONG else 'high'} {pull_ext:.2f} "
                             f"(into {name})", strategy_id=self.id))
            used.add(t)
        # Developing: trend intact and price pulling back to EMA20/EMA50.
        if n > 25:
            i = n - 1
            ok, why, st = self._trend_ok(d, i, direction)
            atr = d.atr_before(n)
            if ok:
                c = bars[i]
                near = [(nm, px) for nm, px in self._levels(d, i, direction)
                        if nm.startswith("EMA") and
                        0 <= s * ((c.low if direction == LONG else c.high) - px) <= cfg.watch_distance_atr * atr]
                if near:
                    nm, px = near[0]
                    trig = c.high if direction == LONG else c.low
                    anchor = min(c.low, px) if direction == LONG else max(c.high, px)
                    res.watch = watch_plan(
                        ctx, d, direction, trig, anchor,
                        [Condition("Established trend", True, True, f"{d.ema_stack(i)}; {st['label']}"),
                         Condition(f"Pullback into {nm}", True, True, f"{px:.2f}"),
                         Condition("Confirmation candle", False, True,
                                   f"pending: close {'above' if direction == LONG else 'below'} {trig:.2f}")],
                        [f"Trend intact; price is pulling back to {nm} {px:.2f}. Waiting for a "
                         f"{'bullish' if direction == LONG else 'bearish'} close beyond {trig:.2f}."],
                        [{"label": nm, "type": "DYNAMIC", "low": px, "high": px, "timeframe": d.tf,
                          "strength": None}],
                        [f"A close beyond the pullback extreme {anchor:.2f}."],
                        trigger_basis=f"{'Bullish' if direction == LONG else 'Bearish'} close "
                                      f"{'above' if direction == LONG else 'below'} the latest candle "
                                      f"{'high' if direction == LONG else 'low'} {trig:.2f}",
                        anchor_basis=f"Pullback extreme / {nm} {anchor:.2f}", strategy_id=self.id)
            else:
                last_reason = f"No established trend now: {why}."
        if not res.triggers and not res.watch:
            res.reasons.append(last_reason or "No pullback-and-confirmation within the trend.")
        return res
