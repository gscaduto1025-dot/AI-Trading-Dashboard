"""Rule-based analysis engine: market bias, setup detection, entries/stops/targets.

The engine is deterministic and explainable. Claude never invents prices: it is given the
engine's output and asked to explain it (see app/ai/claude.py).

Setup statuses
--------------
* ACTIONABLE -- the setup's trigger has fired on a closed bar, every required check passes,
  the move is not extended past the entry and reward/risk meets the minimum.
* FORMING    -- the core pattern is present but the trigger has not fired yet or a
  required check is still failing. Shows what to wait for.
* NO_TRADE   -- nothing worth acting on: no pattern, reward/risk too low, the move is
  already extended, or conditions are choppy. Doing nothing is a valid outcome.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..data.base import TIMEFRAMES, Bar
from . import indicators as ind
from .levels import SESSION_TOOLS_MAX_MINUTES, Level, compute_levels, find_pivots

ACTIONABLE, FORMING, NO_TRADE = "ACTIONABLE", "FORMING", "NO_TRADE"
_STATUS_RANK = {ACTIONABLE: 2, FORMING: 1, NO_TRADE: 0}

MODE_PARAMS = {
    # fast/slow EMA, long-term average, min reward/risk, max extension (ATR) past entry
    "day": {"fast": 9, "slow": 21, "long": None, "min_rr": 1.5, "max_ext": 0.75},
    "swing": {"fast": 20, "slow": 50, "long": 200, "min_rr": 2.0, "max_ext": 1.0},
}


@dataclass
class Check:
    label: str
    passed: bool
    required: bool = False
    detail: str = ""


@dataclass
class TradeSetup:
    status: str
    setup_type: str
    title: str
    direction: str | None = None  # "long" | "short"
    entry: float | None = None
    stop: float | None = None
    targets: list[float] = field(default_factory=list)
    risk_reward: float | None = None
    risk_per_share: float | None = None
    confidence: int = 0  # 0-100, share of checks passed weighted by required checks
    triggered: bool = False
    checklist: list[Check] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    invalidation: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in ("entry", "stop", "risk_reward", "risk_per_share"):
            if d[key] is not None:
                d[key] = round(d[key], 2)
        d["targets"] = [round(t, 2) for t in self.targets]
        return d


@dataclass
class Context:
    bars: list[Bar]
    mode: str
    price: float
    atr: float
    rsi: float
    ema_fast: float
    ema_slow: float
    vwap: float | None
    long_ma: float | None
    vol_ratio: float  # last bar volume / 20-bar average
    bias_score: int
    levels: list[Level]  # all detected levels (unsorted use)
    rsi_series: ind.Series

    @property
    def params(self) -> dict:
        return MODE_PARAMS[self.mode]

    @property
    def last(self) -> Bar:
        return self.bars[-1]

    @property
    def prev(self) -> Bar:
        return self.bars[-2]


# --------------------------------------------------------------------------- bias

def market_bias(bars: list[Bar], mode: str, bar_minutes: int) -> dict:
    p = MODE_PARAMS[mode]
    closes = [b.close for b in bars]
    price = closes[-1]
    ema_fast = ind.ema(closes, p["fast"])
    ema_slow = ind.ema(closes, p["slow"])
    rsi_val = ind.last(ind.rsi(closes)) or 50.0
    factors: list[dict] = []

    def add(label: str, value: int, detail: str) -> None:
        signal = "bullish" if value > 0 else "bearish" if value < 0 else "neutral"
        factors.append({"label": label, "value": value, "signal": signal, "detail": detail})

    # Dead-bands (in ATR units) so tiny differences in a flat market don't count as trend.
    atr_val = ind.last(ind.atr(bars)) or price * 0.01

    def sign(diff: float, band: float) -> int:
        return 1 if diff > band * atr_val else -1 if diff < -band * atr_val else 0

    ef, es = ind.last(ema_fast), ind.last(ema_slow)
    if es is not None:
        v = sign(price - es, 0.1)
        add(f"Price vs EMA{p['slow']}", v, f"{price:.2f} "
            f"{'above' if v > 0 else 'below' if v < 0 else 'at'} {es:.2f}")
    if ef is not None and es is not None:
        v = sign(ef - es, 0.05)
        add(f"EMA{p['fast']} vs EMA{p['slow']}", v,
            f"Fast EMA {'above' if v > 0 else 'below' if v < 0 else 'flat vs'} slow EMA")
    slow_vals = [v for v in ema_slow if v is not None]
    if len(slow_vals) > 5:
        v = sign(slow_vals[-1] - slow_vals[-6], 0.1)
        add(f"EMA{p['slow']} slope", v,
            f"{'Rising' if v > 0 else 'Falling' if v < 0 else 'Flat'} over 5 bars")
    add("RSI(14) momentum", 1 if rsi_val > 55 else -1 if rsi_val < 45 else 0, f"RSI {rsi_val:.1f}")
    if mode == "day" and bar_minutes <= SESSION_TOOLS_MAX_MINUTES:
        vw = ind.session_vwap(bars)[-1]
        if vw is not None:
            v = sign(price - vw, 0.1)
            add("Price vs VWAP", v, f"{price:.2f} "
                f"{'above' if v > 0 else 'below' if v < 0 else 'at'} VWAP {vw:.2f}")
    else:
        long_ma = ind.last(ind.sma(closes, p["long"] or 200))
        if long_ma is not None:
            v = sign(price - long_ma, 0.1)
            add("Price vs SMA200", v, f"{price:.2f} "
                f"{'above' if v > 0 else 'below' if v < 0 else 'at'} {long_ma:.2f}")
    highs, lows = find_pivots(bars[-120:], 3 if mode == "day" else 5)
    if len(highs) >= 2 and len(lows) >= 2:
        hh, hl = highs[-1][1] > highs[-2][1], lows[-1][1] > lows[-2][1]
        lh, ll = highs[-1][1] < highs[-2][1], lows[-1][1] < lows[-2][1]
        value = 1 if hh and hl else -1 if lh and ll else 0
        detail = ("Higher highs & higher lows" if value > 0 else
                  "Lower highs & lower lows" if value < 0 else "Mixed swing structure")
        add("Swing structure", value, detail)

    score = sum(f["value"] for f in factors)
    max_score = max(len(factors), 1)
    if score >= 3:
        label = "Bullish"
    elif score <= -3:
        label = "Bearish"
    else:
        label = "Neutral"
    return {
        "label": label,
        "score": score,
        "max_score": max_score,
        "strength": round(abs(score) / max_score, 2),
        "factors": factors,
    }


# ------------------------------------------------------------------------ helpers

def _targets(ctx: Context, direction: str, entry: float, risk: float) -> list[float]:
    """First two levels beyond entry in the trade's direction, else measured R multiples."""
    gap = ctx.atr * 0.25
    if direction == "long":
        beyond = sorted(l.price for l in ctx.levels if l.price > entry + gap)
        t1 = beyond[0] if beyond else entry + 2 * risk
        t2 = beyond[1] if len(beyond) > 1 else max(t1 + risk, entry + 3 * risk)
    else:
        beyond = sorted((l.price for l in ctx.levels if l.price < entry - gap), reverse=True)
        t1 = beyond[0] if beyond else entry - 2 * risk
        t2 = beyond[1] if len(beyond) > 1 else min(t1 - risk, entry - 3 * risk)
    return [t1, t2]


