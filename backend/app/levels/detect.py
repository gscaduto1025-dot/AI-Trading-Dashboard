"""Pure detection functions. Inputs are completed candles only, oldest first.

Point-in-time rule: every function sees only candles that had closed at the calculation
time (the caller truncates), and anything that needs future candles to be confirmed
(swing points, breakouts) records the moment it became confirmed.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..analysis import indicators as ind
from ..data.base import Bar
from .model import (BREAKOUT_BUFFER_ATR, EMA_WARMUP_MULTIPLE, EQUAL_EXTREME_ATR,
                    LIQUIDITY_BAND_ATR, LIQUIDITY_PER_SIDE, LIQUIDITY_SWEEP_LOOKBACK, NY,
                    OPENING_RANGE_MINUTES, SPLIT_RATIO_TOLERANCE, SPLIT_RATIOS, SWING_K,
                    SWING_LEVELS_PER_TF, TF_LABEL,
                    ZONE_INVALID_CROSSES, ZONE_JOIN_ATR, ZONE_MAX_SPAN_ATR, ZONE_MIN_REACTIONS,
                    ZONE_MIN_WIDTH_ATR, KeyLevel, Swing)

EndFn = Callable[[Bar], int]  # unix time a candle closed


def ny_date(ts: int) -> date:
    return datetime.fromtimestamp(ts, NY).date()


def fmt_t(ts: int) -> str:
    t = datetime.fromtimestamp(ts, NY)
    # Daily/weekly candles are stamped at midnight ET: show the date only.
    return t.strftime("%Y-%m-%d") if (t.hour, t.minute) == (0, 0) else t.strftime("%Y-%m-%d %H:%M ET")


def fmt_d(ts: int) -> str:
    return datetime.fromtimestamp(ts, NY).strftime("%Y-%m-%d")


def atr(bars: list[Bar], period: int = 14) -> float:
    value = ind.last(ind.atr(bars, period))
    if value is None:  # short history: mean true range of what exists
        rngs = [b.high - b.low for b in bars] or [0.0]
        value = sum(rngs) / len(rngs)
    return value


# ------------------------------------------------------------------------- swings

def find_swings(bars: list[Bar], k: int, tf: str, end_of: EndFn) -> list[Swing]:
    """Fractal swing points.

    Swing high at i: high[i] >  max(high[i-k .. i-1])  and  high[i] >= max(high[i+1 .. i+k])
    Swing low  at i: low[i]  <  min(low[i-k .. i-1])   and  low[i]  <= min(low[i+1 .. i+k])
    (strict on the left, non-strict on the right, so a flat double top yields its first bar).
    Confirmed when candle i+k closes; never reported earlier.
    """
    out: list[Swing] = []
    for i in range(k, len(bars) - k):
        left, right = bars[i - k:i], bars[i + 1:i + k + 1]
        b = bars[i]
        confirmed = end_of(bars[i + k])
        if b.high > max(x.high for x in left) and b.high >= max(x.high for x in right):
            out.append(Swing(tf, "high", i, b.time, b.high, confirmed))
        if b.low < min(x.low for x in left) and b.low <= min(x.low for x in right):
            out.append(Swing(tf, "low", i, b.time, b.low, confirmed))
    return out


def swing_levels(bars: list[Bar], swings: list[Swing], tf: str) -> list[KeyLevel]:
    """The most recent confirmed swing highs and lows, with whether they still stand."""
    levels = []
    for kind in ("high", "low"):
        recent = [s for s in swings if s.kind == kind][-SWING_LEVELS_PER_TF:]
        for s in recent:
            later = bars[s.index + 1:]
            breaker = next((b for b in later if (b.high > s.price if kind == "high"
                                                 else b.low < s.price)), None)
            status = (f"taken out {fmt_t(breaker.time)}" if breaker else "intact")
            levels.append(KeyLevel(
                type=f"SWING_{kind.upper()}", label=f"{TF_LABEL[tf]} swing {kind}",
                category="swing", timeframe=tf, low=s.price, high=s.price,
                formed_at=s.confirmed_at, valid=breaker is None, status=status,
                summary=(f"Fractal swing {kind} at {s.price:.2f} on the {fmt_t(s.time)} candle, "
                         f"confirmed when the candle {SWING_K.get(tf, 3)} candles later "
                         f"closed ({fmt_t(s.confirmed_at)})."),
                details={"candle_time": s.time, "confirmed_at": s.confirmed_at},
                events=[s.to_event()], reactions=1, last_reaction=s.time))
    return levels


# -------------------------------------------------------------------------- zones

@dataclass
class Zone:
    tf: str
    low: float
    high: float
    pivots: list[Swing]
    atr: float
    crosses: int
    role_at_last: str  # "support" | "resistance"

    @property
    def last_pivot(self) -> Swing:
        return max(self.pivots, key=lambda p: p.index)


def cluster_pivots(pivots: list[Swing], atr_value: float) -> list[list[Swing]]:
    """Greedy price clustering of pivots (highs and lows together).

    Sorted by price, a pivot joins the current cluster if it is within ZONE_JOIN_ATR * ATR
    of the cluster's highest price and the cluster's span stays <= ZONE_MAX_SPAN_ATR * ATR.
    """
    clusters: list[list[Swing]] = []
    join, span = ZONE_JOIN_ATR * atr_value, ZONE_MAX_SPAN_ATR * atr_value
    for p in sorted(pivots, key=lambda s: (s.price, s.index)):
        if clusters and p.price - clusters[-1][-1].price <= join and \
                p.price - clusters[-1][0].price <= span:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return clusters


def count_crosses(bars: list[Bar], start: int, low: float, high: float) -> int:
    """Times the close moved from one side of [low, high] to the other after index `start`."""
    side, crosses = 0, 0
    for b in bars[start + 1:]:
        s = 1 if b.close > high else -1 if b.close < low else 0
        if s == 0:
            continue
        if side and s != side:
            crosses += 1
        side = s
    return crosses


def find_zones(bars: list[Bar], swings: list[Swing], tf: str) -> list[Zone]:
    if len(bars) < 20:
        return []
    a = atr(bars)
    zones = []
    for cluster in cluster_pivots(swings, a):
        if len(cluster) < ZONE_MIN_REACTIONS:
            continue
        lo, hi = min(p.price for p in cluster), max(p.price for p in cluster)
        if hi - lo < ZONE_MIN_WIDTH_ATR * a:
            mid, half = (lo + hi) / 2, ZONE_MIN_WIDTH_ATR * a / 2
            lo, hi = mid - half, mid + half
        last = max(cluster, key=lambda p: p.index)
        zones.append(Zone(tf, lo, hi, sorted(cluster, key=lambda p: p.index), a,
                          count_crosses(bars, last.index, lo, hi),
                          "resistance" if last.kind == "high" else "support"))
    return zones


def merge_zones(zones: list[Zone], price: float, weight: dict[str, int]) -> list[KeyLevel]:
    """Combine zones from different timeframes without letting them chain into wide bands.

    Zones are taken from the highest timeframe down. A zone whose midpoint lies inside an
    existing (higher-timeframe) zone is attached to it as confirming evidence and does not
    change its bounds; otherwise it starts a zone of its own.
    """
    out: list[KeyLevel] = []
    groups: list[list[Zone]] = []
    for z in sorted(zones, key=lambda z: (-weight.get(z.tf, 0), -len(z.pivots), z.low)):
        mid = (z.low + z.high) / 2
        home = next((g for g in groups if g[0].low <= mid <= g[0].high), None)
        if home is not None:
            home.append(z)
        else:
            groups.append([z])
    for g in groups:
        lo, hi = g[0].low, g[0].high
        top = g[0]
        tfs = sorted({z.tf for z in g}, key=lambda t: -weight.get(t, 0))
        pivots = sorted((p for z in g for p in z.pivots), key=lambda p: p.time)
        last = max(pivots, key=lambda p: p.time)
        role_last = "resistance" if last.kind == "high" else "support"
        side = "resistance" if lo > price else "support" if hi < price else "inside"
        role = side if side != "inside" else role_last
        valid = any(z.crosses < ZONE_INVALID_CROSSES for z in g)
        crosses = top.crosses
        if not valid:
            status = f"invalidated: closes crossed it {crosses} times since its last reaction"
        elif side == "inside":
            status = "price inside zone (contested)"
        elif role != role_last:
            status = f"flipped: last reacted as {role_last}, now {role}"
        else:
            status = "intact"
        highs = sum(p.kind == "high" for p in pivots)
        lows = len(pivots) - highs
        per_tf = {z.tf: len(z.pivots) for z in g}
        polarity = highs > 0 and lows > 0
        summary = (f"{len(pivots)} confirmed swing reactions ({highs} highs, {lows} lows) "
                   f"between {lo:.2f} and {hi:.2f} on {', '.join(TF_LABEL[t] for t in tfs)} "
                   f"candles; last reaction {fmt_d(last.time)}."
                   + (" Acted as both support and resistance." if polarity else ""))
        out.append(KeyLevel(
            type="RESISTANCE_ZONE" if role == "resistance" else "SUPPORT_ZONE",
            label=f"{TF_LABEL[tfs[0]]} {role} zone", category="zone", timeframe=tfs[0],
            timeframes=tfs, low=lo, high=hi, formed_at=min(p.confirmed_at for p in pivots),
            valid=valid, status=status, summary=summary,
            details={"reactions_by_timeframe": per_tf, "swing_highs": highs, "swing_lows": lows,
                     "role_reversal": polarity, "closes_crossing_since_last_reaction": crosses,
                     "atr": {z.tf: round(z.atr, 4) for z in g},
                     "rule": (f"pivots within {ZONE_JOIN_ATR} ATR, span <= {ZONE_MAX_SPAN_ATR} "
                              f"ATR, >= {ZONE_MIN_REACTIONS} reactions, min width "
                              f"{ZONE_MIN_WIDTH_ATR} ATR")},
            events=[p.to_event() for p in pivots], reactions=len(pivots),
            last_reaction=last.time))
    return out


# ---------------------------------------------------------- reference levels

def session_bars(bars: list[Bar], day: date) -> list[Bar]:
    return [b for b in bars if ny_date(b.time) == day]


def previous_day_levels(day_bars: list[Bar], intraday: dict[str, list[Bar]], prev_day: date,
                        prev_open: int, prev_close: int, end_of: dict[str, EndFn],
                        current_bars: list[Bar]) -> list[KeyLevel]:
    """PDH/PDL from regular-session intraday candles when they cover the whole previous
    session (the standard definition); otherwise from the daily candle."""
    source, hi_bar, lo_bar = None, None, None
    for tf, bars in intraday.items():
        sess = session_bars(bars, prev_day)
        if sess and sess[0].time == prev_open and end_of[tf](sess[-1]) >= prev_close:
            hi_bar = max(sess, key=lambda b: b.high)
            lo_bar = min(sess, key=lambda b: b.low)
            source = f"{TF_LABEL[tf]} regular-session candles ({len(sess)} candles)"
            break
    if source is None:
        daily = [b for b in day_bars if ny_date(b.time) == prev_day]
        if not daily:
            return []
        hi_bar = lo_bar = daily[0]
        source = "daily candle"
    out = []
    for typ, value, bar, word in (("PDH", hi_bar.high, hi_bar, "high"),
                                  ("PDL", lo_bar.low, lo_bar, "low")):
        taken = next((b for b in current_bars
                      if (b.high > value if word == "high" else b.low < value)), None)
        out.append(KeyLevel(
            type=typ, label=f"Previous day {word}", category="reference", timeframe="1Day",
            low=value, high=value, formed_at=prev_close, valid=True,
            status=f"traded through {fmt_t(taken.time)}" if taken else "untested this session",
            summary=f"{prev_day.isoformat()} regular-session {word} {value:.2f}, from {source}.",
            details={"session_date": prev_day.isoformat(), "source": source,
                     "extreme_candle_time": bar.time},
            events=[{"time": bar.time, "price": value, "kind": f"session {word}",
                     "timeframe": "1Day"}]))
    return out


def previous_week_levels(day_bars: list[Bar], current_day: date,
                         week_close_ts: Callable[[list[Bar]], int]) -> list[KeyLevel]:
    """PWH/PWL: extremes of the daily candles in the last complete ISO week before the
    current trading day's week."""
    cur_week = current_day.isocalendar()[:2]
    weeks: dict[tuple[int, int], list[Bar]] = {}
    for b in day_bars:
        key = ny_date(b.time).isocalendar()[:2]
        if key < cur_week:
            weeks.setdefault(key, []).append(b)
    if not weeks:
        return []
    key = max(weeks)
    days = weeks[key]
    hi, lo = max(days, key=lambda b: b.high), min(days, key=lambda b: b.low)
    first, last = fmt_d(days[0].time), fmt_d(days[-1].time)
    formed = week_close_ts(days)
    return [
        KeyLevel(type=typ, label=f"Previous week {word}", category="reference",
                 timeframe="1Week", low=v, high=v, formed_at=formed, valid=True, status="reference",
                 summary=(f"Week {key[0]}-W{key[1]:02d} ({first} to {last}, {len(days)} sessions) "
                          f"{word} {v:.2f} made on {fmt_d(b.time)}."),
                 details={"week": f"{key[0]}-W{key[1]:02d}", "sessions": len(days),
                          "extreme_session": fmt_d(b.time)},
                 events=[{"time": b.time, "price": v, "kind": f"week {word}", "timeframe": "1Day"}])
        for typ, v, b, word in (("PWH", hi.high, hi, "high"), ("PWL", lo.low, lo, "low"))
    ]


