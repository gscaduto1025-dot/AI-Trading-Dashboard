"""Chronological replay of the LIVE strategy engines to produce historical signals.

The day engine (`app.strategies.engine.evaluate_symbol`) and the swing engine
(`app.swing.engine.evaluate_swing_symbol`) are called exactly as the live service calls
them, with the same planning rules, the same lookback windows and only the candles that
were complete at the replay time. There are no backtest-only signal rules.

Two passes per symbol (default):
1. Discovery: evaluate at each session close (day) or every 20 sessions (swing, which scans
   its last 60 candles) to list the triggers the engines accepted.
2. Confirmation: re-evaluate at each trigger's confirmation time (`as_of` = the trigger
   candle's close + the live completion grace). A signal is recorded only if the engine
   reported that setup as TRIGGERED at that moment; its plan, quality and status are the
   ones the dashboard would have shown then.
`exhaustive=True` instead evaluates at every candle close; tests assert both produce the
same signals.

Point-in-time inputs (`pit_inputs`), identical to the live service at the same moment:
* the engine sees candles completed by the data cutoff (decision time minus the feed's
  delay, e.g. 15 minutes for delayed SIP);
* on an IEX feed, consolidated (SIP) volume is overlaid only on candles that ended at least
  16 minutes before the decision time (`market.service.sip_covered_until`), newer candles
  keep the IEX proxy (UNVERIFIED / LIMITED per partial_volume_policy);
* the liquidity gate uses consolidated daily volume of the 20 sessions before the one in
  progress (`market.service.liquidity_from_daily`).
A signal found here is a historical trigger; whether and when it could be entered is
decided separately by the live entry rules (`backtest.eligibility`).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from ..market.calendar import NY, MarketCalendar
from ..market.service import COMPLETION_GRACE_SECONDS, liquidity_from_daily, sip_covered_until
from ..market.timeframes import lookback_start, spec
from ..strategies.engine import evaluate_symbol
from ..strategies.volume import VolumeOverlay, VolumeSource
from ..swing.engine import evaluate_swing_symbol
from .config import DAY_FRAMES, SWING_FRAMES
from .data import HistoryProvider, Integrity, Series, validate_series

SWING_DISCOVERY_STEP = 20  # sessions between discovery passes (< the 60-candle live scan)
# Signal timeframes that get the SIP volume overlay, as in the live service.
OVERLAY_TFS = {"day": ("5Min", "15Min"), "swing": ("1Day", "1Week")}


def _dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, NY)


def load_series(provider: HistoryProvider, symbol: str, mode: str, start: date, end: date,
                cal: MarketCalendar, only: tuple[str, ...] | None = None
                ) -> tuple[dict[str, Series], list[Integrity]]:
    """Every timeframe the engine needs, from enough history before `start` to fill the
    live lookback windows, up to the close of `end`."""
    frames = DAY_FRAMES if mode == "day" else SWING_FRAMES
    end_sd = cal.trading_days(end - timedelta(days=7), end)
    end_dt = end_sd[-1].close if end_sd else datetime.combine(end, datetime.max.time(), NY)
    start_dt = datetime.combine(start, datetime.min.time(), NY)
    series, reports = {}, []
    for tf, limit in frames.items():
        if only and tf not in only:
            continue
        lb = lookback_start(spec(tf), cal, start_dt, limit, "regular")
        try:
            raw = provider.bars(symbol, tf, lb, end_dt + timedelta(days=1))
        except Exception as exc:  # optional supporting timeframes may be unavailable
            if (mode == "swing" and tf in ("4Hour", "1Hour")):
                reports.append(Integrity(symbol, tf, issues=[f"unavailable: {exc}"]))
                raw = []
            else:
                raise
        bars, rep = validate_series(symbol, tf, raw, cal)
        series[tf] = Series(tf, bars, cal)
        reports.append(rep)
    return series, reports


def _frames(series: dict[str, Series], mode: str, as_of_ts: int) -> dict:
    limits = DAY_FRAMES if mode == "day" else SWING_FRAMES
    return {tf: series[tf].completed(as_of_ts, limits[tf]) for tf in limits if tf in series}


def load_sip(provider: HistoryProvider, symbol: str, mode: str, start: date, end: date,
             cal: MarketCalendar) -> tuple[dict | None, list[str]]:
    """Consolidated history for a partial-volume (IEX) provider: {tf: {start: volume}} for
    the overlay timeframes and {"daily": [Bar]} for liquidity. None when the provider is
    not partial; missing pieces are reported (those decisions then use the IEX proxy)."""
    if getattr(provider, "volume_scope", "consolidated") != "partial":
        return None, []
    frames = DAY_FRAMES if mode == "day" else SWING_FRAMES
    end_sd = cal.trading_days(end - timedelta(days=7), end)
    end_dt = (end_sd[-1].close if end_sd else datetime.combine(end, datetime.max.time(), NY)) \
        + timedelta(days=1)
    start_dt = datetime.combine(start, datetime.min.time(), NY)
    out: dict = {}
    notes = []
    for tf in set(OVERLAY_TFS[mode]) | {"1Day"}:
        lb = lookback_start(spec(tf), cal, start_dt, frames.get(tf, 60), "regular")
        bars = provider.sip_bars(symbol, tf, lb, end_dt)
        if bars is None:
            notes.append(f"consolidated {tf} volume unavailable: IEX-only volume labelled "
                         f"UNVERIFIED/LIMITED, liquidity on IEX volume")
            continue
        out[tf] = {b.time: b.volume for b in bars}
        if tf == "1Day":
            out["daily"] = sorted(bars, key=lambda b: b.time)
    return out, notes


def pit_inputs(ctx_cfg: dict, sip: dict | None, mode: str, data_ts: int, decided_ts: int,
               cal: MarketCalendar) -> tuple[VolumeSource | None, dict | None]:
    """(VolumeSource, liquidity) a live user of this feed would have had at `decided_ts`
    (wall clock) with data up to `data_ts` - built exactly like Service.volume_source and
    MarketDataService.consolidated_liquidity."""
    scope = ctx_cfg.get("volume_scope", "consolidated")
    policy = ctx_cfg["planning"].partial_volume_policy
    label = ctx_cfg.get("volume_label", "feed")
    if scope != "partial":
        return VolumeSource(scope, label, policy, None), None
    decided = _dt(decided_ts)
    overlays = {}
    for tf in OVERLAY_TFS[mode]:
        vols = (sip or {}).get(tf)
        overlays[tf] = None if vols is None else VolumeOverlay(vols, sip_covered_until(decided, tf))
    liq = None
    if sip and sip.get("daily") is not None:
        current = cal.last_session_started(_dt(data_ts), "regular").day
        liq = liquidity_from_daily(sip["daily"], current)
    return VolumeSource(scope, label, policy, overlays), liq


def proxy_only(volume: VolumeSource | None) -> VolumeSource | None:
    """The same source with no consolidated overlay (every candle judged on IEX only)."""
    if volume is None or volume.scope != "partial":
        return None
    return VolumeSource(volume.scope, volume.feed_label, volume.policy,
                        {tf: None for tf in (volume.overlays or {})})


def _evaluate(symbol, series, mode, as_of_ts, cal, ctx_cfg, sip=None,
              volume_view: str = "pit") -> list[dict]:
    """One live-engine evaluation with data up to `as_of_ts` (setup dicts, as the service
    returns them). The decision moment is as_of + the feed's delay. volume_view "proxy"
    drops the SIP overlay (discovery only)."""
    as_of = _dt(as_of_ts)
    frames = _frames(series, mode, as_of_ts)
    decided = as_of_ts + int(ctx_cfg.get("feed_delay", 0))
    volume, liquidity = pit_inputs(ctx_cfg, sip, mode, as_of_ts, decided, cal)
    if volume_view == "proxy":
        volume = proxy_only(volume)
    if mode == "day":
        _, setups = evaluate_symbol(symbol, frames, as_of, cal, ctx_cfg["strategy"], [],
                                    strategies=ctx_cfg.get("strategies"),
                                    planning=ctx_cfg["planning"], liquidity=liquidity,
                                    volume=volume)
        return [s.to_dict() for s in setups]
    _, setups = evaluate_swing_symbol(symbol, frames, as_of, cal, ctx_cfg["swing"], [],
                                      strategies=ctx_cfg.get("swing_strategies"),
                                      is_demo=False, planning=ctx_cfg["planning"],
                                      liquidity=liquidity, volume=volume)
    return setups


def _triggers(s: dict, mode: str) -> list[dict]:
    return s.get("triggers_today", []) if mode == "day" else s.get("triggers", [])


def _signal(s: dict, mode: str, seen_ts: int) -> dict:
    plan = s.get("plan") or {}
    p = lambda k: (plan.get(k) or {}).get("price")  # noqa: E731
    zone = plan.get("entry_zone")
    return {
        "id": s["id"], "ticker": s["ticker"], "mode": mode, "strategy": s["strategy"],
        "strategy_name": s["strategy_name"], "timeframe": s["timeframe"],
        "direction": s["direction"], "trigger_time": s["trigger_time"],
        "confirmed_at": s["confirmed_at"], "seen_at": seen_ts,
        "entry_trigger": p("entry_trigger"), "plan_entry": p("entry"), "stop": p("stop"),
        "target1": p("target1"), "target2": p("target2"),
        "entry_zone": [zone["low"], zone["high"]] if zone else None,
        "planned_rr": plan.get("risk_reward_t1"),
        "quality": (plan.get("quality") or {}).get("score"),
        "reason": (s.get("reasons") or [""])[0],
        "volume_confirmation": s.get("volume_confirmation"),
    }


def replay_symbol(symbol: str, mode: str, start: date, end: date, series: dict[str, Series],
                  cal: MarketCalendar, ctx_cfg: dict, exhaustive: bool = False,
                  progress=None, sip: dict | None = None) -> tuple[list[dict], list[dict], int]:
    """Signals for one symbol: ([signal], [not-actionable-live trigger], evaluations)."""
    sessions = cal.trading_days(start, end)
    lo = int(datetime.combine(start, datetime.min.time(), NY).timestamp())
    hi = int(sessions[-1].close.timestamp()) if sessions else lo
    grace = COMPLETION_GRACE_SECONDS
    signals: dict[str, dict] = {}
    rejected: list[dict] = []
    evals = 0
    quiet: set = set()  # candidates only the IEX-only view found (not reported if dropped)

    def record_at(ts: int, want: set | None = None) -> None:
        nonlocal evals
        evals += 1
        live: dict[tuple, dict] = {}
        delay = int(ctx_cfg.get("feed_delay", 0))
        for s in _evaluate(symbol, series, mode, ts + grace, cal, ctx_cfg, sip):
            key = (s["strategy"], s["timeframe"], s["direction"])
            live[key] = s
            if s["status"] == "TRIGGERED" and s.get("confirmed_at") == ts \
                    and s["id"] not in signals:
                # Visible to a live user once the feed delivers the candle.
                signals[s["id"]] = _signal(s, mode, ts + grace + delay)
                if want is not None:
                    want.discard(key)
                    quiet.discard(key)
        for key in sorted((want or set()) - quiet):
            s = live.get(key) or {}
            rejected.append({"ticker": symbol, "strategy": key[0], "timeframe": key[1],
                             "direction": key[2], "confirmed_at": ts,
                             "live_status": s.get("status"),
                             "reason": "The live engine did not show this trigger as TRIGGERED "
                                       "at its confirmation: "
                                       + ((s.get("reasons") or ["no setup"])[0])})

    if exhaustive:
        step_tf = "5Min" if mode == "day" else "1Day"
        for e in series[step_tf].ends:
            if lo <= e <= hi:
                record_at(e)
        return sorted(signals.values(), key=lambda x: (x["seen_at"], x["id"])), rejected, evals

    # ---- pass 1: discovery of accepted triggers (by confirmation time)
    # Volume readings depend on the moment (SIP covers a candle only 16 minutes after it
    # closes), so a trigger accepted on IEX volume at its confirmation can read differently
    # at a later checkpoint. Discovery therefore also evaluates the IEX-only view; such
    # extra candidates are confirmed (or silently dropped) by pass 2 at their true time.
    pending: dict[int, set] = {}
    views = ("pit", "proxy") if ctx_cfg.get("volume_scope") == "partial" else ("pit",)
    if mode == "day":
        checkpoints = [int(sd.close.timestamp()) for sd in sessions]
    else:
        checkpoints = [int(sd.close.timestamp()) for sd in sessions[::SWING_DISCOVERY_STEP]]
        if sessions:
            checkpoints.append(int(sessions[-1].close.timestamp()))
    found: dict[int, dict[tuple, str]] = {}
    for i, cp in enumerate(checkpoints):
        for view in views:
            evals += 1
            for s in _evaluate(symbol, series, mode, cp + grace, cal, ctx_cfg, sip, view):
                for t in _triggers(s, mode):
                    if t.get("accepted") and lo <= t["confirmed_at"] <= hi:
                        key = (s["strategy"], s["timeframe"], s["direction"])
                        seen = found.setdefault(t["confirmed_at"], {})
                        if seen.get(key) != "pit":
                            seen[key] = view
        if progress:
            progress(i + 1, len(checkpoints) * 2)
    for ts, keys in found.items():
        pending[ts] = set(keys)
    # ---- pass 2: confirmation with the live engine at each confirmation time
    for i, ts in enumerate(sorted(pending)):
        quiet.clear()
        quiet.update(k for k, v in found[ts].items() if v == "proxy")
        record_at(ts, set(pending[ts]))
        if progress:
            progress(len(checkpoints) + int((i + 1) / max(len(pending), 1) * len(checkpoints)),
                     len(checkpoints) * 2)
    return sorted(signals.values(), key=lambda x: (x["seen_at"], x["id"])), rejected, evals
