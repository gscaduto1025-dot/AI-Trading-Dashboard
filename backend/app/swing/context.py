"""Point-in-time context for the swing engine.

* Only candles that had closed by `as_of` exist here.
* Every per-candle quantity at index i (ATR, EMA20/50/200, relative volume) uses candles
  0..i only. Swing points are usable for a decision on candle i only if they were confirmed
  by the close of candle i-1, and support/resistance zones "as of candle i" are rebuilt from
  candles 0..i-1 with the key-level engine's own zone rules. So a past trigger is judged
  with exactly the levels that existed then, and never changes later.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime

from ..analysis import indicators as ind
from ..data.base import Bar
from ..levels.detect import find_swings, find_zones
from ..levels.engine import InsufficientData, compute_key_levels
from ..levels.model import SWING_K, Swing
from ..market.calendar import NY, MarketCalendar
from ..market.timeframes import bar_end, spec
from ..planning.config import PlanningConfig
from .config import SwingConfig


def fmt_day(ts: int) -> str:
    return datetime.fromtimestamp(ts, NY).strftime("%Y-%m-%d")


@dataclass
class SwingTF:
    tf: str
    bars: list[Bar]
    end: list[int]
    atr: list[float | None]
    ema20: list[float | None]
    ema50: list[float | None]
    ema200: list[float | None]
    swings: list[Swing]
    k: int
    _zones: dict = field(default_factory=dict)

    def atr_before(self, i: int) -> float:
        for j in range(i - 1, -1, -1):
            if self.atr[j] is not None:
                return self.atr[j]
        b = self.bars[max(i - 1, 0)]
        return max(b.high - b.low, 1e-6)

    def relvol(self, i: int, n: int = 20) -> float | None:
        prior = [b.volume for b in self.bars[max(0, i - n):i]]
        avg = sum(prior) / len(prior) if prior else 0
        return self.bars[i].volume / avg if avg > 0 else None

    @property
    def volume_available(self) -> bool:
        return any(b.volume > 0 for b in self.bars[-20:])

    def swings_before(self, i: int) -> list[Swing]:
        """Swings confirmed by the close of candle i-1 (usable for a decision on i)."""
        return [s for s in self.swings if s.index + self.k <= i - 1]

    def zones_at(self, i: int):
        """Support/resistance zones built only from candles 0..i-1."""
        if i not in self._zones:
            known = self.swings_before(i)
            self._zones[i] = find_zones(self.bars[:i], known, self.tf) if i >= 20 else []
        return self._zones[i]

    def structure_at(self, i: int) -> dict:
        """Market structure from the last two confirmed swing highs and lows before i."""
        sw = self.swings_before(i)
        highs = [s for s in sw if s.kind == "high"][-2:]
        lows = [s for s in sw if s.kind == "low"][-2:]
        out = {"label": "insufficient swings", "highs": [round(s.price, 2) for s in highs],
               "lows": [round(s.price, 2) for s in lows]}
        if len(highs) < 2 or len(lows) < 2:
            return out
        hh, hl = highs[1].price > highs[0].price, lows[1].price > lows[0].price
        lh, ll = highs[1].price < highs[0].price, lows[1].price < lows[0].price
        if hh and hl:
            out["label"] = "uptrend (higher highs and higher lows)"
            out["trend"] = "up"
        elif lh and ll:
            out["label"] = "downtrend (lower highs and lower lows)"
            out["trend"] = "down"
        else:
            out["label"] = "range / mixed structure"
            out["trend"] = "mixed"
        return out

    def ema_stack(self, i: int) -> str:
        e20, e50, e200 = self.ema20[i], self.ema50[i], self.ema200[i]
        if e20 is None or e50 is None:
            return "insufficient history"
        if e200 is None:
            rel = ">" if e20 > e50 else "<"
            return f"EMA20 {rel} EMA50 (EMA200 needs 200 candles)"
        if e20 > e50 > e200:
            return "bullish (EMA20 > EMA50 > EMA200)"
        if e20 < e50 < e200:
            return "bearish (EMA20 < EMA50 < EMA200)"
        return "mixed"


def build_tf(tf: str, bars: list[Bar], cal: MarketCalendar, as_ts: int) -> SwingTF:
    s = spec(tf)
    bars = [b for b in sorted(bars, key=lambda b: b.time)
            if bar_end(b, s, cal, "regular") <= as_ts]
    end = [bar_end(b, s, cal, "regular") for b in bars]
    closes = [b.close for b in bars]
    k = SWING_K.get(tf, 3)
    swings = find_swings(bars, k, tf, lambda b: b.time) if len(bars) > 2 * k else []
    return SwingTF(tf, bars, end, ind.atr(bars), ind.ema(closes, 20), ind.ema(closes, 50),
                   ind.ema(closes, 200), swings, k)


def _trend(close, fast, slow) -> str:
    if None in (close, fast, slow):
        return "neutral"
    if close > fast > slow:
        return "bullish"
    if close < fast < slow:
        return "bearish"
    return "neutral"


@dataclass
class SwingContext:
    ticker: str
    as_of: datetime
    calendar: MarketCalendar
    config: SwingConfig
    tfs: dict[str, SwingTF]
    support: dict[str, SwingTF]
    key_levels: list[dict]
    levels_note: str | None
    gate_reasons: list[str]
    planning: PlanningConfig = field(default_factory=PlanningConfig)

    def htf(self, tf: str, i: int) -> dict:
        """Higher-timeframe context for candle i of `tf`, using only closed candles.

        Daily candles: weekly structure + weekly close vs EMA20 vs EMA50.
        Weekly candles: weekly close vs EMA50 and the EMA50's 4-week slope.
        """
        if tf == "1Day":
            w = self.tfs.get("1Week")
            t = self.tfs["1Day"].end[i]
            j = bisect.bisect_right(w.end, t) - 1 if w else -1
            if j < 0:
                return {"label": "neutral", "detail": "no completed weekly candles"}
            st = w.structure_at(j + 1)
            tr = _trend(w.bars[j].close, w.ema20[j], w.ema50[j])
            st_dir = {"up": "bullish", "down": "bearish"}.get(st.get("trend"), "neutral")
            label = tr if tr == st_dir or st_dir == "neutral" else (
                st_dir if tr == "neutral" else "neutral")
            return {"label": label, "detail": f"weekly {st['label']}; weekly close vs EMA20/50 "
                                              f"{tr} (week of {fmt_day(w.bars[j].time)})"}
        d = self.tfs[tf]
        e50 = d.ema50[i]
        prev = d.ema50[i - 4] if i >= 4 else None
        if e50 is None or prev is None:
            return {"label": "neutral", "detail": "insufficient weekly history for EMA50"}
        c = d.bars[i].close
        label = "bullish" if c > e50 and e50 > prev else "bearish" if c < e50 and e50 < prev \
            else "neutral"
        return {"label": label, "detail": f"weekly close {c:.2f} vs EMA50 {e50:.2f} "
                                          f"({'rising' if e50 > prev else 'falling'})"}

    def supporting(self, t: int) -> dict:
        """4-hour and 1-hour trend (close vs EMA20 vs EMA50) using candles closed by t."""
        out = {}
        for tf, d in self.support.items():
            n = bisect.bisect_right(d.end, t)
            if n == 0:
                out[tf] = "no data"
                continue
            out[tf] = _trend(d.bars[n - 1].close, d.ema20[n - 1], d.ema50[n - 1])
        return out

    def weekly_zones_for_daily(self, i: int):
        """Weekly zones usable for a decision on daily candle i: built from weekly candles that
        had closed by the close of daily candle i-1 (the same cutoff as daily zones_at(i))."""
        w = self.tfs.get("1Week")
        if not w or i < 1:
            return []
        j = bisect.bisect_right(w.end, self.tfs["1Day"].end[i - 1])
        return w.zones_at(j)


def build_swing_context(ticker: str, frames: dict[str, list[Bar]], as_of: datetime,
                        cal: MarketCalendar, cfg: SwingConfig,
                        data_problems: list[str] | None = None,
                        planning: PlanningConfig | None = None,
                        liquidity: dict | None = None) -> SwingContext:
    as_ts = int(as_of.timestamp())
    tfs = {tf: build_tf(tf, frames.get(tf, []), cal, as_ts) for tf in ("1Day", "1Week")
           if frames.get(tf)}
    support = {tf: build_tf(tf, frames.get(tf, []), cal, as_ts) for tf in ("4Hour", "1Hour")
               if frames.get(tf)}
    gate = list(data_problems or [])
    daily = tfs.get("1Day")
    if not daily or len(daily.bars) < 60:
        gate.append("Fewer than 60 completed daily candles.")
    else:
        recent = daily.bars[-20:]
        adv = sum(b.close * b.volume for b in recent) / len(recent)
        basis = "on this feed"
        if liquidity is not None:  # IEX volume is a small share of the market: use SIP
            adv, basis = liquidity["adv"], f"on {liquidity['basis']}"
        if adv < cfg.gates.min_avg_dollar_volume:
            gate.append(f"Insufficient liquidity: average daily dollar volume ${adv / 1e6:.1f}M "
                        f"< ${cfg.gates.min_avg_dollar_volume / 1e6:.0f}M {basis}")
    levels, note = [], None
    try:
        report = compute_key_levels(ticker, "swing", {tf: frames.get(tf, []) for tf in
                                                      ("1Hour", "4Hour", "1Day", "1Week")},
                                    as_of, cal)
        levels = report["levels"]
    except InsufficientData as exc:
        note = f"Key levels unavailable: {exc}"
    return SwingContext(ticker, as_of, cal, cfg, tfs, support, levels, note, gate,
                        planning or PlanningConfig())
