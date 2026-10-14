"""Shared, causal building blocks for the day-trading strategies.

Point-in-time contract:
* Only candles that had closed by `as_of` exist in the context.
* Every per-candle series (ATR, EMA, VWAP, relative volume, HTF bias) at index i depends
  only on candles 0..i. Strategies scan the session candle by candle and decide each
  event from data available at that candle's close, so earlier results never change when
  later candles arrive (no repainting).
* Key levels (zones, PDH/PDL, PWH/PWL, swings, liquidity) are computed as of the session's
  09:30 open from prior sessions only; intraday levels (opening range, VWAP, intraday swings)
  are derived inside the scan, causally.
"""

from __future__ import annotations

import bisect
import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime

from ..analysis import indicators as ind
from ..data.base import Bar
from ..levels.engine import InsufficientData, compute_key_levels
from ..market.calendar import NY, MarketCalendar, SessionDay
from ..market.timeframes import bar_end, spec
from ..planning.config import PlanningConfig
from .config import StrategyConfig

WATCHING, TRIGGERED, NO_TRADE = "WATCHING", "TRIGGERED", "NO_TRADE"
LONG, SHORT = "LONG", "SHORT"


def fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, NY).strftime("%H:%M")


def fmt_full(ts: int) -> str:
    return datetime.fromtimestamp(ts, NY).strftime("%Y-%m-%d %H:%M ET")


# --------------------------------------------------------------------- results

def signal_id(ticker: str, strategy: str, timeframe: str, direction: str, session_date: str,
              trigger_time: int | None) -> str:
    """Deterministic identity of a setup/signal (same inputs -> same id, forever)."""
    raw = f"{ticker}|{strategy}|{timeframe}|{direction}|{session_date}|{trigger_time}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


@dataclass
class Condition:
    name: str
    passed: bool | None  # None = not applicable (e.g. no volume on this feed)
    required: bool = True
    detail: str = ""
    at: int | None = None  # candle start time the condition was evaluated on

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TriggerEvent:
    """A completed-candle trigger. Immutable once emitted."""

    time: int  # start of the trigger candle
    confirmed_at: int  # its close
    entry: float
    stop: float
    target1: float | None
    target2: float | None
    risk_reward: float | None
    targets_basis: list[str]
    conditions: list[Condition]
    evidence: list[dict]
    levels: list[dict]
    invalidation: list[str]
    blocked: str | None = None  # why the trade plan rejected this trigger (None = accepted)
    plan_input: object | None = None  # planning.PlanInput the plan was built from
    plan: dict | None = None  # planning.build_plan output at the trigger

    def key(self) -> tuple:
        return (self.time, round(self.entry, 4), round(self.stop, 4))


