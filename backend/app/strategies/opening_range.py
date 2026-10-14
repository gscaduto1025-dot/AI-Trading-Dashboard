"""Strategy 2: Opening range breakout (ORB).

 1. Opening range: the first range_minutes (15 or 30) of the regular session, built from
    the strategy timeframe's candles (the range length must be a multiple of the candle
    length and every candle must be present). ORH = max high, ORL = min low.
 2. Range sanity: min_range_daily_atr <= (ORH - ORL) / daily ATR <= max_range_daily_atr.
 3. Breakout: the first completed candle after the range that closes beyond ORH (LONG) or
    ORL (SHORT) by breakout_buffer_atr x ATR, with relative volume (vs the same time of day
    in prior sessions) >= min_rel_volume, no later than latest_breakout_time. Closes beyond
    the range on weak volume are recorded but do not decide the direction.
 4. Entry = the breakout candle's close. Stop = range midpoint (stop_mode "midpoint") or the
    opposite side ("opposite"), minus stop_buffer_atr x ATR.
 5. Targets: key levels or range-multiple measured moves (measured_move_multiples).
 6. Retest: after the trigger, whether price came back to the broken edge is reported.
    Invalidation: a close back inside the range, or the stop.
"""

from __future__ import annotations

from ..market.timeframes import spec
from .base import Strategy, StrategyResult, accept, ref, sgn
from .common import LONG, Condition, StrategyContext, TFData, fmt, hhmm_to_ts