def session_open_and_range(bars: list[Bar], tf: str, minutes: int, day: date, open_ts: int,
                           as_of_ts: int) -> tuple[list[KeyLevel], list[str]]:
    """Current session open and the opening range (first OPENING_RANGE_MINUTES)."""
    notes: list[str] = []
    sess = session_bars(bars, day)
    if not sess:
        notes.append("Current session has no completed candles yet; session open and opening "
                     "range are not available.")
        return [], notes
    out = []
    first = sess[0]
    if first.time == open_ts:
        out.append(KeyLevel(
            type="SESSION_OPEN", label="Session open", category="reference", timeframe=tf,
            low=first.open, high=first.open, formed_at=first.time + minutes * 60, valid=True,
            status="reference",
            summary=f"Open of the first regular-session candle on {day.isoformat()}.",
            details={"candle_time": first.time},
            events=[{"time": first.time, "price": first.open, "kind": "session open",
                     "timeframe": tf}]))
    or_end = open_ts + OPENING_RANGE_MINUTES * 60
    if OPENING_RANGE_MINUTES % minutes:
        notes.append(f"Opening range needs candles that divide {OPENING_RANGE_MINUTES} minutes.")
    elif as_of_ts < or_end:
        notes.append("Opening range forms 09:30–10:00 ET and is not complete yet.")
    else:
        rng = [b for b in sess if b.time < or_end]
        expected = OPENING_RANGE_MINUTES // minutes
        if len(rng) == expected and rng[0].time == open_ts:
            hi, lo = max(rng, key=lambda b: b.high), min(rng, key=lambda b: b.low)
            for typ, v, b, word in (("ORH", hi.high, hi, "high"), ("ORL", lo.low, lo, "low")):
                out.append(KeyLevel(
                    type=typ, label=f"Opening range {word}", category="reference",
                    timeframe=tf, low=v, high=v, formed_at=or_end, valid=True,
                    status="reference",
                    summary=(f"{word.capitalize()} of the first {OPENING_RANGE_MINUTES} minutes "
                             f"({expected} x {TF_LABEL[tf]} candles) on {day.isoformat()}."),
                    details={"window": "09:30-10:00 ET", "candles": expected,
                             "extreme_candle_time": b.time},
                    events=[{"time": b.time, "price": v, "kind": f"opening range {word}",
                             "timeframe": tf}]))
        else:
            notes.append(f"Opening range skipped: expected {expected} candles from 09:30, "
                         f"found {len(rng)}.")
    return out, notes