def _finish(ctx: Context, setup: TradeSetup, extra_checks: list[Check]) -> TradeSetup:
    """Add the shared checks (R:R, extension, stop distance) and decide the status."""
    p = ctx.params
    long = setup.direction == "long"
    entry, stop = setup.entry, setup.stop
    risk = (entry - stop) if long else (stop - entry)
    setup.risk_per_share = risk
    setup.targets = _targets(ctx, setup.direction, entry, risk) if risk > 0 else []
    reward = abs(setup.targets[0] - entry) if setup.targets else 0.0
    # Rounded so a measured 2R target isn't rejected by floating-point noise.
    setup.risk_reward = round(reward / risk, 2) if risk > 0 else 0.0
    extension = (ctx.price - entry if long else entry - ctx.price) / ctx.atr if ctx.atr else 0.0

    checks = list(extra_checks)
    checks.append(Check(f"Reward/risk >= {p['min_rr']}:1", setup.risk_reward >= p["min_rr"], True,
                        f"{setup.risk_reward:.2f}:1 to first target"))
    checks.append(Check("Stop distance sensible (0.3-3 ATR)", 0.3 <= risk / ctx.atr <= 3.0
                        if ctx.atr else False, True, f"Risk {risk:.2f} = {risk / ctx.atr:.2f} ATR"
                        if ctx.atr else "ATR unavailable"))
    checks.append(Check("Not extended past entry", extension <= p["max_ext"], True,
                        f"Price is {max(extension, 0):.2f} ATR beyond entry"
                        f" (max {p['max_ext']})"))
    setup.checklist = checks

    required = [c for c in checks if c.required]
    optional = [c for c in checks if not c.required]
    req_pass = sum(c.passed for c in required) / max(len(required), 1)
    opt_pass = sum(c.passed for c in optional) / max(len(optional), 1) if optional else 1.0
    setup.confidence = round(100 * (0.7 * req_pass + 0.3 * opt_pass))

    failed_required = [c for c in required if not c.passed]
    if setup.risk_reward < p["min_rr"]:
        setup.status = NO_TRADE
        setup.reasons.append(
            f"Reward/risk is only {setup.risk_reward:.2f}:1 (minimum {p['min_rr']}:1) — "
            "the nearest opposing level is too close.")
    elif extension > p["max_ext"]:
        setup.status = NO_TRADE
        setup.reasons.append("Price has already run past the entry — do not chase; "
                             "wait for a retest or a new setup.")
    elif len(failed_required) > 1:
        setup.status = NO_TRADE
        setup.reasons.append("Too many required conditions are missing: " + "; ".join(
            f"{c.label.lower()} ({c.detail})" for c in failed_required) + ".")
    elif setup.triggered and not failed_required:
        setup.status = ACTIONABLE
        setup.reasons.append("Trigger fired on a closed bar and all required checks pass.")
    else:
        setup.status = FORMING
        if not setup.triggered:
            word = "above" if long else "below"
            setup.reasons.append(f"Waiting for trigger: a close {word} {entry:.2f}.")
        for c in failed_required:
            setup.reasons.append(f"Missing: {c.label.lower()} ({c.detail}).")
    return setup


