"""Strategy 1: Breakout and retest.

LONG (SHORT mirrors with support):
 1. Zone: a valid key-level S/R zone known at the open, or PDH/PDL/PWH/PWL widened to
    +/- reference_zone_half_width_atr.
 2. Breakout: a completed candle closes above zone.high + breakout_buffer_atr x ATR after
    the previous close was at or below zone.high.
 3. Volume: breakout candle relative volume >= min_breakout_rel_volume (skipped, and shown
    as n/a, when the feed reports no volume).
 4. Retest: within retest_window_bars, a candle's low comes back to within
    retest_tolerance_atr x ATR of zone.high (or into the zone) without closing below zone.low.
 5. Entry confirmation: within confirm_window_bars after the retest, a candle closes above
    the retest candle's high. Entry = that close.
 6. Invalidation / stop: a close below zone.low cancels the setup; stop = min(retest lows,
    zone.low) - stop_buffer_atr x ATR.
"""

from __future__ import annotations

from .base import Strategy, StrategyResult, accept, ref, sgn
from .common import LONG, Condition, StrategyContext, TFData, fmt, level_ref

REF_TYPES = ("PDH", "PDL", "PWH", "PWL")


def candidate_zones(ctx: StrategyContext, tfd: TFData, half_width_atr: float) -> list[dict]:
    """Zones as (low, high) ranges with their key-level record."""
    out = []
    atr0 = tfd.atr_before(tfd.start) if tfd.start < len(tfd.bars) else 0.0
    for l in ctx.zones():
        out.append({"low": l["low"], "high": l["high"], "level": l})
    for l in ctx.reference_levels(REF_TYPES):
        w = half_width_atr * atr0
        out.append({"low": l["price"] - w, "high": l["price"] + w, "level": l})
    return out


