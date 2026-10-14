"""Swing strategy 5: Trend reversal - a sequence of structure changes, never one candle.

Bullish reversal (bearish mirrors):
 1. Prior downtrend: at the break-of-structure candle c, the confirmed structure (last two
    swing highs and lows confirmed before c) was lower highs AND lower lows. c lies within
    `lookback` candles of the trigger.
 2. Break of structure: candle c closes above the last confirmed swing high (the final
    lower high) after the previous close was at or below it.
 3. Higher low: the first swing low formed at/after c, confirmed before the trigger candle,
    above the last swing low before c (the final lower low), and not undercut since.
 4. Higher high (trigger): a completed candle closes above the highest high between c and
    that higher low (the post-break high).
 5. Moving-average shift (require_ema_shift): trigger close above EMA20 and EMA20 higher than
    `ema_slope_bars` candles earlier. EMA20 above EMA50 is a supporting condition.
 Entry trigger = the post-break high; stop = higher low - stop_buffer_atr x ATR; a close
 below the higher low invalidates.
"""

from __future__ import annotations

from ..strategies.common import Condition
from .base import LONG, SwingResult, SwingStrategy, accept, sgn, watch_plan
from .context import SwingContext, SwingTF, fmt_day


class TrendReversal(SwingStrategy):
    id = "swing_trend_reversal"
    name = "Trend reversal"
    config_key = "trend_reversal"

    def _breaks(self, d: SwingTF, direction: str) -> list[dict]:
        """Break-of-structure candles: prior opposite trend, close through the last swing."""
        out = []
        old = "down" if direction == LONG else "up"
        kind = "high" if direction == LONG else "low"
        opp = "low" if direction == LONG else "high"
        s = sgn(direction)
        for c in range(2, len(d.bars)):
            st = d.structure_at(c)
            if st.get("trend") != old:
                continue
            sw = d.swings_before(c)
            last = [x for x in sw if x.kind == kind]
            last_opp = [x for x in sw if x.kind == opp]
            if not last or not last_opp:
                continue
            lvl = last[-1].price
            if s * (d.bars[c - 1].close - lvl) <= 0 < s * (d.bars[c].close - lvl):
                out.append({"c": c, "level": lvl, "swing": last[-1], "extreme": last_opp[-1],
                            "structure": st})
        return out

    def _setup_at(self, d: SwingTF, brk: dict, i: int, direction: str):
        """(pivot, post-break extreme) if a valid higher low / lower high is confirmed before i."""
        s = sgn(direction)
        c = brk["c"]
        opp = "low" if direction == LONG else "high"
        pivots = [x for x in d.swings_before(i) if x.kind == opp and x.index >= c]
        if not pivots:
            return None
        pv = pivots[0]
        if s * (pv.price - brk["extreme"].price) <= 0:
            return None  # not a higher low (lower high for shorts)
        after = d.bars[pv.index + 1:i]
        if after and s * ((min(b.low for b in after) if direction == LONG
                           else max(b.high for b in after)) - pv.price) < 0:
            return None  # price undercut the higher low afterwards
        seg = d.bars[c:pv.index + 1]
        post = max(b.high for b in seg) if direction == LONG else min(b.low for b in seg)
        return pv, post

    def evaluate(self, ctx: SwingContext, d: SwingTF, direction: str, start: int) -> SwingResult:
        cfg = ctx.config.trend_reversal
        s = sgn(direction)
        res = SwingResult()
        bars = d.bars
        n = len(bars)
        lookback = cfg.lookback[d.tf]
        breaks = self._breaks(d, direction)
        used: set[tuple[int, int]] = set()
        old_word = "downtrend" if direction == LONG else "uptrend"
        pv_word = "higher low" if direction == LONG else "lower high"
        new_word = "higher high" if direction == LONG else "lower low"
        for i in range(max(start, 30), n):
            recent = [bk for bk in breaks if i - lookback <= bk["c"] < i]
            if not recent:
                continue
            brk = recent[-1]
            got = self._setup_at(d, brk, i, direction)
            if not got:
                continue
            pv, post = got
            b, p = bars[i], bars[i - 1]
            if not (s * (p.close - post) <= 0 < s * (b.close - post)):
                continue
            if (brk["c"], pv.index) in used:
                continue
            used.add((brk["c"], pv.index))
            e20, e20_prev = d.ema20[i], d.ema20[i - cfg.ema_slope_bars] if i >= cfg.ema_slope_bars \
                else None
            shift = e20 is not None and e20_prev is not None and s * (b.close - e20) > 0 \
                and s * (e20 - e20_prev) > 0
            e50 = d.ema50[i]
            st = brk["structure"]
            sup = ctx.supporting(d.end[i])
            conds = [
                Condition(f"Prior {old_word} (confirmed swings)", True, True,
                          f"at {fmt_day(bars[brk['c']].time)}: {st['label']}; highs {st['highs']}, "
                          f"lows {st['lows']}"),
                Condition("Break of structure", True, True,
                          f"{fmt_day(bars[brk['c']].time)} close {bars[brk['c']].close:.2f} beyond "
                          f"the last swing {brk['swing'].kind} {brk['level']:.2f}",
                          bars[brk["c"]].time),
                Condition(f"Confirmed {pv_word}", True, True,
                          f"{fmt_day(pv.time)} {pv.price:.2f} vs prior {brk['extreme'].price:.2f}",
                          pv.time),
                Condition(f"{new_word.capitalize()} close", True, True,
                          f"{fmt_day(b.time)} close {b.close:.2f} beyond post-break "
                          f"{'high' if direction == LONG else 'low'} {post:.2f}", b.time),
                Condition("Moving-average shift (close beyond EMA20, EMA20 turning)", shift,
                          cfg.require_ema_shift,
                          "EMA20 unavailable" if e20 is None or e20_prev is None else
                          f"close {b.close:.2f}, EMA20 {e20:.2f} vs {e20_prev:.2f} "
                          f"{cfg.ema_slope_bars} candles earlier", b.time),
                Condition(f"EMA20 {'above' if direction == LONG else 'below'} EMA50 (supporting)",
                          None if e20 is None or e50 is None else s * (e20 - e50) > 0, False,
                          "n/a" if e20 is None or e50 is None else f"{e20:.2f} vs {e50:.2f}"),
                Condition("4-hour trend aligned (supporting)",
                          sup.get("4Hour") == ("bullish" if direction == LONG else "bearish")
                          if "4Hour" in sup else None, False,
                          f"4H {sup.get('4Hour', 'n/a')}, 1H {sup.get('1Hour', 'n/a')}"),
            ]
            trig = accept(
                ctx, d, i, direction, post, pv.price, conds,
                [{"time": brk["swing"].time, "kind": f"last {old_word} swing {brk['swing'].kind}",
                  "price": brk["level"]},
                 {"time": bars[brk["c"]].time, "kind": "break of structure",
                  "price": bars[brk["c"]].close},
                 {"time": pv.time, "kind": pv_word, "price": pv.price},
                 {"time": b.time, "kind": f"{new_word} close", "price": b.close}],
                [{"label": f"Post-break {'high' if direction == LONG else 'low'}", "type": "SWING",
                  "low": round(post, 4), "high": round(post, 4), "timeframe": d.tf,
                  "strength": None},
                 {"label": pv_word.capitalize(), "type": "SWING", "low": round(pv.price, 4),
                  "high": round(pv.price, 4), "timeframe": d.tf, "strength": None}],
                [f"A close {'below' if direction == LONG else 'above'} the {pv_word} "
                 f"{pv.price:.2f} invalidates the reversal."], pv.price,
                trigger_basis=f"{fmt_day(b.time)} close {'above' if direction == LONG else 'below'} the "
                              f"post-break {'high' if direction == LONG else 'low'} {post:.2f} "
                              f"({new_word})",
                anchor_basis=f"Confirmed {pv_word} {pv.price:.2f} ({fmt_day(pv.time)})",
                strategy_id=self.id)
            res.triggers.append(trig)
        # Developing: break of structure + higher low confirmed, waiting for the higher high.
        if n > 30:
            recent = [bk for bk in breaks if n - lookback <= bk["c"] < n]
            if recent:
                brk = recent[-1]
                got = self._setup_at(d, brk, n, direction)
                if got and s * (bars[-1].close - got[1]) <= 0:
                    pv, post = got
                    res.watch = watch_plan(
                        ctx, d, direction, post, pv.price,
                        [Condition(f"Prior {old_word}", True, True, brk["structure"]["label"]),
                         Condition("Break of structure", True, True,
                                   f"{fmt_day(bars[brk['c']].time)} beyond {brk['level']:.2f}"),
                         Condition(f"Confirmed {pv_word}", True, True,
                                   f"{fmt_day(pv.time)} {pv.price:.2f}"),
                         Condition(f"{new_word.capitalize()} close", False, True,
                                   f"pending: close {'above' if direction == LONG else 'below'} "
                                   f"{post:.2f}")],
                        [f"Structure broke on {fmt_day(bars[brk['c']].time)} and a {pv_word} "
                         f"held at {pv.price:.2f}; waiting for a close beyond {post:.2f}."],
                        [{"label": f"Post-break {'high' if direction == LONG else 'low'}",
                          "type": "SWING", "low": round(post, 4), "high": round(post, 4),
                          "timeframe": d.tf, "strength": None}],
                        [f"A close {'below' if direction == LONG else 'above'} {pv.price:.2f}."],
                        trigger_basis=f"Close {'above' if direction == LONG else 'below'} the post-break "
                                      f"{'high' if direction == LONG else 'low'} {post:.2f} ({new_word})",
                        anchor_basis=f"Confirmed {pv_word} {pv.price:.2f}", strategy_id=self.id)
                elif not got:
                    res.reasons.append(
                        f"Structure broke on {fmt_day(bars[brk['c']].time)} but no confirmed "
                        f"{pv_word} yet - one break is not a reversal.")
        if not res.triggers and not res.watch and not res.reasons:
            res.reasons.append(f"No {old_word} -> break of structure -> {pv_word} -> {new_word} "
                               f"sequence within {lookback} candles.")
        return res