def session_vwap_level(bars: list[Bar], tf: str, day: date, close_ts: int,
                       as_of_ts: int) -> KeyLevel | None:
    """VWAP = sum(typical price x volume) / sum(volume) over the current session's candles,
    typical price = (high + low + close) / 3, anchored at 09:30 ET."""
    sess = session_bars(bars, day)
    vol = sum(b.volume for b in sess)
    if not sess or vol <= 0:
        return None
    value = sum((b.high + b.low + b.close) / 3 * b.volume for b in sess) / vol
    live = as_of_ts < close_ts
    return KeyLevel(
        type="VWAP", label="Session VWAP", category="indicator", timeframe=tf, low=value,
        high=value, formed_at=sess[-1].time, valid=live,
        status="live (session in progress)" if live else "final value (session closed)",
        summary=(f"Volume-weighted average of typical price over {len(sess)} "
                 f"{TF_LABEL[tf]} regular-session candles on {day.isoformat()}."),
        details={"candles": len(sess), "volume": vol, "anchor": "09:30 ET"})


def ema_levels(bars: list[Bar], tf: str, periods) -> list[KeyLevel]:
    closes = [b.close for b in bars]
    out = []
    for p in periods:
        v = ind.last(ind.ema(closes, p))
        if v is None:
            continue
        warm = len(closes) >= EMA_WARMUP_MULTIPLE * p
        out.append(KeyLevel(
            type=f"EMA{p}", label=f"{p} EMA ({TF_LABEL[tf]})", category="indicator",
            timeframe=tf, low=v, high=v, formed_at=bars[-1].time, valid=warm,
            status="current value" if warm else
            f"unreliable: {len(closes)} candles < {EMA_WARMUP_MULTIPLE * p} needed to warm up",
            summary=(f"EMA({p}) of {TF_LABEL[tf]} closes, alpha = 2/({p}+1), seeded with the "
                     f"SMA of the first {p} closes; {len(closes)} candles."),
            details={"period": p, "candles": len(closes), "warmed_up": warm}))
    return out