class BreakoutRetest(Strategy):
    id = "breakout_retest"
    name = "Breakout and retest"
    config_key = "breakout_retest"

    def evaluate(self, ctx: StrategyContext, tfd: TFData, direction: str) -> StrategyResult:
        cfg = ctx.config.breakout_retest
        s = sgn(direction)
        res = StrategyResult()
        bars = tfd.bars
        if not list(tfd.session):
            res.reasons.append("No completed candles in the session yet.")
            return res
        zones = candidate_zones(ctx, tfd, cfg.reference_zone_half_width_atr)
        if not zones:
            res.reasons.append("No confirmed support/resistance zones available.")
            return res
        latest_watch = None
        weak_volume: list[str] = []
        for z in zones:
            out_edge = z["high"] if direction == LONG else z["low"]  # side price breaks through
            in_edge = z["low"] if direction == LONG else z["high"]  # close back past = failure
            label = z["level"]["label"]
            phase, b, r = "pre", None, None
            for i in tfd.session:
                bar, prev = bars[i], bars[i - 1] if i > 0 else None
                atr = tfd.atr_before(i)
                if phase == "pre":
                    if prev is None:
                        continue
                    broke = s * (bar.close - (out_edge + s * cfg.breakout_buffer_atr * atr)) > 0 \
                        and s * (prev.close - out_edge) <= 0
                    if not broke:
                        continue
                    rv = tfd.relvol[i]
                    if tfd.volume_available and rv is not None and rv < cfg.min_breakout_rel_volume:
                        weak_volume.append(f"{fmt(bar.time)} break of {label} had relative volume "
                                           f"{rv:.2f} < {cfg.min_breakout_rel_volume}")
                        continue
                    phase, b = "broken", i
                    continue
                if s * (bar.close - in_edge) < 0:  # closed back through the zone: failed
                    phase, b, r = "pre", None, None
                    continue
                if phase == "broken":
                    if i - b > cfg.retest_window_bars:
                        phase, b = "pre", None
                        continue
                    probe = bar.low if direction == LONG else bar.high
                    if s * (probe - (out_edge + s * cfg.retest_tolerance_atr * atr)) <= 0:
                        phase, r = "retested", i
                    continue
                if phase == "retested":
                    rb = bars[r]
                    if i - r > cfg.confirm_window_bars:
                        phase, b, r = "pre", None, None
                        continue
                    extreme = rb.high if direction == LONG else rb.low
                    if s * (bar.close - extreme) > 0:
                        lows = [x.low if direction == LONG else x.high for x in bars[r:i + 1]]
                        far = min(lows + [in_edge]) if direction == LONG else max(lows + [in_edge])
                        bb = bars[b]
                        rv = tfd.relvol[b]
                        retest_px = rb.low if direction == LONG else rb.high
                        conds = [
                            Condition(f"Confirmed zone: {label}", True, True,
                                      f"{z['low']:.2f}-{z['high']:.2f}"),
                            Condition("Candle closed beyond the zone", True, True,
                                      f"{fmt(bb.time)} close {bb.close:.2f} vs edge {out_edge:.2f} "
                                      f"+ {cfg.breakout_buffer_atr} ATR", bb.time),
                            Condition(f"Breakout relative volume >= {cfg.min_breakout_rel_volume}",
                                      None if not tfd.volume_available or rv is None
                                      else rv >= cfg.min_breakout_rel_volume, True,
                                      "volume unavailable on this feed"
                                      if not tfd.volume_available or rv is None
                                      else f"{rv:.2f}x same-time average", bb.time),
                            Condition("Retest of the broken edge", True, True,
                                      f"{fmt(rb.time)} {'low' if direction == LONG else 'high'} "
                                      f"{retest_px:.2f} within {cfg.retest_tolerance_atr} ATR of "
                                      f"{out_edge:.2f}", rb.time),
                            Condition("Entry confirmation: close beyond the retest candle",
                                      True, True, f"{fmt(bar.time)} close {bar.close:.2f} vs "
                                      f"{extreme:.2f}", bar.time),
                        ]
                        ev = [{"time": bb.time, "kind": "breakout", "price": bb.close},
                              {"time": rb.time, "kind": "retest", "price": retest_px},
                              {"time": bar.time, "kind": "confirmation", "price": bar.close}]
                        inv = [f"A close {'below' if direction == LONG else 'above'} {in_edge:.2f} "
                               f"(back through {label}) invalidates the breakout."]
                        trig = ref(extreme, f"{tfd.tf} close {'above' if direction == LONG else 'below'} "
                                            f"the retest candle {'high' if direction == LONG else 'low'} "
                                            f"{extreme:.2f} ({fmt(rb.time)})", "retest", tfd.tf)
                        anchor = ref(far, f"Beyond the retest {'low' if direction == LONG else 'high'} "
                                          f"and the far edge of {label} ({far:.2f})", "zone", tfd.tf)
                        res.triggers.append(accept(ctx, tfd, i, direction, trig, anchor, conds,
                                                   ev, [level_ref(z["level"])], inv,
                                                   strategy_id=self.id))
                        phase, b, r = "pre", None, None
            # Developing state at the latest candle.
            if phase in ("broken", "retested"):
                t = bars[r if phase == "retested" else b].time
                if latest_watch is None or t > latest_watch["time"]:
                    bb = bars[b]
                    pending = ("retest of the broken edge" if phase == "broken" else
                               "a close beyond the retest candle")
                    if phase == "retested":
                        rb_ = bars[r]
                        rx = rb_.high if direction == LONG else rb_.low
                        fx = min(rb_.low, in_edge) if direction == LONG else max(rb_.high, in_edge)
                    else:
                        rx, fx = out_edge, in_edge
                    latest_watch = {
                        "time": t,
                        "reasons": [f"{'Broke above' if direction == LONG else 'Broke below'} "
                                    f"{label} at {fmt(bb.time)} (close {bb.close:.2f}); waiting for "
                                    f"{pending}."],
                        "conditions": [
                            Condition("Candle closed beyond the zone", True, True,
                                      f"{fmt(bb.time)} close {bb.close:.2f}", bb.time),
                            Condition("Retest of the broken edge", phase == "retested", True,
                                      "done" if phase == "retested" else
                                      f"within {cfg.retest_window_bars} candles"),
                            Condition("Entry confirmation", False, True, "pending"),
                        ],
                        "levels": [level_ref(z["level"])],
                        "invalidation": [f"A close {'below' if direction == LONG else 'above'} "
                                         f"{in_edge:.2f} cancels the setup."],
                        # Conditional plan: retested -> close beyond the retest candle;
                        # broken -> priced at the broken edge, where the retest is expected.
                        "trigger": ref(rx, f"{tfd.tf} close {'above' if direction == LONG else 'below'} "
                                           f"{'the retest candle extreme' if phase == 'retested' else 'the broken edge after a retest'} "
                                           f"{rx:.2f}", "retest", tfd.tf),
                        "stop_anchor": ref(fx, f"Far edge of {label} / retest extreme ({fx:.2f})",
                                           "zone", tfd.tf),
                    }
        res.watch = latest_watch
        if not res.triggers and not latest_watch:
            res.reasons.append(f"No completed candle closed {'above' if direction == LONG else 'below'} "
                               f"a confirmed zone with the required follow-through this session.")
        res.reasons += weak_volume[-2:]
        return res
