"""Strategy 3: Liquidity sweep reversal.

SHORT (sweep of a high; LONG mirrors with lows):
 1. Level: a previously confirmed high - PDH, PWH, a valid swing high or buy-side liquidity
    area known at the open, or an intraday swing high confirmed earlier in the session.
 2. Sweep: a candle trades above the level by >= min_penetration_atr x ATR but no more than
    max_penetration_atr x ATR (deeper = a breakout, the level is retired).
 3. Return: within return_window_bars of the sweep candle (including it), a candle closes
    back below the level.
 4. Reversal confirmation (completed candle): that close is below the level, the candle is
    bearish (close < open) and closes in the lower confirm_close_position share of its range.
 5. Entry = confirmation close. Stop = the sweep extreme + stop_buffer_atr x ATR.
"""

from __future__ import annotations

from ..levels.detect import find_swings
from ..levels.model import SWING_K
from .base import Strategy, StrategyResult, accept, sgn
from .common import (LONG, Condition, StrategyContext, TFData, candle_metrics, fmt,
                     level_ref)

HIGH_TYPES = ("PDH", "PWH", "SWING_HIGH")
LOW_TYPES = ("PDL", "PWL", "SWING_LOW")


def sweep_levels(ctx: StrategyContext, tfd: TFData, direction: str) -> list[dict]:
    """Levels with the first candle index they may be swept on (`from`). Levels known at the
    open are usable from the session's first candle; intraday swings only once confirmed and
    at least min_level_age_bars old."""
    want_high = direction != LONG
    out = []
    for l in ctx.levels:
        if not l["valid"]:
            continue
        if l["type"] in (HIGH_TYPES if want_high else LOW_TYPES):
            out.append({"price": l["price"], "label": l["label"], "ref": level_ref(l), "from": tfd.start})
        elif l["type"] == "LIQUIDITY" and l["label"].startswith("Buy" if want_high else "Sell"):
            p = l["low"] if want_high else l["high"]
            out.append({"price": p, "label": l["label"], "ref": level_ref(l), "from": tfd.start})
    cfg = ctx.config.liquidity_sweep
    k = SWING_K.get(tfd.tf, 3)
    swings = find_swings(tfd.bars, k, tfd.tf, lambda b: b.time) \
        if cfg.include_intraday_swings else []
    for sw in swings:
        if sw.index < tfd.start or sw.kind != ("high" if want_high else "low"):
            continue
        label = f"intraday {tfd.tf} swing {sw.kind} {fmt(sw.time)}"
        out.append({"price": sw.price, "label": label,
                    "ref": {"label": label, "type": f"SWING_{sw.kind.upper()}", "low": sw.price,
                            "high": sw.price, "timeframe": tfd.tf, "strength": None},
                    # usable once confirmed (index + k closed) and old enough
                    "from": max(sw.index + k + 1, sw.index + cfg.min_level_age_bars)})
    # De-duplicate near-identical prices (keep the first label, mention the others).
    out.sort(key=lambda x: (x["price"], x["from"]))
    merged: list[dict] = []
    for lv in out:
        if merged and abs(lv["price"] - merged[-1]["price"]) < 1e-6 and lv["from"] == merged[-1]["from"]:
            merged[-1]["label"] += f" / {lv['label']}"
            continue
        merged.append(lv)
    return merged