def _candle_strength(bar: Bar) -> float:
    """Where the close sits in the bar's range: 0 = at the low, 1 = at the high."""
    rng = bar.high - bar.low
    return (bar.close - bar.low) / rng if rng > 0 else 0.5


def _crossed(ctx: Context, level: float, direction: str, lookback: int = 3) -> bool:
    closes = [b.close for b in ctx.bars[-(lookback + 1):]]
    if direction == "long":
        return closes[-1] > level and min(closes[:-1]) <= level
    return closes[-1] < level and max(closes[:-1]) >= level


# ----------------------------------------------------------------- setup detectors

def detect_breakout(ctx: Context, direction: str) -> TradeSetup | None:
    long = direction == "long"
    buf = ctx.atr * 0.1
    crossed = [l for l in ctx.levels if _crossed(ctx, l.price, direction)]
    if crossed:
        level = max(crossed, key=lambda l: l.price) if long else min(crossed, key=lambda l: l.price)
        triggered = True
    else:
        # Approaching: within 0.6 ATR of the nearest level in the trade direction.
        side = [l for l in ctx.levels if (l.price > ctx.price if long else l.price < ctx.price)]
        if not side:
            return None
        level = min(side, key=lambda l: abs(l.price - ctx.price))
        if abs(level.price - ctx.price) > ctx.atr * 0.6:
            return None
        triggered = False
    expected_bias = 1 if long else -1
    if ctx.bias_score * expected_bias < 2:
        return None  # breakouts only in the direction of a clear prevailing bias

    entry = level.price + buf if long else level.price - buf
    stop = level.price - ctx.atr * 0.6 if long else level.price + ctx.atr * 0.6
    name = "Breakout" if long else "Breakdown"
    word = "above" if long else "below"
    setup = TradeSetup(
        status=FORMING, setup_type="breakout" if long else "breakdown",
        title=f"{'Bullish' if long else 'Bearish'} {name.lower()} {word} {level.price:.2f}",
        direction=direction, entry=entry, stop=stop, triggered=triggered,
        invalidation=f"A close back {'below' if long else 'above'} {level.price:.2f} "
                     f"({level.source.lower()}) fails the {name.lower()}.",
    )
    strength = _candle_strength(ctx.last)
    checks = [
        Check("Trend aligned with direction", ctx.bias_score * expected_bias >= 3, True,
              f"Bias score {ctx.bias_score:+d}"),
        Check("Breakout volume >= 1.3x average", ctx.vol_ratio >= 1.3, True,
              f"{ctx.vol_ratio:.2f}x 20-bar average"),
        Check("Strong close in direction", strength >= 0.6 if long else strength <= 0.4, False,
              f"Close at {strength * 100:.0f}% of bar range"),
        Check("RSI not exhausted", ctx.rsi < 75 if long else ctx.rsi > 25, False,
              f"RSI {ctx.rsi:.1f}"),
        Check(f"Level is meaningful ({level.source})", level.strength >= 0.5, False,
              f"{level.touches} touch(es), strength {level.strength:.2f}"),
    ]
    if ctx.vwap is not None:
        ok = ctx.price > ctx.vwap if long else ctx.price < ctx.vwap
        checks.append(Check(f"Price {word} VWAP", ok, False, f"VWAP {ctx.vwap:.2f}"))
    return _finish(ctx, setup, checks)


