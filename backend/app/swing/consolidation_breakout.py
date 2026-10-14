"""Swing strategy 3: Consolidation breakout.

Range (rebuilt for every candle i from candles before i only):
 * the longest window of L candles ending at i-1, min_bars <= L <= max_bars, whose
   height (highest high - lowest low) is <= max_range_atr x ATR;
 * at least min_touches candles with a high within edge_tolerance_atr x ATR of the range
   high, and at least min_touches with a low within that distance of the range low
   (price really oscillated between both edges, not a drift).
LONG (SHORT mirrors):
 * Breakout confirmation: candle i closes above range high + breakout_buffer_atr x ATR,
   the previous close was inside the range, relative volume >= min_rel_volume when
   volume exists.
 * Entry trigger = range high. Stop anchor (stop_mode): "breakout_candle" (default) - the
   breakout candle's low, but never below the range midpoint (a move back through the
   breakout candle = failed breakout); "midpoint"; or "opposite" (range low). The planner
   subtracts the volatility buffer. Invalidation: a close back below the midpoint.
   Note: with a midpoint stop and a 1x measured-move T1, reward/risk is structurally
   below 2:1, so those plans are NO_TRADE by the planner's default rule.
 * Targets: levels known at the time plus measured moves range high + m x height.
"""

from __future__ import annotations

from ..strategies.common import Condition
from .base import LONG, SwingResult, SwingStrategy, accept, sgn, watch_plan
from .context import SwingContext, SwingTF, fmt_day


def find_range(d: SwingTF, i: int, cfg) -> dict | None:
    """Longest qualifying consolidation ending at candle i-1 (uses candles < i only)."""
    atr = d.atr_before(i)
    lo_n, hi_n = cfg.min_bars[d.tf], cfg.max_bars[d.tf]
    best = None
    for L in range(lo_n, min(hi_n, i) + 1):
        w = d.bars[i - L:i]
        top, bot = max(b.high for b in w), min(b.low for b in w)
        if top - bot > cfg.max_range_atr * atr:
            break  # a longer window can only be taller
        tol = cfg.edge_tolerance_atr * atr
        top_t = sum(1 for b in w if b.high >= top - tol)
        bot_t = sum(1 for b in w if b.low <= bot + tol)
        if top_t >= cfg.min_touches and bot_t >= cfg.min_touches:
            best = {"start": i - L, "end": i - 1, "bars": L, "high": top, "low": bot,
                    "mid": (top + bot) / 2, "height": top - bot, "atr": atr,
                    "top_touches": top_t, "bottom_touches": bot_t}
    return best


