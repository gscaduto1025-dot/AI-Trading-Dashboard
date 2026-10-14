"""Key-level engine: orchestrates the detectors across timeframes, point-in-time.

compute_key_levels() receives candles per timeframe and an `as_of` instant. Before anything
else, every candle that had not closed by `as_of` is dropped, so the result is exactly what
could have been computed at that moment (no look-ahead). The same function serves live use
(as_of = the feed's data cutoff) and historical replay/backtests.
"""

from __future__ import annotations

from datetime import datetime

from ..analysis import indicators as ind
from ..data.base import Bar
from ..market.calendar import MarketCalendar
from ..market.timeframes import bar_end, spec
from . import detect as d
from .model import (BREAKOUT_SCAN_BARS, EMA_PERIODS, LIQUIDITY_PER_SIDE, METHOD_VERSION,
                    MODE_BREAKOUT_TF,
                    MODE_EMA_TFS, MODE_FRAMES, MODE_SWING_TFS, MODE_ZONE_TFS, NY, STRENGTH_NOTE,
                    SWING_K, TF_LABEL, TF_WEIGHT, ZONE_LOOKBACK, KeyLevel, score_levels)

INTRADAY_ORDER = ["1Min", "5Min", "15Min", "30Min", "1Hour", "4Hour"]


class InsufficientData(ValueError):
    pass


def _end_fn(tf: str, cal: MarketCalendar):
    s = spec(tf)
    return lambda b: bar_end(b, s, cal, "regular")


def truncate_point_in_time(frames: dict[str, list[Bar]], as_of_ts: int,
                           cal: MarketCalendar) -> dict[str, list[Bar]]:
    """Keep only candles that had closed at `as_of` (sorted, oldest first)."""
    out = {}
    for tf, bars in frames.items():
        end = _end_fn(tf, cal)
        out[tf] = [b for b in sorted(bars, key=lambda b: b.time) if end(b) <= as_of_ts]
    return out


def current_trading_day(as_of: datetime, cal: MarketCalendar):
    """The session levels are computed *for*: today while today's extended session is
    still running (04:00-20:00 ET, including before the open), otherwise the next one."""
    day = as_of.astimezone(NY).date()
    sd = cal.day(day)
    if sd is not None and as_of < sd.session_close:
        return day
    return cal.next_trading_day(day)


