"""Strategy 4: Trend continuation pullback.

LONG (SHORT mirrors):
 1. Higher-timeframe trend: HTF bias (daily close vs EMA20 vs EMA50, combined with the
    1-hour same rule using only closed 1-hour candles) is bullish (require_htf_alignment).
 2. Intraday alignment on the confirmation candle: EMA9 > EMA20 and close > session VWAP.
 3. Pullback: at least min_pullback_bars consecutive lower highs ending at a touch candle.
 4. Touch: within the last touch_lookback_bars candles, a low came within
    touch_tolerance_atr x ATR of VWAP, EMA20 or a support zone's top, and that candle did not
    close more than max_violation_atr x ATR below it.
 5. Price-action confirmation: a bullish candle (close > open) closing above the prior
    candle's high. Entry = that close. Stop = lowest low since the touch - stop_buffer_atr x ATR.
An indicator being bullish never triggers on its own: steps 3-5 are required.
"""

from __future__ import annotations

from .base import Strategy, StrategyResult, accept, ref, sgn
from .common import LONG, Condition, StrategyContext, TFData, fmt, level_ref


class TrendPullback(Strategy):
    id = "trend_pullback"
    name = "Trend continuation pullback"
    config_key = "trend_pullback"

    def _supports(self, ctx: StrategyContext, tfd: TFData, j: int, direction: str):
        """(name, price) of the levels a pullback can touch at candle j."""
        out = []
        if tfd.vwap[j] is not None:
            out.append(("session VWAP", tfd.vwap[j]))
        if tfd.ema20[j] is not None:
            out.append((f"{tfd.tf} EMA20", tfd.ema20[j]))
        for z in ctx.zones():
            if direction == LONG and z["type"] == "SUPPORT_ZONE":
                out.append((z["label"], z["high"]))
            if direction != LONG and z["type"] == "RESISTANCE_ZONE":
                out.append((z["label"], z["low"]))
        return out

    def evaluate(self, ctx: StrategyContext, tfd: TFData, direction: str) -> StrategyResult:
        cfg = ctx.config.trend_pullback
        s = sgn(direction)
        want = "bullish" if direction == LONG else "bearish"
        res = StrategyResult()
        bars = tfd.bars
        sess = list(tfd.session)
        if len(sess) < 3:
            res.reasons.append("Not enough session candles for a pullback.")
            return res
        last_reason = None
        used_touches: set[int] = set()  # one trigger per pullback
        for i in sess[2:]:
            b, prev = bars[i], bars[i - 1]
            atr = tfd.atr_before(i)
            htf = ctx.htf_bias(tfd.end[i])
            if cfg.require_htf_alignment and htf["label"] != want:
                last_reason = f"Higher-timeframe bias is {htf['label']} ({htf['detail']})."
                continue
            e9, e20, vw = tfd.ema9[i], tfd.ema20[i], tfd.vwap[i]
            if None in (e9, e20, vw) or not (s * (e9 - e20) > 0 and s * (b.close - vw) > 0):
                last_reason = (f"Intraday alignment missing at {fmt(b.time)}: EMA9 "
                               f"{'above' if direction == LONG else 'below'} EMA20 and close "
                               f"{'above' if direction == LONG else 'below'} VWAP required.")
                continue
            confirm = (b.close > b.open and b.close > prev.high) if direction == LONG else \
                (b.close < b.open and b.close < prev.low)
            if not confirm:
                continue
            touch = None
            for t in range(i - 1, max(tfd.start, i - cfg.touch_lookback_bars) - 1, -1):
                tb = bars[t]
                probe = tb.low if direction == LONG else tb.high
                for name, px in self._supports(ctx, tfd, t, direction):
                    near = s * (probe - px) <= cfg.touch_tolerance_atr * atr
                    held = s * (tb.close - px) >= -cfg.max_violation_atr * atr
                    above_before = t > 0 and s * (bars[t - 1].close - px) > 0
                    if near and held and above_before:
                        touch = (t, name, px)
                        break
                if touch:
                    break
            if not touch:
                last_reason = f"No pullback touch of VWAP/EMA20/support before {fmt(b.time)}."
                continue
            t, name, px = touch
            if t in used_touches:
                continue
            # Pullback structure: consecutive lower highs (long) ending at the touch.
            n = 0
            for k in range(t, tfd.start, -1):
                cur, before = bars[k], bars[k - 1]
                if (cur.high < before.high) if direction == LONG else (cur.low > before.low):
                    n += 1
                else:
                    break
            if n < cfg.min_pullback_bars:
                last_reason = (f"Pullback into {name} at {fmt(bars[t].time)} had {n} "
                               f"{'lower highs' if direction == LONG else 'higher lows'} "
                               f"(< {cfg.min_pullback_bars}).")
                continue
            extreme = min(x.low for x in bars[t:i + 1]) if direction == LONG else \
                max(x.high for x in bars[t:i + 1])
            sess_ext = max(x.high for x in bars[tfd.start:i]) if direction == LONG else \
                min(x.low for x in bars[tfd.start:i])
            conds = [
                Condition(f"Higher-timeframe trend {want}", True, cfg.require_htf_alignment,
                          htf["detail"], b.time),
                Condition("EMA9 vs EMA20 and close vs VWAP aligned", True, True,
                          f"EMA9 {e9:.2f}, EMA20 {e20:.2f}, VWAP {vw:.2f}, close {b.close:.2f}",
                          b.time),
                Condition(f">= {cfg.min_pullback_bars} consecutive "
                          f"{'lower highs' if direction == LONG else 'higher lows'}", True, True,
                          f"{n} into the touch", bars[t].time),
                Condition(f"Pullback touched {name}", True, True,
                          f"{fmt(bars[t].time)} {'low' if direction == LONG else 'high'} "
                          f"{(bars[t].low if direction == LONG else bars[t].high):.2f} vs {px:.2f}",
                          bars[t].time),
                Condition("Price-action confirmation", True, True,
                          f"{fmt(b.time)} {'bullish' if direction == LONG else 'bearish'} close "
                          f"{b.close:.2f} beyond prior candle "
                          f"{'high' if direction == LONG else 'low'} "
                          f"{(prev.high if direction == LONG else prev.low):.2f}", b.time),
            ]
            ev = [{"time": bars[t].time, "kind": f"touch {name}", "price": px},
                  {"time": b.time, "kind": "confirmation", "price": b.close}]
            lv = [{"label": name, "type": "DYNAMIC", "low": px, "high": px, "timeframe": tfd.tf,
                   "strength": None}]
            lv += [level_ref(z) for z in ctx.zones() if z["label"] == name]
            inv = [f"A move {'below the pullback low' if direction == LONG else 'above the pullback high'} "
                   f"{extreme:.2f} invalidates the pullback.",
                   f"A close {'below' if direction == LONG else 'above'} {name} weakens the trend."]
            pref = prev.high if direction == LONG else prev.low
            trig = ref(pref, f"{tfd.tf} {'bullish' if direction == LONG else 'bearish'} close "
                             f"{'above' if direction == LONG else 'below'} the prior candle "
                             f"{'high' if direction == LONG else 'low'} {pref:.2f}", "candle", tfd.tf)
            anchor = ref(extreme, f"Pullback {'low' if direction == LONG else 'high'} "
                                  f"{extreme:.2f} (into {name})", "pullback", tfd.tf)
            res.triggers.append(accept(ctx, tfd, i, direction, trig, anchor, conds, ev, lv, inv,
                                       [(sess_ext, f"session {'high' if direction == LONG else 'low'} (swing level)")],
                                       strategy_id=self.id))
            used_touches.add(t)
        # Developing: aligned trend and price pulling back near VWAP/EMA20.
        i = sess[-1]
        b = bars[i]
        htf = ctx.htf_bias(tfd.end[i])
        if ctx.session_open and (not cfg.require_htf_alignment or htf["label"] == want):
            atr = tfd.atr_before(i)
            near = [(n, p) for n, p in self._supports(ctx, tfd, i, direction)
                    if 0 <= s * ((b.low if direction == LONG else b.high) - p)
                    <= cfg.watch_distance_atr * atr]
            if near:
                res.watch = {"time": b.time, "reasons": [
                    f"HTF {want}; price pulling back to {near[0][0]} {near[0][1]:.2f}. Waiting for "
                    f"a {'bullish' if direction == LONG else 'bearish'} confirmation candle."],
                    "conditions": [Condition(f"Higher-timeframe trend {want}", True, True,
                                             htf["detail"]),
                                   Condition("Pullback touch", False, True, "approaching"),
                                   Condition("Price-action confirmation", False, True, "pending")],
                    "levels": [],
                    "invalidation": [f"A move {'below' if direction == LONG else 'above'} "
                                     f"{near[0][0]} {near[0][1]:.2f}."],
                    "trigger": ref(b.high if direction == LONG else b.low,
                                   f"{tfd.tf} {'bullish' if direction == LONG else 'bearish'} close "
                                   f"{'above' if direction == LONG else 'below'} the latest candle "
                                   f"{'high' if direction == LONG else 'low'} "
                                   f"{(b.high if direction == LONG else b.low):.2f}", "candle", tfd.tf),
                    "stop_anchor": ref(min(b.low, near[0][1]) if direction == LONG else max(b.high, near[0][1]),
                                       f"Below {near[0][0]} / the pullback low" if direction == LONG
                                       else f"Above {near[0][0]} / the pullback high", "pullback", tfd.tf)}
        if not res.triggers and not res.watch:
            res.reasons.append(last_reason or "No qualifying pullback and confirmation.")
        return res
