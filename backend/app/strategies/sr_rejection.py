"""Strategy 5: Support/resistance rejection.

SHORT at resistance (LONG mirrors at support):
 1. Zone: a valid key-level zone with strength >= min_zone_strength, or PDH/PWH widened to
    +/- reference_zone_half_width_atr (breakout_retest setting), lying above the prior close.
 2. Entry into the zone: a candle's high reaches zone.low while its close stays at or below
    zone.high.
 3. Objective rejection candle (either):
    a. Pin bar: upper wick >= min_wick_to_body x body, upper wick >= min_wick_share of the
       range, close in the lower max_close_position of the range; or
    b. Bearish engulfing (allow_engulfing): bearish candle whose body engulfs the prior
       bullish candle's body, with its high inside the zone.
 4. Direction confirmation: within confirm_window_bars, a candle closes below the rejection
    candle's low. Entry = that close.
 5. Invalidation: a close above zone.high before or after the trigger. Stop = max(rejection
    high, zone.high) + stop_buffer_atr x ATR.
"""

from __future__ import annotations

from .base import Strategy, StrategyResult, accept, sgn
from .breakout_retest import REF_TYPES
from .common import LONG, Condition, StrategyContext, TFData, candle_metrics, fmt, level_ref


class SRRejection(Strategy):
    id = "sr_rejection"
    name = "Support/resistance rejection"
    config_key = "sr_rejection"

    def _zones(self, ctx: StrategyContext, tfd: TFData, direction: str) -> list[dict]:
        cfg = ctx.config
        half = cfg.breakout_retest.reference_zone_half_width_atr
        atr0 = tfd.atr_before(tfd.start) if tfd.start < len(tfd.bars) else 0.0
        want = "SUPPORT_ZONE" if direction == LONG else "RESISTANCE_ZONE"
        out = [{"low": z["low"], "high": z["high"], "level": z}
               for z in ctx.zones(cfg.sr_rejection.min_zone_strength) if z["type"] == want]
        refs = ("PDL", "PWL") if direction == LONG else ("PDH", "PWH")
        for l in ctx.reference_levels(tuple(t for t in REF_TYPES if t in refs)):
            out.append({"low": l["price"] - half * atr0, "high": l["price"] + half * atr0,
                        "level": l})
        return out

    def _rejection(self, b, prev, direction: str, zone: dict, cfg) -> str | None:
        m = candle_metrics(b)
        if direction == LONG:
            wick, close_ok = m["lower"], m["close_pos"] >= 1 - cfg.max_close_position
        else:
            wick, close_ok = m["upper"], m["close_pos"] <= cfg.max_close_position
        if wick >= cfg.min_wick_to_body * max(m["body"], 1e-9) and \
                wick >= cfg.min_wick_share * m["range"] and close_ok:
            return (f"pin bar: wick {wick:.2f} = {wick / max(m['body'], 1e-9):.1f}x body, "
                    f"{wick / m['range'] * 100:.0f}% of range, close at "
                    f"{m['close_pos'] * 100:.0f}% of range")
        if cfg.allow_engulfing and prev is not None:
            if direction == LONG:
                eng = b.close > b.open and prev.close < prev.open and \
                    b.close >= prev.open and b.open <= prev.close and b.low <= zone["high"]
            else:
                eng = b.close < b.open and prev.close > prev.open and \
                    b.open >= prev.close and b.close <= prev.open and b.high >= zone["low"]
            if eng:
                return "engulfing candle"
        return None

    def evaluate(self, ctx: StrategyContext, tfd: TFData, direction: str) -> StrategyResult:
        cfg = ctx.config.sr_rejection
        s = sgn(direction)
        res = StrategyResult()
        bars = tfd.bars
        sess = list(tfd.session)
        if not sess:
            res.reasons.append("No completed candles in the session yet.")
            return res
        zones = self._zones(ctx, tfd, direction)
        if not zones:
            res.reasons.append(f"No confirmed {'support' if direction == LONG else 'resistance'} "
                               f"zone with strength >= {cfg.min_zone_strength}.")
            return res
        watch = None
        for z in zones:
            near = z["low"] if direction != LONG else z["high"]  # edge price enters through
            far = z["high"] if direction != LONG else z["low"]  # close beyond = invalidation
            label = z["level"]["label"]
            pending = None  # index of rejection candle awaiting confirmation
            for i in sess:
                b = bars[i]
                prev = bars[i - 1] if i > 0 else None
                if pending is not None:
                    rb = bars[pending]
                    if s * (b.close - far) < 0:  # closed through the zone: invalidated
                        pending = None
                        continue
                    extreme = rb.high if direction == LONG else rb.low
                    if s * (b.close - extreme) > 0:
                        atr = tfd.atr_before(i)
                        top = max(rb.high, far) if direction != LONG else min(rb.low, far)
                        stop = top - s * ctx.config.targets.stop_buffer_atr * atr
                        conds = [
                            Condition(f"Confirmed zone: {label}", True, True,
                                      f"{z['low']:.2f}-{z['high']:.2f}"),
                            Condition("Price entered the zone", True, True,
                                      f"{fmt(rb.time)} {'low' if direction == LONG else 'high'} "
                                      f"{(rb.low if direction == LONG else rb.high):.2f}", rb.time),
                            Condition("Objective rejection candle", True, True,
                                      self._rejection(rb, bars[pending - 1] if pending else None,
                                                      direction, z, cfg) or "", rb.time),
                            Condition("Direction confirmed", True, True,
                                      f"{fmt(b.time)} close {b.close:.2f} beyond rejection "
                                      f"{'high' if direction == LONG else 'low'} {extreme:.2f}",
                                      b.time),
                        ]
                        ev = [{"time": rb.time, "kind": "rejection", "price": rb.close},
                              {"time": b.time, "kind": "confirmation", "price": b.close}]
                        inv = [f"A close {'below' if direction == LONG else 'above'} {far:.2f} "
                               f"(through {label}) invalidates the rejection.",
                               f"Stop {stop:.2f}."]
                        vw = tfd.vwap[i]
                        res.triggers.append(accept(ctx, tfd, i, direction, b.close, stop, conds,
                                                   ev, [level_ref(z["level"])], inv,
                                                   [(vw, "session VWAP")] if vw else []))
                        pending = None
                        continue
                    if i - pending >= cfg.confirm_window_bars:
                        pending = None
                    else:
                        continue
                # Look for a rejection candle at the zone.
                if prev is None or s * (prev.close - near) <= 0:
                    continue  # must approach from outside the zone
                probe = b.low if direction == LONG else b.high
                entered = s * (probe - near) <= 0
                closed_ok = s * (b.close - far) >= 0
                if entered and closed_ok and self._rejection(b, prev, direction, z, cfg):
                    pending = i
            if pending is not None:
                rb = bars[pending]
                if watch is None or rb.time > watch["time"]:
                    watch = {"time": rb.time, "reasons": [
                        f"Rejection candle at {label} at {fmt(rb.time)}; waiting for a close "
                        f"{'above' if direction == LONG else 'below'} "
                        f"{(rb.high if direction == LONG else rb.low):.2f}."],
                        "conditions": [Condition("Objective rejection candle", True, True,
                                                 fmt(rb.time)),
                                       Condition("Direction confirmed", False, True, "pending")],
                        "levels": [level_ref(z["level"])], "invalidation": [
                            f"A close {'below' if direction == LONG else 'above'} {far:.2f}."]}
            else:
                last = bars[sess[-1]]
                probe = last.low if direction == LONG else last.high
                if s * (probe - near) <= 0 and s * (last.close - far) >= 0 and watch is None \
                        and ctx.session_open:
                    watch = {"time": last.time, "reasons": [
                        f"Price is inside {label} ({z['low']:.2f}-{z['high']:.2f}); watching for "
                        f"an objective rejection candle."],
                        "conditions": [Condition("Price entered the zone", True, True,
                                                 fmt(last.time)),
                                       Condition("Objective rejection candle", False, True,
                                                 "pending")],
                        "levels": [level_ref(z["level"])], "invalidation": []}
        res.watch = watch
        if not res.triggers and not watch:
            res.reasons.append(f"No objective rejection of a confirmed "
                               f"{'support' if direction == LONG else 'resistance'} zone this session.")
        return res
