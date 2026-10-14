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
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from ..market.calendar import NY, MarketCalendar
from ..market.service import COMPLETION_GRACE_SECONDS
from ..market.timeframes import lookback_start, spec
from ..strategies.engine import evaluate_symbol
from ..swing.engine import evaluate_swing_symbol
from .config import DAY_FRAMES, SWING_FRAMES
from .data import HistoryProvider, Integrity, Series, validate_series

SWING_DISCOVERY_STEP = 20  # sessions between discovery passes (< the 60-candle live scan)


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


def _evaluate(symbol, series, mode, as_of_ts, cal, ctx_cfg) -> list[dict]:
    """One live-engine evaluation at `as_of_ts` (setup dicts, as the service returns them)."""
    as_of = _dt(as_of_ts)
    frames = _frames(series, mode, as_of_ts)
    if mode == "day":
        _, setups = evaluate_symbol(symbol, frames, as_of, cal, ctx_cfg["strategy"], [],
                                    strategies=ctx_cfg.get("strategies"),
                                    planning=ctx_cfg["planning"])
        return [s.to_dict() for s in setups]
    _, setups = evaluate_swing_symbol(symbol, frames, as_of, cal, ctx_cfg["swing"], [],
                                      strategies=ctx_cfg.get("swing_strategies"),
                                      is_demo=False, planning=ctx_cfg["planning"])
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
    }


def replay_symbol(symbol: str, mode: str, start: date, end: date, series: dict[str, Series],
                  cal: MarketCalendar, ctx_cfg: dict, exhaustive: bool = False,
                  progress=None) -> tuple[list[dict], list[dict], int]:
    """Signals for one symbol: ([signal], [not-actionable-live trigger], evaluations)."""
    sessions = cal.trading_days(start, end)
    lo = int(datetime.combine(start, datetime.min.time(), NY).timestamp())
    hi = int(sessions[-1].close.timestamp()) if sessions else lo
    grace = COMPLETION_GRACE_SECONDS
    signals: dict[str, dict] = {}
    rejected: list[dict] = []
    evals = 0

    def record_at(ts: int, want: set | None = None) -> None:
        nonlocal evals
        evals += 1
        live: dict[tuple, dict] = {}
        for s in _evaluate(symbol, series, mode, ts + grace, cal, ctx_cfg):
            key = (s["strategy"], s["timeframe"], s["direction"])
            live[key] = s
            if s["status"] == "TRIGGERED" and s.get("confirmed_at") == ts \
                    and s["id"] not in signals:
                signals[s["id"]] = _signal(s, mode, ts + grace)
                if want is not None:
                    want.discard(key)
        for key in sorted(want or ()):
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
    pending: dict[int, set] = {}
    if mode == "day":
        checkpoints = [int(sd.close.timestamp()) for sd in sessions]
    else:
        checkpoints = [int(sd.close.timestamp()) for sd in sessions[::SWING_DISCOVERY_STEP]]
        if sessions:
            checkpoints.append(int(sessions[-1].close.timestamp()))
    for i, cp in enumerate(checkpoints):
        evals += 1
        for s in _evaluate(symbol, series, mode, cp + grace, cal, ctx_cfg):
            for t in _triggers(s, mode):
                if t.get("accepted") and lo <= t["confirmed_at"] <= hi:
                    pending.setdefault(t["confirmed_at"], set()).add(
                        (s["strategy"], s["timeframe"], s["direction"]))
        if progress:
            progress(i + 1, len(checkpoints) * 2)
    # ---- pass 2: confirmation with the live engine at each confirmation time
    for i, ts in enumerate(sorted(pending)):
        record_at(ts, set(pending[ts]))
        if progress:
            progress(len(checkpoints) + int((i + 1) / max(len(pending), 1) * len(checkpoints)),
                     len(checkpoints) * 2)
    return sorted(signals.values(), key=lambda x: (x["seen_at"], x["id"])), rejected, evals