class LiquiditySweepReversal(Strategy):
    id = "liquidity_sweep_reversal"
    name = "Liquidity sweep reversal"
    config_key = "liquidity_sweep"

    def evaluate(self, ctx: StrategyContext, tfd: TFData, direction: str) -> StrategyResult:
        cfg = ctx.config.liquidity_sweep
        s = sgn(direction)  # LONG: sweep of lows (+1), SHORT: sweep of highs (-1)
        res = StrategyResult()
        bars = tfd.bars
        sess = list(tfd.session)
        if not sess:
            res.reasons.append("No completed candles in the session yet.")
            return res
        levels = sweep_levels(ctx, tfd, direction)
        if not levels:
            res.reasons.append("No confirmed swing highs/lows to sweep.")
            return res
        watch = None
        retired: list[str] = []
        for lv in levels:
            L = lv["price"]
            swept_i, extreme, done = None, None, False
            for i in sess:
                if done or i < lv["from"]:
                    continue
                b = bars[i]
                atr = tfd.atr_before(i)
                beyond = (L - b.low) if direction == LONG else (b.high - L)  # penetration
                if swept_i is None:
                    if beyond >= cfg.min_penetration_atr * atr:
                        if beyond > cfg.max_penetration_atr * atr:
                            retired.append(f"{lv['label']} {L:.2f} broken by {beyond:.2f} "
                                           f"(> {cfg.max_penetration_atr} ATR) at {fmt(b.time)}: "
                                           f"a breakout, not a sweep")
                            done = True
                            continue
                        swept_i, extreme = i, (b.low if direction == LONG else b.high)
                    else:
                        continue
                else:
                    extreme = min(extreme, b.low) if direction == LONG else max(extreme, b.high)
                    if abs(extreme - L) > cfg.max_penetration_atr * atr:
                        retired.append(f"{lv['label']} {L:.2f}: move extended beyond "
                                       f"{cfg.max_penetration_atr} ATR after the sweep")
                        done = True
                        continue
                    if i - swept_i >= cfg.return_window_bars:
                        retired.append(f"{lv['label']} {L:.2f}: no close back inside within "
                                       f"{cfg.return_window_bars} candles")
                        done = True
                        continue
                m = candle_metrics(b)
                back_inside = s * (b.close - L) > 0
                reversal = (b.close > b.open and m["close_pos"] >= 1 - cfg.confirm_close_position) \
                    if direction == LONG else \
                    (b.close < b.open and m["close_pos"] <= cfg.confirm_close_position)
                if back_inside and reversal:
                    sb = bars[swept_i]
                    stop = extreme - s * ctx.config.targets.stop_buffer_atr * atr
                    conds = [
                        Condition(f"Previously confirmed level: {lv['label']}", True, True,
                                  f"{L:.2f}"),
                        Condition("Price traded beyond the level", True, True,
                                  f"{fmt(sb.time)} {'low' if direction == LONG else 'high'} "
                                  f"{extreme:.2f} ({abs(extreme - L):.2f} beyond, "
                                  f"{cfg.min_penetration_atr}-{cfg.max_penetration_atr} ATR)",
                                  sb.time),
                        Condition("Close back inside the prior range", True, True,
                                  f"{fmt(b.time)} close {b.close:.2f} "
                                  f"{'above' if direction == LONG else 'below'} {L:.2f}", b.time),
                        Condition("Completed reversal candle", True, True,
                                  f"{'bullish' if direction == LONG else 'bearish'} body, close "
                                  f"at {m['close_pos'] * 100:.0f}% of range", b.time),
                    ]
                    ev = [{"time": sb.time, "kind": "sweep", "price": extreme},
                          {"time": b.time, "kind": "reversal close", "price": b.close}]
                    vw = tfd.vwap[i]
                    extra = [(vw, "session VWAP")] if vw is not None else []
                    inv = [f"Stop {stop:.2f} beyond the sweep extreme {extreme:.2f}.",
                           f"A close back {'below' if direction == LONG else 'above'} {L:.2f} "
                           f"negates the reversal."]
                    res.triggers.append(accept(ctx, tfd, i, direction, b.close, stop, conds, ev,
                                               [lv["ref"]], inv, extra))
                    done = True
            # Developing state.
            last = bars[sess[-1]]
            if swept_i is not None and not done:
                t = bars[swept_i].time
                if watch is None or t > watch["time"]:
                    watch = {"time": t, "reasons": [
                        f"{lv['label']} {L:.2f} swept at {fmt(t)} (extreme {extreme:.2f}); waiting "
                        f"for a reversal candle closing back {'above' if direction == LONG else 'below'} it."],
                        "conditions": [Condition("Price traded beyond the level", True, True, fmt(t)),
                                       Condition("Close back inside + reversal candle", False, True,
                                                 "pending")],
                        "levels": [lv["ref"]], "invalidation": []}
            elif swept_i is None and not done:
                dist = (last.low - L) if direction == LONG else (L - last.high)
                atr = tfd.atr_before(sess[-1])
                if 0 <= dist <= cfg.watch_distance_atr * atr:
                    if watch is None:
                        watch = {"time": last.time, "reasons": [
                            f"Price within {dist:.2f} of {lv['label']} {L:.2f}; watching for a sweep "
                            f"and reversal."], "conditions": [
                            Condition("Price traded beyond the level", False, True, "pending")],
                            "levels": [lv["ref"]], "invalidation": []}
        res.watch = watch
        if not res.triggers and not watch:
            res.reasons.append(f"No confirmed {'low' if direction == LONG else 'high'} was swept "
                               f"and reclaimed with a reversal candle this session.")
        res.reasons += retired[-2:]
        return res