def detect_pullback(ctx: Context, direction: str) -> TradeSetup | None:
    long = direction == "long"
    sign = 1 if long else -1
    if ctx.bias_score * sign < 3:
        return None  # continuation setups need an established trend
    recent = ctx.bars[-4:]
    touch_zone = ctx.atr * 0.3
    supports = [ctx.ema_fast, ctx.ema_slow] + [
        l.price for l in ctx.levels if (l.price < ctx.price if long else l.price > ctx.price)]
    if long:
        extreme = min(b.low for b in recent)
        touched = [s for s in supports if abs(extreme - s) <= touch_zone or (extreme < s < ctx.price)]
    else:
        extreme = max(b.high for b in recent)
        touched = [s for s in supports if abs(extreme - s) <= touch_zone or (ctx.price < s < extreme)]
    if not touched:
        return None
    # Still on the right side of the slow EMA -- otherwise it's a breakdown, not a pullback.
    if (long and ctx.price < ctx.ema_slow) or (not long and ctx.price > ctx.ema_slow):
        return None

    trigger_bar = ctx.prev
    triggered = ctx.last.close > trigger_bar.high if long else ctx.last.close < trigger_bar.low
    if triggered:
        entry = trigger_bar.high if long else trigger_bar.low
    else:
        entry = ctx.last.high + ctx.atr * 0.05 if long else ctx.last.low - ctx.atr * 0.05
    stop = extreme - ctx.atr * 0.25 if long else extreme + ctx.atr * 0.25
    anchor = max(touched) if long else min(touched)
    setup = TradeSetup(
        status=FORMING, setup_type="pullback_long" if long else "pullback_short",
        title=f"{'Bullish' if long else 'Bearish'} trend pullback to {anchor:.2f}",
        direction=direction, entry=entry, stop=stop, triggered=triggered,
        invalidation=f"A move {'below' if long else 'above'} the pullback "
                     f"{'low' if long else 'high'} {extreme:.2f} ends the setup.",
    )
    rsi_ok = 38 <= ctx.rsi <= 62
    checks = [
        Check("Established trend", ctx.bias_score * sign >= 3, True, f"Bias score {ctx.bias_score:+d}"),
        Check("Pulled back into support zone" if long else "Pulled back into resistance zone",
              True, True, f"Tested {anchor:.2f} (EMA or key level)"),
        Check("RSI reset to neutral (38-62)", rsi_ok, False, f"RSI {ctx.rsi:.1f}"),
        Check("Confirmation candle", (ctx.last.close > ctx.last.open) if long
              else (ctx.last.close < ctx.last.open), False,
              "Last bar closed " + ("green" if ctx.last.close > ctx.last.open else "red")),
    ]
    return _finish(ctx, setup, checks)