@dataclass
class Setup:
    ticker: str
    strategy: str
    strategy_name: str
    direction: str
    timeframe: str
    status: str
    htf_bias: dict
    evaluated_at: str
    session_date: str
    session_state: str
    entry: float | None = None
    stop: float | None = None
    target1: float | None = None
    target2: float | None = None
    risk_reward: float | None = None
    risk_per_share: float | None = None
    supporting_levels: list[dict] = field(default_factory=list)
    confirmation: list[Condition] = field(default_factory=list)
    invalidation: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    trigger_time: int | None = None
    confirmed_at: int | None = None
    outcome: dict | None = None
    targets_basis: list[str] = field(default_factory=list)
    triggers_today: list[dict] = field(default_factory=list)
    plan: dict | None = None  # complete conditional trade plan (planning engine)

    @property
    def id(self) -> str:
        return signal_id(self.ticker, self.strategy, self.timeframe, self.direction,
                         self.session_date, self.trigger_time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["id"] = self.id
        for k in ("entry", "stop", "target1", "target2", "risk_per_share"):
            if d[k] is not None:
                d[k] = round(d[k], 4)
        if d["risk_reward"] is not None:
            d["risk_reward"] = round(d["risk_reward"], 2)
        d["trigger_time_et"] = fmt_full(self.trigger_time) if self.trigger_time else None
        return d


# --------------------------------------------------------------------- context

@dataclass
class TFData:
    tf: str
    bars: list[Bar]
    end: list[int]
    start: int  # index of the session's first candle (== len(bars) if none yet)
    atr: list[float | None]
    ema9: list[float | None]
    ema20: list[float | None]
    vwap: list[float | None]
    relvol: list[float | None]
    abnormal: list[str | None]
    missing_slots: int
    volume_available: bool

    @property
    def session(self) -> range:
        return range(self.start, len(self.bars))

    def atr_before(self, i: int) -> float:
        """ATR known before candle i closes (from candle i-1)."""
        for j in range(i - 1, -1, -1):
            if self.atr[j] is not None:
                return self.atr[j]
        b = self.bars[max(i - 1, 0)]
        return max(b.high - b.low, 1e-6)


@dataclass
class StrategyContext:
    ticker: str
    as_of: datetime
    calendar: MarketCalendar
    config: StrategyConfig
    session: SessionDay
    session_open: bool
    tfs: dict[str, TFData]
    daily: list[Bar]
    hourly: list[Bar]
    hourly_end: list[int]
    levels: list[dict]  # key levels as of the session open
    levels_note: str | None
    daily_atr: float
    gate_reasons: list[str]
    daily_bias: dict
    planning: PlanningConfig = field(default_factory=PlanningConfig)
    _hourly_ema20: list[float | None] = field(default_factory=list)
    _hourly_ema50: list[float | None] = field(default_factory=list)

    # ---- higher-timeframe bias, causal in time t
    def htf_bias(self, t: int) -> dict:
        n = bisect.bisect_right(self.hourly_end, t)
        hourly = _trend(self.hourly[n - 1].close if n else None,
                        self._hourly_ema20[n - 1] if n else None,
                        self._hourly_ema50[n - 1] if n else None)
        daily = self.daily_bias["trend"]
        if daily == hourly:
            label = daily
        elif daily == "neutral":
            label = hourly
        elif hourly == "neutral":
            label = daily
        else:
            label = "neutral"  # mixed: daily and hourly disagree
        return {"label": label, "daily": daily, "hourly": hourly,
                "detail": f"daily {daily} ({self.daily_bias['detail']}); "
                          f"1-hour {hourly} at {fmt(t)} (close vs EMA20 vs EMA50)"}

    # ---- level helpers
    def reference_levels(self, types: tuple[str, ...]) -> list[dict]:
        return [l for l in self.levels if l["type"] in types]

    def zones(self, min_strength: int = 0) -> list[dict]:
        return [l for l in self.levels if l["category"] == "zone" and l["valid"]
                and l["strength"]["score"] >= min_strength]

    def entry_block(self, tfd: TFData, i: int) -> str | None:
        """Why no trigger may happen on candle i (time window, abnormal candle), if any."""
        g = self.config.gates
        b_end = tfd.end[i]
        open_ts = int(self.session.open.timestamp())
        close_ts = int(self.session.close.timestamp())
        if b_end < open_ts + g.entry_start_minutes * 60:
            return f"before {g.entry_start_minutes}-minute entry window opens"
        if b_end > close_ts - g.stop_entries_minutes_before_close * 60:
            return (f"within {g.stop_entries_minutes_before_close} minutes of the "
                    f"{self.session.close.strftime('%H:%M')} close")
        if tfd.abnormal[i]:
            return f"abnormal candle: {tfd.abnormal[i]}"
        return None


def _trend(close, ema20, ema50) -> str:
    if None in (close, ema20, ema50):
        return "neutral"
    if close > ema20 > ema50:
        return "bullish"
    if close < ema20 < ema50:
        return "bearish"
    return "neutral"


def _relvol_time_of_day(bars: list[Bar], sessions: int = 5) -> list[float | None]:
    """Volume vs the average volume at the same time of day in the prior `sessions`
    sessions (needs >= 3); falls back to the prior 20 candles. Causal."""
    by_day: dict = {}
    out: list[float | None] = []
    days_seen: list = []
    for i, b in enumerate(bars):
        t = datetime.fromtimestamp(b.time, NY)
        d, tod = t.date(), (t.hour, t.minute)
        prior = [by_day[x].get(tod) for x in days_seen[-sessions:] if x != d]
        prior = [v for v in prior if v is not None]
        if len(prior) >= 3 and sum(prior) > 0:
            out.append(b.volume / (sum(prior) / len(prior)))
        else:
            window = [x.volume for x in bars[max(0, i - 20):i]]
            avg = sum(window) / len(window) if window else 0
            out.append(b.volume / avg if avg > 0 else None)
        if d not in by_day:
            by_day[d] = {}
            days_seen.append(d)
        by_day[d][tod] = b.volume
    return out


def build_tf(tf: str, bars: list[Bar], session: SessionDay, cal: MarketCalendar,
             as_of_ts: int, cfg: StrategyConfig) -> TFData:
    s = spec(tf)
    bars = [b for b in sorted(bars, key=lambda b: b.time)
            if bar_end(b, s, cal, "regular") <= as_of_ts]
    end = [bar_end(b, s, cal, "regular") for b in bars]
    start = next((i for i, b in enumerate(bars)
                  if datetime.fromtimestamp(b.time, NY).date() == session.day), len(bars))
    closes = [b.close for b in bars]
    atr = ind.atr(bars)
    vwap: list[float | None] = [None] * len(bars)
    if start < len(bars):
        vw = ind.session_vwap(bars[start:])
        vwap[start:] = vw
    g = cfg.gates
    step = s.minutes * 60
    abnormal: list[str | None] = [None] * len(bars)
    missing = 0
    expected_next = int(session.open.timestamp())
    for i in range(start, len(bars)):
        b = bars[i]
        gap_slots = max(0, (b.time - expected_next) // step)
        missing += gap_slots
        prev_atr = next((atr[j] for j in range(i - 1, -1, -1) if atr[j] is not None), None)
        if gap_slots >= g.halt_gap_slots:
            abnormal[i] = f"{gap_slots} empty {tf} slots before this candle (possible halt)"
        elif prev_atr and b.high - b.low > g.max_bar_range_atr * prev_atr:
            abnormal[i] = (f"range {b.high - b.low:.2f} > {g.max_bar_range_atr} x ATR "
                           f"{prev_atr:.2f}")
        expected_next = b.time + step
    sess_vol = [bars[i].volume for i in range(start, len(bars))]
    return TFData(tf, bars, end, start, atr, ind.ema(closes, 9), ind.ema(closes, 20), vwap,
                  _relvol_time_of_day(bars), abnormal, missing,
                  volume_available=bool(sess_vol) and any(v > 0 for v in sess_vol))


def build_context(ticker: str, frames: dict[str, list[Bar]], as_of: datetime,
                  cal: MarketCalendar, cfg: StrategyConfig,
                  data_problems: list[str] | None = None,
                  planning: PlanningConfig | None = None,
                  liquidity: dict | None = None) -> StrategyContext:
    as_ts = int(as_of.timestamp())
    session = cal.last_session_started(as_of, "regular")
    session_open = as_of < session.close
    day_ts = int(datetime.combine(session.day, datetime.min.time(), NY).timestamp())
    daily = [b for b in sorted(frames.get("1Day", []), key=lambda b: b.time) if b.time < day_ts]
    hourly_spec = spec("1Hour")
    hourly = [b for b in sorted(frames.get("1Hour", []), key=lambda b: b.time)
              if bar_end(b, hourly_spec, cal, "regular") <= as_ts]
    hourly_end = [bar_end(b, hourly_spec, cal, "regular") for b in hourly]
    tfs = {tf: build_tf(tf, frames.get(tf, []), session, cal, as_ts, cfg)
           for tf in ("5Min", "15Min") if frames.get(tf)}

    daily_atr = ind.last(ind.atr(daily)) if len(daily) > 15 else None
    dcloses = [b.close for b in daily]
    d20, d50 = ind.last(ind.ema(dcloses, 20)), ind.last(ind.ema(dcloses, 50))
    dtrend = _trend(dcloses[-1] if dcloses else None, d20, d50)
    daily_bias = {"trend": dtrend,
                  "detail": (f"close {dcloses[-1]:.2f}, EMA20 {d20:.2f}, EMA50 {d50:.2f}"
                             if d20 and d50 else "insufficient daily history")}

    # Key levels known at the open: only prior sessions' candles can be used.
    levels, note = [], None
    try:
        report = compute_key_levels(ticker, "day", {tf: frames.get(tf, []) for tf in
                                                    ("5Min", "15Min", "1Hour", "1Day", "1Week")},
                                    session.open, cal)
        levels = report["levels"]
    except InsufficientData as exc:
        note = f"Key levels unavailable: {exc}"

    gate: list[str] = list(data_problems or [])
    g = cfg.gates
    recent = daily[-20:]
    if recent:
        adv = sum(b.close * b.volume for b in recent) / len(recent)
        basis = "on this feed"
        if liquidity is not None:  # IEX volume is a small share of the market: use SIP
            adv, basis = liquidity["adv"], f"on {liquidity['basis']}"
        if adv < g.min_avg_dollar_volume:
            gate.append(f"Insufficient liquidity: average daily dollar volume ${adv / 1e6:.1f}M "
                        f"< ${g.min_avg_dollar_volume / 1e6:.0f}M {basis}")
    else:
        gate.append("No daily history before this session")
    if daily_atr is None:
        gate.append("Insufficient daily history for ATR")
    for tf, d in tfs.items():
        n = len(d.session)
        if n:
            slots = n + d.missing_slots
            if slots and d.missing_slots / slots > g.max_missing_bar_ratio:
                gate.append(f"{tf}: {d.missing_slots} of {slots} candle slots this session have "
                            f"no trades (thin liquidity or data gaps)")
    first = next((d.bars[d.start] for d in tfs.values() if d.start < len(d.bars)), None)
    if first is not None and daily and daily_atr:
        gap = abs(first.open - daily[-1].close)
        if gap > g.max_open_gap_daily_atr * daily_atr:
            gate.append(f"Abnormal opening gap {gap:.2f} > {g.max_open_gap_daily_atr} daily ATR "
                        f"({daily_atr:.2f})")

    ctx = StrategyContext(ticker, as_of, cal, cfg, session, session_open, tfs, daily, hourly,
                          hourly_end, levels, note, daily_atr or 0.0, gate, daily_bias,
                          planning or PlanningConfig())
    hc = [b.close for b in hourly]
    ctx._hourly_ema20, ctx._hourly_ema50 = ind.ema(hc, 20), ind.ema(hc, 50)
    return ctx


# ------------------------------------------------------------ targets & outcome

def level_target_candidates(ctx: StrategyContext, direction: str) -> list:
    """Confirmed key levels (zones, prior day/week references, swings, liquidity) as target
    candidates; zones are entered through their near edge (low for longs, high for shorts).
    Only levels known at the session open are used."""
    from ..planning.plan import PriceRef
    out = []
    for l in ctx.levels:
        if not l["valid"] or l["category"] not in ("zone", "reference", "swing", "liquidity"):
            continue
        p = (l["low"] if direction == LONG else l["high"]) if l["is_zone"] else l["price"]
        strength = l["strength"]["score"]
        out.append(PriceRef(p, f"{l['label']} {p:.2f} ({l['timeframe']}, strength "
                               f"{strength}/100)", l["type"], l["timeframe"], strength))
    return out


def outcome_after(tfd: TFData, i: int, direction: str, stop: float, t1: float,
                  t2: float) -> dict:
    """What happened after the trigger candle i (informational; never alters the trigger).
    If one candle reaches both stop and a target, the stop is assumed first."""
    hit_t1 = None
    for j in range(i + 1, len(tfd.bars)):
        b = tfd.bars[j]
        stop_hit = b.low <= stop if direction == LONG else b.high >= stop
        t1_hit = b.high >= t1 if direction == LONG else b.low <= t1
        t2_hit = b.high >= t2 if direction == LONG else b.low <= t2
        if stop_hit:
            return {"state": "stopped" if hit_t1 is None else "target1_then_stopped",
                    "time": b.time, "bars_after_trigger": j - i}
        if t2_hit:
            return {"state": "target2", "time": b.time, "bars_after_trigger": j - i}
        if t1_hit and hit_t1 is None:
            hit_t1 = b.time
    if hit_t1:
        return {"state": "target1", "time": hit_t1, "bars_after_trigger": None}
    return {"state": "open", "time": None, "bars_after_trigger": len(tfd.bars) - 1 - i}


def candle_metrics(b: Bar) -> dict:
    rng = max(b.high - b.low, 1e-9)
    body = abs(b.close - b.open)
    return {"range": rng, "body": body, "upper": b.high - max(b.open, b.close),
            "lower": min(b.open, b.close) - b.low, "close_pos": (b.close - b.low) / rng}


def level_ref(l: dict) -> dict:
    return {"label": l["label"], "type": l["type"], "low": l["low"], "high": l["high"],
            "timeframe": l["timeframe"], "strength": l["strength"]["score"]}


def session_minutes(ctx: StrategyContext, t: int) -> float:
    return (t - ctx.session.open.timestamp()) / 60


def hhmm_to_ts(ctx: StrategyContext, hhmm: str) -> int:
    h, m = (int(x) for x in hhmm.split(":"))
    return int((ctx.session.open.replace(hour=h, minute=m)).timestamp())


__all__ = ["Condition", "TriggerEvent", "Setup", "TFData", "StrategyContext", "build_context",
           "level_target_candidates", "outcome_after", "candle_metrics", "level_ref", "fmt", "fmt_full",
           "signal_id", "WATCHING", "TRIGGERED", "NO_TRADE", "LONG", "SHORT"]