def compute_key_levels(ticker: str, mode: str, frames: dict[str, list[Bar]], as_of: datetime,
                       cal: MarketCalendar, chart_tf: str | None = None,
                       chart_bars: list[Bar] | None = None,
                       adjustment: str | None = None) -> dict:
    if mode not in MODE_FRAMES:
        raise ValueError(f"mode must be one of {list(MODE_FRAMES)}")
    as_ts = int(as_of.timestamp())
    warnings: list[str] = []
    notes: list[str] = []
    frames = truncate_point_in_time(frames, as_ts, cal)
    day_bars = frames.get("1Day") or []
    if len(day_bars) < 30:
        raise InsufficientData("At least 30 completed daily candles are required.")

    # --- split guard: unadjusted history across a split would produce bogus levels.
    if adjustment == "raw":
        warnings.append("Prices are unadjusted (ALPACA_ADJUSTMENT=raw); levels spanning a "
                        "split or large dividend would be wrong. Use 'split' or 'all'.")
    splits = d.detect_unadjusted_splits(day_bars)
    if splits:
        cut = splits[-1]["time"]
        warnings.append(f"Suspected unadjusted {splits[-1]['ratio']} split on "
                        f"{splits[-1]['date']}; only candles from that date on are used.")
        frames = {tf: [b for b in bars if b.time >= cut] for tf, bars in frames.items()}
        day_bars = frames["1Day"]

    end_of = {tf: _end_fn(tf, cal) for tf in frames}
    # Last completed price: the most recently closed candle across the frames.
    latest_tf = max((tf for tf in frames if frames[tf]), key=lambda tf: end_of[tf](frames[tf][-1]))
    price = frames[latest_tf][-1].close
    daily_atr = d.atr(day_bars)

    today = current_trading_day(as_of, cal)
    prev_day = cal.previous_trading_day(today)
    prev_sd, today_sd = cal.day(prev_day), cal.day(today)
    intraday = {tf: frames[tf] for tf in INTRADAY_ORDER if frames.get(tf)}
    finest = next(iter(intraday), None)

    levels: list[KeyLevel] = []

    # 1-2. previous day high/low
    current_bars = d.session_bars(frames[finest], today) if finest else []
    levels += d.previous_day_levels(day_bars, intraday, prev_day,
                                    int(prev_sd.open.timestamp()), int(prev_sd.close.timestamp()),
                                    end_of, current_bars)
    # 3-4. previous week high/low (from daily candles)
    levels += d.previous_week_levels(day_bars, today,
                                     lambda days: end_of["1Day"](days[-1]))
    # 5-7. session open and opening range; 14. session VWAP (day trading)
    if mode == "day" and finest:
        minutes = spec(finest).minutes
        sess_levels, sess_notes = d.session_open_and_range(
            frames[finest], finest, minutes, today, int(today_sd.open.timestamp()), as_ts)
        levels += sess_levels
        notes += sess_notes
        vwap = d.session_vwap_level(frames[finest], finest, today,
                                    int(today_sd.close.timestamp()), as_ts)
        if vwap:
            levels.append(vwap)
    # 15-18. EMAs
    for tf in MODE_EMA_TFS[mode]:
        if frames.get(tf):
            periods = EMA_PERIODS if tf != "1Day" or mode == "swing" else (20, 50, 200)
            levels += d.ema_levels(frames[tf], tf, periods)

    # 12-13. confirmed swings (and the pivots used for zones, liquidity and scoring)
    swings_by_tf: dict[str, list] = {}
    for tf in set(MODE_SWING_TFS[mode]) | set(MODE_ZONE_TFS[mode]):
        bars = frames.get(tf) or []
        if tf in ZONE_LOOKBACK:
            bars = bars[-ZONE_LOOKBACK[tf]:]
            frames[tf] = bars
        swings_by_tf[tf] = d.find_swings(bars, SWING_K[tf], tf, end_of[tf]) if bars else []
    for tf in MODE_SWING_TFS[mode]:
        if frames.get(tf):
            levels += d.swing_levels(frames[tf], swings_by_tf[tf], tf)

    # 8-11. support/resistance zones, merged across timeframes
    zones = []
    for tf in MODE_ZONE_TFS[mode]:
        if frames.get(tf):
            zones += d.find_zones(frames[tf], swings_by_tf[tf], tf)
    zone_levels = d.merge_zones(zones, price, TF_WEIGHT)
    levels += zone_levels

    # 19. liquidity areas: nearest few per side across timeframes; where areas from
    # different timeframes overlap, the higher timeframe's is kept.
    liquidity: list[KeyLevel] = []
    for tf in MODE_SWING_TFS[mode]:
        if frames.get(tf):
            liquidity += d.liquidity_levels(frames[tf], swings_by_tf[tf], tf, price)
    kept: list[KeyLevel] = []
    for lq in sorted(liquidity, key=lambda l: -TF_WEIGHT[l.timeframe]):
        if all(lq.high < k.low or lq.low > k.high for k in kept):
            kept.append(lq)
    above = sorted((l for l in kept if l.low >= price), key=lambda l: l.low)
    below = sorted((l for l in kept if l.high <= price), key=lambda l: -l.high)
    levels += above[:LIQUIDITY_PER_SIDE] + below[:LIQUIDITY_PER_SIDE]

    # 20. confirmed breakouts/breakdowns on the mode's trigger timeframe
    bo_tf = MODE_BREAKOUT_TF[mode]
    if frames.get(bo_tf):
        cands = [d.BreakoutCandidate(l.type, l.low, l.high, l.formed_at)
                 for l in levels if l.type in ("PDH", "PDL", "PWH", "PWL", "ORH", "ORL")]
        for zl, z in _zone_sources(zone_levels, zones):
            cands.append(d.BreakoutCandidate(zl.label, zl.low, zl.high, zl.formed_at, z))
        levels += d.find_breakouts(frames[bo_tf], bo_tf, end_of[bo_tf], cands,
                                   BREAKOUT_SCAN_BARS.get(bo_tf, 20))

    all_swings = [s for v in swings_by_tf.values() for s in v]
    score_levels(levels, all_swings, daily_atr, as_ts)
    for lvl in levels:
        lvl.side = "resistance" if lvl.low > price else "support" if lvl.high < price else "at price"

    levels.sort(key=lambda l: -l.price)
    report = {
        "ticker": ticker,
        "mode": mode,
        "method_version": METHOD_VERSION,
        "calculated_at": as_of.astimezone(NY).isoformat(),
        "current_session": today.isoformat(),
        "previous_session": prev_day.isoformat(),
        "last_price": price,
        "last_price_timeframe": latest_tf,
        "timeframes_used": {tf: {"candles": len(b), "from": _iso(b[0].time) if b else None,
                                 "to": _iso(b[-1].time) if b else None,
                                 "label": TF_LABEL.get(tf, tf)}
                            for tf, b in frames.items()},
        "atr": {tf: round(d.atr(b), 4) for tf, b in frames.items() if len(b) > 1},
        "adjustment": adjustment,
        "splits_detected": splits,
        "warnings": warnings,
        "notes": notes,
        "strength_note": STRENGTH_NOTE,
        "levels": [l.to_dict(ticker, as_of) for l in levels],
    }
    if chart_tf and chart_bars:
        report["indicators"] = indicator_series(
            [b for b in chart_bars if _end_fn(chart_tf, cal)(b) <= as_ts], chart_tf)
    return report


def _zone_sources(zone_levels: list[KeyLevel], zones) -> list:
    """Pair merged zone levels with the pivots of their constituent zones."""
    out = []
    for zl in zone_levels:
        pivots = [p for z in zones if z.low <= zl.high and z.high >= zl.low for p in z.pivots]
        out.append((zl, pivots))
    return out


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, NY).isoformat()


def indicator_series(bars: list[Bar], tf: str) -> dict[str, list]:
    """Drawing data for the chart timeframe: EMA 9/20/50/200, session VWAP (intraday
    <= 1 hour) and confirmed swing points (for markers)."""
    closes = [b.close for b in bars]
    out: dict[str, list] = {}
    cal_free_end = (lambda b: b.time + spec(tf).minutes * 60)  # markers only need ordering
    out["swings"] = [{"time": s.time, "price": round(s.price, 4), "kind": s.kind}
                     for s in d.find_swings(bars, SWING_K.get(tf, 3), tf, cal_free_end)]
    for p in EMA_PERIODS:
        out[f"ema{p}"] = [{"time": b.time, "value": round(v, 4)}
                          for b, v in zip(bars, ind.ema(closes, p)) if v is not None]
    if spec(tf).intraday and spec(tf).minutes <= 60:
        out["vwap"] = [{"time": b.time, "value": round(v, 4)}
                       for b, v in zip(bars, ind.session_vwap(bars)) if v is not None]
    return out