# ------------------------------------------------------------------- liquidity

def liquidity_levels(bars: list[Bar], swings: list[Swing], tf: str, price: float
                     ) -> list[KeyLevel]:
    """Potential liquidity beyond equal highs/lows.

    Equal highs: >= 2 confirmed swing highs within EQUAL_EXTREME_ATR * ATR of each other.
    Untaken (no later candle traded above the highest of them) => potential buy-side
    liquidity in [top, top + LIQUIDITY_BAND_ATR * ATR]. Mirror for equal lows.
    Groups swept within the last LIQUIDITY_SWEEP_LOOKBACK candles are reported as swept.
    """
    if len(bars) < 20:
        return []
    a = atr(bars)
    tol, band = EQUAL_EXTREME_ATR * a, LIQUIDITY_BAND_ATR * a
    out: list[KeyLevel] = []
    for kind in ("high", "low"):
        pts = sorted((s for s in swings if s.kind == kind), key=lambda s: s.price)
        groups: list[list[Swing]] = []
        for s in pts:
            if groups and s.price - groups[-1][0].price <= tol:
                groups[-1].append(s)
            else:
                groups.append([s])
        for g in groups:
            if len(g) < 2:
                continue
            extreme = max(s.price for s in g) if kind == "high" else min(s.price for s in g)
            last_idx = max(s.index for s in g)
            sweep_i = next((i for i in range(last_idx + 1, len(bars))
                            if (bars[i].high > extreme if kind == "high"
                                else bars[i].low < extreme)), None)
            if sweep_i is not None and sweep_i < len(bars) - LIQUIDITY_SWEEP_LOOKBACK:
                continue  # old news
            lo, hi = (extreme, extreme + band) if kind == "high" else (extreme - band, extreme)
            name = "Buy-side" if kind == "high" else "Sell-side"
            if sweep_i is None:
                status, valid = "untaken", True
            else:
                sb = bars[sweep_i]
                rejected = sb.close < extreme if kind == "high" else sb.close > extreme
                status = (f"swept {fmt_t(sb.time)}"
                          + (" and closed back inside (rejected)" if rejected else ""))
                valid = False
            out.append(KeyLevel(
                type="LIQUIDITY", label=f"{name} liquidity ({TF_LABEL[tf]} equal {kind}s)",
                category="liquidity", timeframe=tf, low=lo, high=hi,
                formed_at=max(s.confirmed_at for s in g), valid=valid, status=status,
                summary=(f"{len(g)} swing {kind}s within {tol:.2f} ({EQUAL_EXTREME_ATR} ATR) of "
                         f"each other at {extreme:.2f}. Stop orders often rest beyond equal "
                         f"{kind}s; this is a potential area, not observed order flow."),
                details={"side": name.lower(), "equal_extremes": len(g), "atr": round(a, 4)},
                events=[s.to_event() for s in g], reactions=len(g),
                last_reaction=max(s.time for s in g)))
    above = sorted((l for l in out if l.low >= price), key=lambda l: l.low)
    below = sorted((l for l in out if l.high <= price), key=lambda l: -l.high)
    return above[:LIQUIDITY_PER_SIDE] + below[:LIQUIDITY_PER_SIDE]


