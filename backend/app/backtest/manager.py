"""Runs backtests in a background thread (one at a time) and stores results in SQLite."""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from datetime import date, datetime

from ..market.calendar import NY
from ..strategies.volume import VolumeSource
from .config import EXEC_TF, BacktestConfig
from .data import BUNDLED, BundledHistory, MarketHistory, validate_series
from .runner import LIMITATIONS, MAX_DAYS, report, run_backtest

log = logging.getLogger("tradescope.backtest")
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
MAX_SYMBOLS = 30


class BacktestBusy(Exception):
    pass


class BacktestManager:
    def __init__(self, service) -> None:
        self.service = service
        self.db = service.db
        self._lock = threading.Lock()
        self.current: dict | None = None  # {"id", "progress", "message", "started"}
        self.db.interrupt_running_backtests()

    def sources(self) -> dict:
        svc = self.service
        return {
            "market": {"label": "Market data (" + ("DEMO - simulated" if svc.is_demo else
                                                   f"Alpaca {svc.market.feed.label}") + ")",
                       "is_demo": svc.is_demo, "modes": ["day", "swing", "both"],
                       "symbols": list(svc.market.symbols)},
            "bundled": {"label": "Bundled real daily history (AAPL Feb 2015-Feb 2017, "
                                 "GOOG Aug 2004-Mar 2013)",
                        "is_demo": False, "modes": ["swing"], "symbols": list(BUNDLED)},
        }

    def validate(self, req: dict) -> dict:
        mode = req.get("mode")
        if mode not in ("day", "swing", "both"):
            raise ValueError("mode must be day, swing or both")
        src = req.get("data_source", "market")
        if src not in ("market", "bundled"):
            raise ValueError("data_source must be market or bundled")
        if src == "bundled" and mode != "swing":
            raise ValueError("bundled history is daily only: use swing mode")
        limit = MAX_DAYS["day"] if mode == "both" else MAX_DAYS[mode]
        try:
            start, end = date.fromisoformat(req["start"]), date.fromisoformat(req["end"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("start and end must be YYYY-MM-DD") from None
        if start >= end:
            raise ValueError("start must be before end")
        if (end - start).days > limit:
            raise ValueError(f"{mode} backtests are limited to {limit} calendar days")
        if end > date.today():
            raise ValueError("end cannot be in the future")
        universe = req.get("universe") or None
        if universe:
            for m in universe:
                sym = str(m.get("symbol", "")).upper()
                if not SYMBOL_RE.match(sym):
                    raise ValueError(f"invalid symbol in universe: {m.get('symbol')!r}")
                for k in ("start", "end"):
                    if m.get(k):
                        date.fromisoformat(m[k])
            symbols = sorted({m["symbol"].upper() for m in universe})
        else:
            symbols = [s.upper() for s in (req.get("symbols") or
                                           self.sources()[src]["symbols"])]
        if not symbols or len(symbols) > MAX_SYMBOLS:
            raise ValueError(f"choose 1-{MAX_SYMBOLS} symbols")
        bad = [s for s in symbols if not SYMBOL_RE.match(s)]
        if bad:
            raise ValueError(f"invalid symbols: {bad}")
        if src == "bundled" and set(symbols) - set(BUNDLED):
            raise ValueError(f"bundled history exists only for {', '.join(BUNDLED)}")
        BacktestConfig().with_overrides(req.get("settings"))  # raises on bad settings
        return {"mode": mode, "data_source": src, "start": start.isoformat(),
                "end": end.isoformat(), "symbols": symbols, "universe": universe,
                "settings": req.get("settings") or {}}

    def start(self, req: dict, background: bool = True) -> str:
        req = self.validate(req)
        with self._lock:
            if self.current is not None:
                raise BacktestBusy("A backtest is already running; wait for it to finish.")
            run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
            self.current = {"id": run_id, "progress": 0.0, "message": "starting",
                            "started": time.time()}
        self.db.create_backtest(run_id, req)
        if background:
            threading.Thread(target=self._run, args=(run_id, req), daemon=True,
                             name=f"backtest-{run_id}").start()
        else:
            self._run(run_id, req)
        return run_id

    def _run(self, run_id: str, req: dict) -> None:
        svc = self.service
        try:
            provider = BundledHistory() if req["data_source"] == "bundled" \
                else MarketHistory(svc.market)

            def progress(frac: float, msg: str) -> None:
                if self.current:
                    self.current.update(progress=round(frac, 3), message=msg)

            run = run_backtest(
                provider, mode=req["mode"], symbols=req["symbols"],
                start=date.fromisoformat(req["start"]), end=date.fromisoformat(req["end"]),
                cal=svc.market.calendar,
                engine_cfg={"strategy": svc.strategy_config, "swing": svc.swing_config,
                            "planning": svc.planning_config,
                            "volume": None if req["data_source"] == "bundled" else
                            VolumeSource(svc.market.volume_scope,
                                         "DEMO" if svc.is_demo else svc.market.feed.label,
                                         svc.planning_config.partial_volume_policy)},
                settings=req["settings"], universe=req["universe"], progress=progress)
            rep = report(run)
            summary = {"trades": rep["overall"].get("trades", 0),
                       "win_rate": rep["overall"].get("win_rate"),
                       "expectancy_r": rep["overall"].get("expectancy_r"),
                       "profit_factor": rep["overall"].get("profit_factor"),
                       "verdict": rep["verdict"]["label"], "is_demo": run["is_demo"],
                       "duration_s": round(time.time() - self.current["started"], 1)}
            self.db.finish_backtest(run_id, "done", summary, run)
        except Exception as exc:  # stored and shown; the server keeps running
            log.exception("Backtest %s failed", run_id)
            self.db.finish_backtest(run_id, "failed", error=str(exc))
        finally:
            with self._lock:
                self.current = None

    def status(self, run_id: str) -> dict | None:
        row = self.db.backtest(run_id)
        if row is None:
            return None
        if self.current and self.current["id"] == run_id:
            row["live"] = {k: self.current[k] for k in ("progress", "message")}
        return row

    def report(self, run_id: str, **filters) -> dict | None:
        row = self.db.backtest(run_id)
        if row is None:
            return None
        out = {k: row[k] for k in ("id", "created_at", "finished_at", "status", "request",
                                   "summary", "error")}
        if self.current and self.current["id"] == run_id:
            out["live"] = {k: self.current[k] for k in ("progress", "message")}
        run = row.get("result")
        if run:
            meta = {k: v for k, v in run.items() if k not in ("trades", "skipped")}
            out["run"] = meta
            out["report"] = report(run, **filters)
            out["skipped"] = run["skipped"][:500]
        return out

    def trade_chart(self, run_id: str, trade_id: str) -> dict | None:
        """Candles around one simulated trade (re-fetched from the run's data source), for
        inspecting entry, stop, targets and exit. Simulated - never a real fill."""
        row = self.db.backtest(run_id)
        run = row.get("result") if row else None
        if not run:
            return None
        trade = next((t for t in run["trades"] if t["id"] == trade_id), None)
        if trade is None:
            return None
        md = trade.get("mode", run["mode"])
        tf = EXEC_TF[md]
        provider = BundledHistory() if run["data_source"] == "bundled" \
            else MarketHistory(self.service.market)
        cal = self.service.market.calendar
        if md == "day":
            d0 = datetime.fromtimestamp(trade["entry_time"], NY).date()
            sd = cal.day(d0)
            lo, hi = sd.open, sd.close
        else:
            lo = datetime.fromtimestamp(trade["entry_time"] - 90 * 86400, NY)
            hi = datetime.fromtimestamp(trade["exit_time"] + 45 * 86400, NY)
        bars, rep = validate_series(trade["ticker"], tf, provider.bars(trade["ticker"], tf, lo, hi),
                                    cal)
        return {"trade": trade, "timeframe": tf, "data_source": run["data_source"],
                "is_demo": run["is_demo"], "simulated": True,
                "bars": [{"time": b.time, "open": b.open, "high": b.high, "low": b.low,
                          "close": b.close, "volume": b.volume} for b in bars],
                "integrity": rep.to_dict()}

    def defaults(self) -> dict:
        return {"settings": BacktestConfig().to_dict(), "limitations": LIMITATIONS,
                "max_days": MAX_DAYS, "sources": self.sources(), "order_execution": False}