def detect_reversal(ctx: Context, direction: str) -> TradeSetup | None:
    long = direction == "long"
    last, prev = ctx.last, ctx.prev
    zone = ctx.atr * 0.35
    if long:
        near = [l for l in ctx.levels if l.price <= ctx.price + zone
                and abs(min(last.low, prev.low) - l.price) <= zone]
    else:
        near = [l for l in ctx.levels if l.price >= ctx.price - zone
                and abs(max(last.high, prev.high) - l.price) <= zone]
    if not near:
        return None
    level = max(near, key=lambda l: l.strength)
    rsi_recent = [v for v in ctx.rsi_series[-6:] if v is not None]
    if not rsi_recent:
        return None
    if long:
        stretched = min(rsi_recent) < 33 and ctx.rsi > min(rsi_recent)
    else:
        stretched = max(rsi_recent) > 67 and ctx.rsi < max(rsi_recent)
    if not stretched:
        return None

    body = abs(last.close - last.open)
    lower_wick = min(last.open, last.close) - last.low
    upper_wick = last.high - max(last.open, last.close)
    if long:
        hammer = lower_wick >= 2 * body and _candle_strength(last) >= 0.6
        engulf = last.close > last.open and prev.close < prev.open and \
            last.close >= prev.open and last.open <= prev.close
    else:
        hammer = upper_wick >= 2 * body and _candle_strength(last) <= 0.4
        engulf = last.close < last.open and prev.close > prev.open and \
            last.close <= prev.open and last.open >= prev.close
    pattern = "hammer" if hammer else "engulfing" if engulf else None
    if long:
        triggered = pattern is not None and last.close > prev.high
        entry = prev.high if triggered else last.high + ctx.atr * 0.05
        stop = min(last.low, prev.low, level.price) - ctx.atr * 0.2
    else:
        triggered = pattern is not None and last.close < prev.low
        entry = prev.low if triggered else last.low - ctx.atr * 0.05
        stop = max(last.high, prev.high, level.price) + ctx.atr * 0.2

    sign = 1 if long else -1
    setup = TradeSetup(
        status=FORMING, setup_type="reversal_long" if long else "reversal_short",
        title=f"{'Bullish' if long else 'Bearish'} reversal at {level.price:.2f} "
              f"{'support' if long else 'resistance'}",
        direction=direction, entry=entry, stop=stop, triggered=triggered,
        invalidation=f"A close {'below' if long else 'above'} {level.price:.2f} "
                     f"means {'support' if long else 'resistance'} failed.",
    )
    checks = [
        Check(f"At key {'support' if long else 'resistance'} ({level.source})", True, True,
              f"{level.price:.2f}, strength {level.strength:.2f}"),
        Check("RSI stretched and turning", stretched, True,
              f"RSI {ctx.rsi:.1f} (recent extreme {min(rsi_recent) if long else max(rsi_recent):.1f})"),
        Check("Reversal candle pattern", pattern is not None, True,
              f"Pattern: {pattern}" if pattern else "No hammer/engulfing yet"),
        Check("Not fighting a strong trend", ctx.bias_score * sign > -5, False,
              f"Counter-trend trade; bias score {ctx.bias_score:+d}"),
    ]
    return _finish(ctx, setup, checks)


DETECTORS = [detect_breakout, detect_pullback, detect_reversal]


# ---------------------------------------------------------------------- analysis

def build_context(bars: list[Bar], mode: str, bar_minutes: int, bias: dict,
                  levels: list[Level]) -> Context:
    p = MODE_PARAMS[mode]
    closes = [b.close for b in bars]
    vols = [b.volume for b in bars]
    rsi_series = ind.rsi(closes)
    avg_vol = ind.last(ind.sma(vols[:-1], 20)) if len(vols) > 20 else None
    session_tools = bar_minutes <= SESSION_TOOLS_MAX_MINUTES
    return Context(
        bars=bars, mode=mode, price=closes[-1],
        atr=ind.last(ind.atr(bars)) or (bars[-1].high - bars[-1].low) or closes[-1] * 0.01,
        rsi=ind.last(rsi_series) or 50.0,
        ema_fast=ind.last(ind.ema(closes, p["fast"])) or closes[-1],
        ema_slow=ind.last(ind.ema(closes, p["slow"])) or closes[-1],
        vwap=ind.session_vwap(bars)[-1] if mode == "day" and session_tools else None,
        long_ma=ind.last(ind.sma(closes, p["long"])) if p["long"] else None,
        vol_ratio=(vols[-1] / avg_vol) if avg_vol else 1.0,
        bias_score=bias["score"], levels=levels, rsi_series=rsi_series,
    )