# -------------------------------------------------------------------- breakouts

@dataclass
class BreakoutCandidate:
    name: str  # e.g. "PDH", "Daily resistance zone"
    low: float
    high: float
    known_from: int  # unix: level is knowable from this time
    pivots: list[Swing] | None = None  # zones: bounds are rebuilt point-in-time


def find_breakouts(bars: list[Bar], tf: str, end_of: EndFn, candidates: list[BreakoutCandidate],
                   scan: int) -> list[KeyLevel]:
    """Confirmed close-based breakouts/breakdowns in the last `scan` candles.

    Up:   close[i] > top + buffer_i  and  close[i-1] <= top,  then  close[i+1] > top
          (the follow-through candle i+1 confirms; confirmation time = its close).
    Down: close[i] < bottom - buffer_i and close[i-1] >= bottom, then close[i+1] < bottom.
    buffer_i = BREAKOUT_BUFFER_ATR * ATR14 at candle i-1 (no future data).
    For zones, top/bottom come only from swing pivots confirmed before candle i.
    After confirmation, a close back beyond the far side of the level marks it failed.
    """
    if len(bars) < 20:
        return []
    atr_series = ind.atr(bars)
    out: list[KeyLevel] = []
    seen: set[tuple] = set()  # overlapping candidates can describe the same event
    start = max(1, len(bars) - scan)
    for c in candidates:
        found = None
        for i in range(start, len(bars) - 1):
            b, prev, nxt = bars[i], bars[i - 1], bars[i + 1]
            if c.known_from > b.time:
                continue
            lo, hi = c.low, c.high
            if c.pivots is not None:
                known = [p for p in c.pivots if p.confirmed_at <= b.time]
                if len(known) < ZONE_MIN_REACTIONS:
                    continue
                lo, hi = min(p.price for p in known), max(p.price for p in known)
            a = atr_series[i - 1] or (prev.high - prev.low)
            buf = BREAKOUT_BUFFER_ATR * a
            if b.close > hi + buf and prev.close <= hi and nxt.close > hi:
                found = ("BREAKOUT", "above", i, lo, hi, buf)
            elif b.close < lo - buf and prev.close >= lo and nxt.close < lo:
                found = ("BREAKDOWN", "below", i, lo, hi, buf)
        if not found:
            continue
        typ, word, i, lo, hi, buf = found
        b, nxt = bars[i], bars[i + 1]
        level_price = hi if typ == "BREAKOUT" else lo
        event = (typ, i, round(level_price, 4))
        if event in seen:
            continue
        seen.add(event)
        after = bars[i + 2:]
        fail = next((x for x in after if (x.close < lo if typ == "BREAKOUT" else x.close > hi)),
                    None)
        vols = [x.volume for x in bars[max(0, i - 20):i]]
        rel_vol = b.volume / (sum(vols) / len(vols)) if vols and sum(vols) else None
        out.append(KeyLevel(
            type=typ, label=f"{'Breakout' if typ == 'BREAKOUT' else 'Breakdown'} {word} {c.name}",
            category="breakout", timeframe=tf, low=level_price, high=level_price,
            formed_at=end_of(nxt), valid=fail is None,
            status=(f"failed: closed back {'below' if typ == 'BREAKOUT' else 'above'} the level "
                    f"{fmt_t(fail.time)}") if fail else "holding",
            summary=(f"{TF_LABEL[tf]} candle {fmt_t(b.time)} closed at {b.close:.2f}, "
                     f"{word} {c.name} {level_price:.2f} by more than the {buf:.2f} buffer; "
                     f"confirmed when the next candle also closed {word} it ({nxt.close:.2f})."),
            details={"level": c.name, "level_low": round(lo, 4), "level_high": round(hi, 4),
                     "buffer": round(buf, 4), "breakout_candle": b.time,
                     "confirmation_candle": nxt.time,
                     "relative_volume": round(rel_vol, 2) if rel_vol else None},
            events=[{"time": b.time, "price": b.close, "kind": typ.lower(), "timeframe": tf},
                    {"time": nxt.time, "price": nxt.close, "kind": "confirmation",
                     "timeframe": tf}],
            reactions=0, last_reaction=b.time))
    return out