class OpeningRangeBreakout(Strategy):
    id = "opening_range_breakout"
    name = "Opening range breakout"
    config_key = "opening_range_breakout"

    @staticmethod
    def _anchor(orh, orl, direction, cfg, tfd):
        if cfg.stop_mode == "midpoint":
            return ref((orh + orl) / 2, f"Opening-range midpoint {(orh + orl) / 2:.2f}", "range",
                       tfd.tf)
        p = orl if direction == LONG else orh
        return ref(p, f"Opposite side of the opening range "
                      f"({'ORL' if direction == LONG else 'ORH'} {p:.2f})", "range", tfd.tf)

    def evaluate(self, ctx: StrategyContext, tfd: TFData, direction: str) -> StrategyResult:
        cfg = ctx.config.opening_range_breakout
        s = sgn(direction)
        res = StrategyResult()
        bars = tfd.bars
        minutes = spec(tfd.tf).minutes
        R = cfg.range_minutes
        if R % minutes:
            res.reasons.append(f"A {R}-minute range cannot be built from {tfd.tf} candles.")
            return res
        open_ts = int(ctx.session.open.timestamp())
        or_end = open_ts + R * 60
        need = R // minutes
        sess = list(tfd.session)
        rng = [i for i in sess if bars[i].time < or_end]
        complete = len(rng) == need and rng and bars[rng[0]].time == open_ts and \
            tfd.end[rng[-1]] == or_end
        if not complete:
            if ctx.session_open and (not sess or tfd.end[sess[-1]] < or_end):
                res.watch = {"time": open_ts, "reasons": [
                    f"Opening range forming until {fmt(or_end)} ET ({R} minutes)."],
                    "conditions": [Condition(f"{R}-minute opening range complete", False, True,
                                             f"{len(rng)} of {need} candles")],
                    "levels": [], "invalidation": []}
            else:
                res.reasons.append(f"Opening range incomplete: {len(rng)} of {need} {tfd.tf} "
                                   f"candles from 09:30.")
            return res
        orh = max(bars[i].high for i in rng)
        orl = min(bars[i].low for i in rng)
        width = orh - orl
        datr = ctx.daily_atr or width
        ratio = width / datr if datr else 0
        or_level = {"label": f"{R}-min opening range", "type": "OPENING_RANGE", "low": orl,
                    "high": orh, "timeframe": tfd.tf, "strength": None}
        if ratio < cfg.min_range_daily_atr:
            res.reasons.append(f"Opening range {width:.2f} is only {ratio:.2f} daily ATR "
                               f"(< {cfg.min_range_daily_atr}); too narrow to trade.")
            return res
        if ratio > cfg.max_range_daily_atr:
            res.reasons.append(f"Opening range {width:.2f} is {ratio:.2f} daily ATR "
                               f"(> {cfg.max_range_daily_atr}): abnormal, risk too wide.")
            return res
        cutoff = hhmm_to_ts(ctx, cfg.latest_breakout_time)
        edge = orh if direction == LONG else orl
        weak: list[str] = []
        first_dir, first_i = None, None
        for i in sess:
            if i <= rng[-1]:
                continue
            b = bars[i]
            if b.time > cutoff:
                break
            atr = tfd.atr_before(i)
            up = b.close > orh + cfg.breakout_buffer_atr * atr
            down = b.close < orl - cfg.breakout_buffer_atr * atr
            if not (up or down):
                continue
            vol = tfd.volume(i)
            if vol.rejects(cfg.min_rel_volume):
                weak.append(f"{fmt(b.time)} close beyond the range on relative volume "
                            f"{vol.rv:.2f} < {cfg.min_rel_volume} ({vol.level}, {vol.source}; "
                            f"not a confirmed breakout)")
                continue
            first_dir, first_i = (LONG if up else "SHORT"), i
            break
        if first_i is None:
            last_t = bars[sess[-1]].time
            if ctx.session_open and last_t < cutoff:
                res.watch = {"time": bars[rng[-1]].time,
                             "reasons": [f"Range {orl:.2f}-{orh:.2f} set; waiting for a close "
                                         f"{'above' if direction == LONG else 'below'} {edge:.2f} "
                                         f"before {cfg.latest_breakout_time} ET."] + weak[-1:],
                             "conditions": [Condition(f"{R}-minute opening range complete", True,
                                                      True, f"ORH {orh:.2f} / ORL {orl:.2f}"),
                                            Condition("Confirmed breakout", False, True, "pending")],
                             "levels": [or_level], "entry": edge,
                             "invalidation": [f"No breakout by {cfg.latest_breakout_time} ET."],
                             "trigger": ref(edge, f"{tfd.tf} close {'above ORH' if direction == LONG else 'below ORL'} "
                                                  f"{edge:.2f} (+{cfg.breakout_buffer_atr} ATR) before "
                                                  f"{cfg.latest_breakout_time} ET", "opening_range", tfd.tf),
                             "stop_anchor": self._anchor(orh, orl, direction, cfg, tfd),
                             "extra": [(edge + s * m * (orh - orl), f"{m:g}x opening-range measured move")
                                       for m in cfg.measured_move_multiples]}
            else:
                res.reasons.append(f"No confirmed opening-range breakout before "
                                   f"{cfg.latest_breakout_time} ET.")
            res.reasons += weak[-2:]
            return res
        if first_dir != direction:
            res.reasons.append(f"The first confirmed breakout was "
                               f"{'up' if first_dir == LONG else 'down'} at {fmt(bars[first_i].time)}.")
            return res
        i = first_i
        b = bars[i]
        atr = tfd.atr_before(i)
        extra = [(b.close + s * m * width, f"{m:g}x opening-range measured move")
                 for m in cfg.measured_move_multiples]
        vol = tfd.volume(i)
        conds = [
            Condition(f"{R}-minute opening range complete", True, True,
                      f"ORH {orh:.2f} / ORL {orl:.2f} ({ratio:.2f} daily ATR)"),
            Condition("First confirmed close beyond the range", True, True,
                      f"{fmt(b.time)} close {b.close:.2f} vs {edge:.2f} + "
                      f"{cfg.breakout_buffer_atr} ATR", b.time),
            Condition(f"Relative volume >= {cfg.min_rel_volume}",
                      vol.passed(cfg.min_rel_volume), True, vol.detail("same time of day"),
                      b.time, vol.level),
            Condition(f"Breakout before {cfg.latest_breakout_time} ET", True, True, fmt(b.time)),
        ]
        retest = next((bars[j] for j in range(i + 1, len(bars))
                       if s * ((bars[j].low if direction == LONG else bars[j].high) - edge) <= 0
                       and s * (bars[j].close - edge) >= 0), None)
        ev = [{"time": bars[rng[0]].time, "kind": "range start", "price": bars[rng[0]].open},
              {"time": b.time, "kind": "breakout", "price": b.close}]
        if retest:
            ev.append({"time": retest.time, "kind": "retest held (informational)",
                       "price": retest.low if direction == LONG else retest.high})
        anchor = self._anchor(orh, orl, direction, cfg, tfd)
        inv = [f"A close back inside the range ({'below' if direction == LONG else 'above'} "
               f"{edge:.2f}) weakens the breakout; a move through {anchor.price:.2f} "
               f"({cfg.stop_mode} of the range) invalidates it."]
        trig = ref(edge, f"{tfd.tf} close {'above ORH' if direction == LONG else 'below ORL'} "
                         f"{edge:.2f} by {cfg.breakout_buffer_atr} ATR", "opening_range", tfd.tf)
        res.triggers.append(accept(ctx, tfd, i, direction, trig, anchor, conds, ev, [or_level],
                                   inv, extra, strategy_id=self.id))
        res.reasons += weak[-1:]
        return res
