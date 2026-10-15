"""When could a historical signal actually have been entered? The live scanner's rules.

A replayed signal is a historical trigger (detection). A backtest trade is taken only at
the first execution candle at which the live scanner would have shown the same setup as
CURRENTLY_ACTIONABLE (`strategies.entry.entry_checks`, the very function the scanner uses),
with the information a live user of the feed had at that moment:

* decision time = the execution candle's open (daily candles: the session open), never
  before the signal candle's data reached a live user (close + the feed's delay);
* the engine is re-evaluated with data up to (decision time - feed delay), with the same
  point-in-time volume overlay and liquidity as live (`replay.pit_inputs`), so the volume
  confirmation is what the scanner showed then (an IEX-only UNVERIFIED trigger can become
  FULL once consolidated volume covers it, 16 minutes after its close);
* the "live price" is the execution candle's open - the price the market order would get.

The search stops at the first CURRENTLY_ACTIONABLE candle (entry), when a check fails that
can never pass again (trigger too old, stop/T1/invalidation reached, entry window over),
when the setup is no longer TRIGGERED, or at the session end for day trades.
"""

from __future__ import annotations

from datetime import datetime

from ..market.calendar import NY, MarketCalendar
from ..market.service import COMPLETION_GRACE_SECONDS
from ..strategies.entry import entry_checks, entry_env_at, permanently_failed
from .config import EXEC_TF, BacktestConfig
from .replay import _evaluate


def _day(ts: int):
    return datetime.fromtimestamp(ts, NY).date()


def decision_time(bar_time: int, mode: str, cal: MarketCalendar) -> int:
    """When a market order on this execution candle is placed: intraday candles at their
    start, daily candles at that session's open."""
    if mode == "day":
        return bar_time
    sd = cal.day(_day(bar_time))
    return int(sd.open.timestamp()) if sd else bar_time


def find_entry(symbol: str, sig: dict, mode: str, series: dict, cal: MarketCalendar,
               ctx_cfg: dict, cfg: BacktestConfig, sip: dict | None = None
               ) -> tuple[int | None, dict]:
    """(index into the execution candles after the signal, info) of the first candle at
    which the signal was CURRENTLY_ACTIONABLE; (None, skip info) if it never was."""
    planning = ctx_cfg["planning"]
    gates = ctx_cfg["strategy"].gates
    delay = int(ctx_cfg.get("feed_delay", 0))
    grace = COMPLETION_GRACE_SECONDS
    bars = series[EXEC_TF[mode]].after(sig["confirmed_at"])
    visible = sig["confirmed_at"] + delay
    i = next((k for k, b in enumerate(bars) if decision_time(b.time, mode, cal) >= visible),
             len(bars))
    i += cfg.entry_delay_bars
    last: list[dict] = []
    last_t = None
    while i < len(bars):
        b = bars[i]
        if mode == "day" and _day(b.time) != _day(sig["confirmed_at"]):
            why = ("signal at the session close: no intraday entry possible" if not last
                   else "never currently actionable before the session close")
            return None, {"reason": why, "entry_checks": last, "decided_at": last_t}
        t = decision_time(b.time, mode, cal)
        data_ts = t + grace - delay
        setups = _evaluate(symbol, series, mode, data_ts, cal, ctx_cfg, sip)
        s = next((x for x in setups if x["id"] == sig["id"]), None)
        if s is None or s["status"] != "TRIGGERED":
            lineage = (sig["strategy"], sig["timeframe"], sig["direction"])
            now = s or next((x for x in setups
                             if (x["strategy"], x["timeframe"], x["direction"]) == lineage), {})
            why = (now.get("reasons") or ["setup not evaluated"])[0]
            failed = [c["name"] for c in last if not c["passed"]]
            reason = ("never currently actionable while TRIGGERED: " + ", ".join(failed)
                      if failed else "no longer TRIGGERED at the first entry opportunity")
            return None, {"reason": reason, "detail": why, "entry_checks": last,
                          "decided_at": t}
        env = entry_env_at(datetime.fromtimestamp(t + grace, NY), cal, gates, planning)
        live = {"price": b.open, "time": t,
                "day_low": b.open if mode == "swing" else None,
                "day_high": b.open if mode == "swing" else None}
        checks = entry_checks(s, mode, live, env)
        if all(c["passed"] for c in checks):
            return i, {"setup": s, "entry_checks": checks, "decided_at": t}
        last, last_t = checks, t
        if permanently_failed(checks):
            break
        i += 1
    failed = [c["name"] for c in last if not c["passed"]]
    return None, {"reason": "never currently actionable: " + (", ".join(failed) or
                                                              "no candle after the signal"),
                  "entry_checks": last, "decided_at": last_t}