# ------------------------------------------------------------------------ splits

def detect_unadjusted_splits(day_bars: list[Bar]) -> list[dict]:
    """Find overnight gaps that look like an unadjusted split.

    ratio = prev close / open. Flag when ratio is within 5% of a common split ratio
    (2:1 ... 50:1, or the inverse for reverse splits), the move is at least 45%, and the
    average share volume over the 5 sessions after vs before moved in the split's
    direction by at least half the ratio (share counts scale with the split).
    """
    out = []
    for i in range(1, len(day_bars)):
        prev, cur = day_bars[i - 1], day_bars[i]
        if cur.open <= 0:
            continue
        r = prev.close / cur.open
        if abs(math.log(r)) < math.log(1.8):
            continue
        for n in SPLIT_RATIOS:
            for ratio, label in ((n, f"{n}-for-1"), (1 / n, f"1-for-{n} reverse")):
                if abs(r / ratio - 1) <= SPLIT_RATIO_TOLERANCE:
                    before = [b.volume for b in day_bars[max(0, i - 5):i]]
                    after = [b.volume for b in day_bars[i:i + 5]]
                    if before and after and sum(before) > 0:
                        vr = (sum(after) / len(after)) / (sum(before) / len(before))
                        ok = vr >= ratio * 0.5 if ratio > 1 else vr <= ratio * 2
                        if ok:
                            out.append({"date": fmt_d(cur.time), "time": cur.time,
                                        "ratio": label, "price_ratio": round(r, 4),
                                        "volume_ratio": round(vr, 2)})
    return out


def week_bounds(d: date) -> tuple[date, date]:
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)