class ConsolidationBreakout(SwingStrategy):
    id = "swing_consolidation_breakout"
    name = "Consolidation breakout"
    config_key = "consolidation_breakout"

    def _levels(self, d: SwingTF, r: dict) -> list[dict]:
        return [{"label": f"Range high ({r['top_touches']} touches)", "type": "RANGE_HIGH",
                 "low": round(r["high"], 4), "high": round(r["high"], 4), "timeframe": d.tf,
                 "strength": None},
                {"label": f"Range low ({r['bottom_touches']} touches)", "type": "RANGE_LOW",
                 "low": round(r["low"], 4), "high": round(r["low"], 4), "timeframe": d.tf,
                 "strength": None},
                {"label": "Range midpoint", "type": "RANGE_MID", "low": round(r["mid"], 4),
                 "high": round(r["mid"], 4), "timeframe": d.tf, "strength": None}]

    @staticmethod
    def _anchor(r: dict, candle, direction: str, cfg) -> tuple[float, str]:
        """Structural stop anchor; `candle` is the breakout candle (or, for a developing
        setup, the latest candle the breakout will start from)."""
        if cfg.stop_mode == "breakout_candle":
            if direction == LONG:
                p = max(candle.low, r["mid"])
                return p, (f"Breakout candle low {candle.low:.2f}" if p == candle.low else
                           f"Range midpoint {p:.2f} (breakout candle low is deeper)")
            p = min(candle.high, r["mid"])
            return p, (f"Breakout candle high {candle.high:.2f}" if p == candle.high else
                       f"Range midpoint {p:.2f} (breakout candle high is higher)")
        if cfg.stop_mode == "midpoint":
            return r["mid"], ConsolidationBreakout._anchor_basis(r, direction, cfg)
        p = r["low"] if direction == LONG else r["high"]
        return p, ConsolidationBreakout._anchor_basis(r, direction, cfg)

    @staticmethod
    def _anchor_basis(r: dict, direction: str, cfg) -> str:
        if cfg.stop_mode == "midpoint":
            return f"Range midpoint {r['mid']:.2f} (a close back there fails the breakout)"
        return (f"Opposite side of the range ({'low' if direction == LONG else 'high'} "
                f"{r['low'] if direction == LONG else r['high']:.2f})")

    def evaluate(self, ctx: SwingContext, d: SwingTF, direction: str, start: int) -> SwingResult:
        cfg = ctx.config.consolidation_breakout
        s = sgn(direction)
        res = SwingResult()
        bars = d.bars
        n = len(bars)
        weak: list[str] = []
        for i in range(max(start, cfg.min_bars[d.tf] + 15), n):
            b, p = bars[i], bars[i - 1]
            r = find_range(d, i, cfg)
            if not r:
                continue
            edge = r["high"] if direction == LONG else r["low"]
            atr = r["atr"]
            if not (s * (p.close - edge) <= 0 and
                    s * (b.close - (edge + s * cfg.breakout_buffer_atr * atr)) > 0):
                continue
            vol = d.volume(i)
            if vol.rejects(cfg.min_rel_volume):
                weak.append(f"{fmt_day(b.time)} broke the {r['bars']}-candle range on relative "
                            f"volume {vol.rv:.2f} < {cfg.min_rel_volume} "
                            f"({vol.level}, {vol.source})")
                continue
            base, base_why = self._anchor(r, b, direction, cfg)
            htf = ctx.htf(d.tf, i)
            sup = ctx.supporting(d.end[i])
            conds = [
                Condition(f"Consolidation of {cfg.min_bars[d.tf]}-{cfg.max_bars[d.tf]} candles",
                          True, True,
                          f"{r['bars']} candles {fmt_day(bars[r['start']].time)} to "
                          f"{fmt_day(bars[r['end']].time)}: {r['low']:.2f}-{r['high']:.2f}"),
                Condition(f"Range height <= {cfg.max_range_atr} ATR", True, True,
                          f"{r['height']:.2f} = {r['height'] / atr:.2f} ATR"),
                Condition(f">= {cfg.min_touches} touches of each edge", True, True,
                          f"high {r['top_touches']}, low {r['bottom_touches']} "
                          f"(within {cfg.edge_tolerance_atr} ATR)"),
                Condition("Completed candle closed beyond the range", True, True,
                          f"{fmt_day(b.time)} close {b.close:.2f} vs {edge:.2f} "
                          f"{'+' if direction == LONG else '-'} {cfg.breakout_buffer_atr} ATR",
                          b.time),
                Condition(f"Relative volume >= {cfg.min_rel_volume}",
                          vol.passed(cfg.min_rel_volume), True, vol.detail("20-candle average"),
                          b.time, vol.level),
                Condition("Higher timeframe aligned (supporting)",
                          htf["label"] == ("bullish" if direction == LONG else "bearish"), False,
                          htf["detail"]),
                Condition("4-hour trend aligned (supporting)",
                          sup.get("4Hour") == ("bullish" if direction == LONG else "bearish")
                          if "4Hour" in sup else None, False,
                          f"4H {sup.get('4Hour', 'n/a')}, 1H {sup.get('1Hour', 'n/a')}"),
            ]
            extras = [(edge + s * m * r["height"], f"measured move {m:g}x range height")
                      for m in cfg.measured_move_multiples]
            res.triggers.append(accept(
                ctx, d, i, direction, edge, base, conds,
                [{"time": b.time, "kind": "range breakout close", "price": b.close},
                 {"time": bars[r["start"]].time, "kind": "range start", "price": edge}],
                self._levels(d, r),
                [f"A close back {'below' if direction == LONG else 'above'} the range midpoint "
                 f"{r['mid']:.2f} invalidates the breakout."], r["mid"], extras,
                trigger_basis=f"{fmt_day(b.time)} close {'above the range high' if direction == LONG else 'below the range low'} "
                              f"{edge:.2f} ({r['bars']}-candle range, by {cfg.breakout_buffer_atr} ATR)",
                anchor_basis=base_why, strategy_id=self.id))
        # Developing: the latest candles form a range and price is still inside it.
        if n > cfg.min_bars[d.tf] + 15:
            r = find_range(d, n, cfg)
            c = bars[-1].close
            if r and r["low"] <= c <= r["high"]:
                edge = r["high"] if direction == LONG else r["low"]
                base, base_why = self._anchor(r, bars[-1], direction, cfg)
                if cfg.stop_mode == "breakout_candle":
                    base_why += " (planned from the latest candle; recomputed on the breakout candle)"
                atr = r["atr"]
                res.watch = watch_plan(
                    ctx, d, direction, edge, base,
                    [Condition("Consolidation", True, True,
                               f"{r['bars']} candles {r['low']:.2f}-{r['high']:.2f} "
                               f"({r['height'] / atr:.2f} ATR)"),
                     Condition(">= touches of each edge", True, True,
                               f"high {r['top_touches']}, low {r['bottom_touches']}"),
                     Condition("Completed candle closed beyond the range", False, True,
                               f"pending: close {'above' if direction == LONG else 'below'} "
                               f"{edge + s * cfg.breakout_buffer_atr * atr:.2f} with volume")],
                    [f"Price {c:.2f} is inside a {r['bars']}-candle range "
                     f"{r['low']:.2f}-{r['high']:.2f}; waiting for a completed close "
                     f"{'above' if direction == LONG else 'below'} {edge:.2f}."],
                    self._levels(d, r),
                    [f"After a breakout, a close back through the midpoint {r['mid']:.2f}."],
                    [(edge + s * m * r["height"], f"measured move {m:g}x range height")
                     for m in cfg.measured_move_multiples],
                    trigger_basis=f"Completed close {'above the range high' if direction == LONG else 'below the range low'} "
                                  f"{edge:.2f} ({r['bars']}-candle range) with volume",
                    anchor_basis=base_why, strategy_id=self.id)
        if not res.triggers and not res.watch:
            res.reasons.append("No qualifying consolidation range with a completed breakout.")
        res.reasons += weak[-2:]
        return res