def no_trade(ctx: Context, levels: list[Level], extra: list[str] | None = None) -> TradeSetup:
    reasons = list(extra or [])
    if abs(ctx.bias_score) <= 1:
        reasons.append(f"Bias is neutral (score {ctx.bias_score:+d}) — no trend to lean on.")
    above = min((l for l in levels if l.price > ctx.price), key=lambda l: l.price, default=None)
    below = max((l for l in levels if l.price < ctx.price), key=lambda l: l.price, default=None)
    if above and below:
        reasons.append(f"Price {ctx.price:.2f} is between support {below.price:.2f} and "
                       f"resistance {above.price:.2f}.")
    if above:
        reasons.append(f"Watch for a breakout above {above.price:.2f} ({above.source.lower()}).")
    if below:
        reasons.append(f"Watch for a breakdown below {below.price:.2f} ({below.source.lower()}).")
    return TradeSetup(status=NO_TRADE, setup_type="none", title="No trade — stand aside",
                      reasons=reasons or ["No setup conditions are present."])


def analyze(symbol: str, bars: list[Bar], mode: str, timeframe: str) -> dict:
    if mode not in MODE_PARAMS:
        raise ValueError(f"Unknown mode {mode!r}")
    if len(bars) < 60:
        raise ValueError("At least 60 bars are required for analysis")
    p = MODE_PARAMS[mode]
    closes = [b.close for b in bars]
    atr_series = ind.atr(bars)
    atr_value = ind.last(atr_series) or closes[-1] * 0.01
    bar_minutes = TIMEFRAMES[timeframe]
    all_levels = compute_levels(bars, atr_value, mode, bar_minutes, max_per_side=None)
    key_levels = compute_levels(bars, atr_value, mode, bar_minutes, max_per_side=4)
    bias = market_bias(bars, mode, bar_minutes)
    ctx = build_context(bars, mode, bar_minutes, bias, all_levels)

    candidates: list[TradeSetup] = []
    for detector in DETECTORS:
        for direction in ("long", "short"):
            found = detector(ctx, direction)
            if found is not None:
                candidates.append(found)
    candidates.sort(key=lambda s: (_STATUS_RANK[s.status], s.confidence), reverse=True)

    if candidates and candidates[0].status != NO_TRADE:
        primary = candidates[0]
    else:
        rejected = [f"{c.title}: {c.reasons[0]}" for c in candidates if c.reasons]
        primary = no_trade(ctx, key_levels, rejected[:2])

    def series(values: ind.Series) -> list[dict]:
        return [{"time": b.time, "value": round(v, 4)} for b, v in zip(bars, values) if v is not None]

    overlays = {
        f"ema{p['fast']}": series(ind.ema(closes, p["fast"])),
        f"ema{p['slow']}": series(ind.ema(closes, p["slow"])),
    }
    if ctx.vwap is not None:
        overlays["vwap"] = series(ind.session_vwap(bars))
    if p["long"]:
        overlays[f"sma{p['long']}"] = series(ind.sma(closes, p["long"]))

    return {
        "symbol": symbol,
        "mode": mode,
        "timeframe": timeframe,
        "as_of": bars[-1].time,
        "price": closes[-1],
        "indicators": {
            "atr": round(atr_value, 4),
            "rsi": round(ctx.rsi, 2),
            "ema_fast": round(ctx.ema_fast, 2),
            "ema_slow": round(ctx.ema_slow, 2),
            "ema_fast_period": p["fast"],
            "ema_slow_period": p["slow"],
            "vwap": round(ctx.vwap, 2) if ctx.vwap is not None else None,
            "sma_long": round(ctx.long_ma, 2) if ctx.long_ma is not None else None,
            "volume_ratio": round(ctx.vol_ratio, 2),
        },
        "bias": bias,
        "levels": [l.to_dict() for l in key_levels],
        "setup": primary.to_dict(),
        "alternatives": [c.to_dict() for c in candidates if c is not primary][:3],
        "overlays": overlays,
    }
