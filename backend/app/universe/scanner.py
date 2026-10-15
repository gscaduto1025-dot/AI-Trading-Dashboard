"""Universe scanner: the existing day and swing engines run over a liquidity-ranked universe.

Detection is NOT reimplemented here. A second engine instance (app.main.Service with its
own MarketDataService over the universe symbols) runs the same frozen strategy, swing and
planning code as the watchlist scanner. It shares the one Alpaca client, so both scanners
share the same rate limiter. Every row is built with the watchlist scanner's own
setup_row()/annotate_row(), so a universe row and a watchlist row for the same setup are
identical.

Reliability:
- one run at a time: an in-process lock plus a database lease (heartbeat) so two
  processes never scan concurrently; a run whose heartbeat stops is marked interrupted;
- progress is persisted after every batch (total, scanned, failed, remaining);
- the latest result per (symbol, mode) is persisted, so results survive restarts and a
  restart only loses the batch in flight;
- symbols whose data cannot be evaluated are reported as DATA_UNAVAILABLE, never dropped;
- setups are keyed by the engines' deterministic setup id: a setup seen in several scans
  is one row (first_seen / last_seen);
- no Claude calls, no alerts, no signal-log writes, no orders.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime

from ..data.base import DEFAULT_TIMEFRAME, DataError
from ..scanner.engine import (FRESH_OK, _data_state, _proximity, annotate_row, entry_env,
                              filter_rows, include_row, live_quote, rank_key, setup_row)
from . import UniverseConfig, build_universe

log = logging.getLogger("tradescope.universe")


def _env_int(k: str, d: int) -> int:
    return int(os.environ.get(k, str(d)))


class UniverseScanner:
    BATCH = 8  # symbols per engine call (progress is persisted after each)

    def __init__(self, service) -> None:
        self.svc = service
        self.db = service.db
        self.cfg = UniverseConfig.from_env()
        self.interval = _env_int("UNIVERSE_SCAN_INTERVAL_SECONDS", 900)
        self.rebuild_seconds = _env_int("UNIVERSE_REBUILD_HOURS", 24) * 3600
        self.warm_workers = _env_int("UNIVERSE_WARMUP_WORKERS", 4)
        self.retry_pause = _env_int("UNIVERSE_RETRY_PAUSE_SECONDS", 30)
        self._lock = threading.Lock()
        self._engine = None
        self.last_error: str | None = None
        self.current: dict | None = None  # live progress of the run in this process

    # ------------------------------------------------------------------- universe

    def universe(self, rebuild: bool = False) -> dict:
        """The stored universe, rebuilt when missing, older than UNIVERSE_REBUILD_HOURS,
        built with a different configuration, or on request."""
        u = self.db.latest_universe()
        stale = u is None or u.get("config") != self.cfg.to_dict() or (
            time.time() - datetime.fromisoformat(u["built_at"]).timestamp()
            > self.rebuild_seconds)
        if rebuild or stale:
            built = build_universe(self.svc.market, self.cfg, list(self.svc.market.symbols))
            payload = built.to_dict()
            payload["build_id"] = self.db.save_universe(payload)
            return payload
        return u

    def engine(self, symbols: list[str]):
        """The second engine instance (created once; its symbol list follows the
        universe). Its candle caches persist between scans."""
        if self._engine is None:
            from ..db import Database
            from ..main import Service
            from ..market.service import MarketDataService
            m = self.svc.market
            market = MarketDataService(m.source, symbols=symbols, now_fn=m._now_fn,
                                       calendar=m._fixed_calendar, monotonic=m._mono)
            self._engine = Service(self.svc.settings, market=market, db=Database(":memory:"),
                                   with_universe=False)
            from .. import rawcache
            if rawcache.enabled() and not market.source.is_demo:
                log.info("Universe raw cache restored: %s", rawcache.restore(
                    market, rawcache.cache_dir() / "universe.json.gz"))
        self._engine.market.set_symbols(symbols)
        return self._engine

    # ------------------------------------------------------------------ schedule

    def due(self, now: float | None = None) -> bool:
        if self.interval <= 0:
            return False
        now = now or time.time()
        runs = self.db.universe_runs(5)
        last = next((r for r in runs if r["status"] in ("completed", "running")), None)
        if last is None:
            return True
        if last["status"] == "running" and now - last["heartbeat"] < 180:
            return False
        if now - last["started_at"] < self.interval:
            return False
        # Outside the regular session (after-hours, overnight, pre-market) no regular-
        # session candle changes: one complete scan after the last close is enough.
        # ("after-hours" used to fall through, rescanning every interval until 20:00.)
        market = self.svc.market
        phase = market.market_status().get("phase")
        if phase != "regular":
            close = market.calendar.last_completed_session(market.cutoff()).close
            done = next((r for r in runs if r["status"] == "completed"), None)
            if done and done["started_at"] > close.timestamp() + 16 * 60:
                return False
        return True

    # ----------------------------------------------------------------------- run

    def run(self, trigger: str = "scheduled", rebuild: bool = False) -> dict:
        if not self._lock.acquire(blocking=False):
            return {"started": False, "reason": "a universe scan is already running"}
        try:
            return self._run(trigger, rebuild)
        finally:
            self._lock.release()

    def _run(self, trigger: str, rebuild: bool) -> dict:
        try:
            u = self.universe(rebuild=rebuild)
        except DataError as exc:
            self.last_error = f"universe build failed: {exc}"
            return {"started": False, "reason": self.last_error}
        symbols = [m["symbol"] for m in u["members"]]
        run_id = self.db.claim_universe_run(trigger, len(symbols), u.get("build_id"))
        if run_id is None:
            return {"started": False, "reason": "another process is running a universe scan"}
        client = getattr(self.svc.market.source, "client", None)
        req0 = getattr(client, "requests_made", 0)
        self.current = {"run_id": run_id, "total": len(symbols), "scanned": 0, "failed": 0,
                        "started_at": int(time.time())}
        scanned = failed = setups = 0
        retry: list[str] = []
        try:
            eng = self.engine(symbols)
            try:
                quotes = {q["symbol"]: q for q in eng.market.get_quotes()}
            except DataError:
                quotes = {}
            env = entry_env(eng)
            for i in range(0, len(symbols), self.BATCH):
                batch = symbols[i:i + self.BATCH]
                self._warm(eng, batch)
                results = self._evaluate(eng, batch)
                for sym in batch:
                    bad = self._store(eng, sym, results, quotes.get(sym) or {}, env, run_id)
                    setups += bad[1]
                    if bad[0]:
                        retry.append(sym)
                    scanned += 1
                    failed += bool(bad[0])
                self.current.update(scanned=scanned, failed=failed)
                self.db.update_universe_run(run_id, scanned=scanned, failed=failed,
                                            setups=setups)
            # One retry for symbols that hit a data error (e.g. a burst of HTTP 429s or a
            # network blip), after a pause; their results are replaced if it succeeds.
            if retry and self.retry_pause >= 0:
                time.sleep(self.retry_pause)
                still = 0
                for i in range(0, len(retry), self.BATCH):
                    batch = retry[i:i + self.BATCH]
                    results = self._evaluate(eng, batch)
                    for sym in batch:
                        bad, n = self._store(eng, sym, results, quotes.get(sym) or {}, env,
                                             run_id)
                        still += bool(bad)
                        setups += n
                failed = still
                self.db.update_universe_run(run_id, failed=failed, setups=setups)
            self.db.update_universe_run(
                run_id, status="completed", finished_at=int(time.time()),
                requests=getattr(client, "requests_made", 0) - req0)
            self.last_error = None
            return {"started": True, "run_id": run_id, "scanned": scanned, "failed": failed}
        except Exception as exc:  # noqa: BLE001
            log.exception("universe scan failed")
            self.last_error = str(exc)
            self.db.update_universe_run(run_id, status="failed", error=str(exc)[:500],
                                        finished_at=int(time.time()))
            return {"started": True, "run_id": run_id, "error": str(exc)}
        finally:
            self.current = None

    def _evaluate(self, eng, batch: list[str]) -> dict:
        results = {}
        for mode, call in (("day", eng.strategy_setups), ("swing", eng.swing_setups)):
            try:
                results[mode] = call(batch, record=False)
            except Exception as exc:  # noqa: BLE001 - recorded per symbol
                log.exception("universe %s batch failed", mode)
                results[mode] = {"setups": [], "symbols": {
                    s: {"status": "unavailable", "error": str(exc)} for s in batch}}
        return results

    def _store(self, eng, sym, results, q, env, run_id) -> tuple[bool, int]:
        """Persist both modes for one symbol; returns (had a data problem, setups)."""
        bad, n = False, 0
        for mode in ("day", "swing"):
            status, payload = self._symbol(eng, sym, mode, results[mode], q, env)
            bad |= status != "ok"
            n += len(payload["rows"])
            self.db.put_universe_result(sym, mode, run_id, status, payload)
        return bad, n

    def _warm(self, eng, batch: list[str]) -> None:
        from ..rawcache import warm_up
        frames = {**eng.STRATEGY_FRAMES, **eng.SWING_FRAMES}
        warm_up(eng.market, batch, frames, self.warm_workers)

    def _symbol(self, eng, sym: str, mode: str, res: dict, q: dict, env: dict):
        """Rows and context for one symbol and mode (mirrors OpportunityScanner._scan)."""
        state = (res.get("symbols") or {}).get(sym) or {}
        data = _data_state(eng, sym, mode)
        info = {"ticker": sym, "price": q.get("price"), "change_pct": q.get("change_pct"),
                "price_time": q.get("price_time"), "levels": {}, "bias": {},
                "liquidity": state.get("liquidity"), "data": data}
        if state.get("status") != "ok":
            return "DATA_UNAVAILABLE", {"rows": [], "info": info,
                                        "error": state.get("error") or "not evaluated"}
        error = None
        try:
            tf = DEFAULT_TIMEFRAME[mode]
            a = eng.analysis(sym, mode, tf)
            info["bias"][mode] = a["bias"]["label"].lower()
            info["levels"][mode] = _proximity(a)
            ind = a.get("indicators") or {}
            info["key_levels"] = a.get("key_levels") or []
            info["indicators"] = {k: ind.get(k) for k in (
                "vwap", "ema_fast", "ema_fast_period", "ema_slow", "ema_slow_period",
                "sma_long", "atr", "rsi", "volume_ratio")}
            info["analysis_timeframe"] = tf
            if info["price"] is None:
                info["price"] = a["price"]
        except Exception as exc:  # noqa: BLE001 - levels are context; setups still listed
            error = getattr(exc, "message", str(exc))
        rows = []
        for s in res.get("setups") or []:
            if s["ticker"] != sym or not include_row(s):
                continue
            rows.append(annotate_row(setup_row(s, mode, info["price"], info["bias"].get(mode),
                                               data, live_quote(q), env), info, mode))
        status = "ok" if data.get("freshness") in FRESH_OK or rows else "DATA_UNAVAILABLE"
        return status, {"rows": rows, "info": info, "error": error if status == "ok"
                        else (data.get("reason") or error or "data not usable")}

    # -------------------------------------------------------------------- views

    def status(self) -> dict:
        u = self.db.latest_universe()
        runs = self.db.universe_runs(10)
        now = time.time()
        for r in runs:
            r["stale"] = r["status"] == "running" and now - r["heartbeat"] >= 180
            r["remaining"] = max(0, r["total"] - r["scanned"])
        last_done = next((r for r in runs if r["status"] == "completed"), None)
        running = next((r for r in runs if r["status"] == "running" and not r["stale"]), None)
        total = len(u["members"]) if u else 0
        return {
            "universe": ({k: u[k] for k in ("built_at", "source", "config", "stats")}
                         | {"size": total} if u else None),
            "running": running, "last_completed": last_done, "recent_runs": runs,
            "schedule": {"interval_seconds": self.interval,
                         "rebuild_hours": self.rebuild_seconds / 3600,
                         "enabled": self.interval > 0},
            "last_error": self.last_error,
            "coverage_note": (
                "No universe built yet." if not total else
                f"DEMO: the universe is the {total}-symbol demo watchlist (simulated data)."
                if u.get("source") == "DEMO" else
                f"Scans the configured universe of {total} liquid US stocks (top "
                f"{u['config']['size']} by 20-session consolidated dollar volume, price >= "
                f"${u['config']['min_price']:g}, average >= "
                f"${u['config']['min_adv_usd'] / 1e6:g}M/day"
                f"{', ETFs excluded' if not u['config']['include_etfs'] else ''}). It is not "
                "the full US market."),
        }

    def setups(self, **filters) -> dict:
        u = self.db.latest_universe()
        members = [m["symbol"] for m in u["members"]] if u else []
        results = self.db.universe_results(members)
        rows, unavailable, tickers = [], [], {}
        for r in results:
            info = r.get("info") or {}
            t = tickers.setdefault(r["symbol"], {"ticker": r["symbol"], "price": info.get("price"),
                                                 "liquidity": info.get("liquidity"),
                                                 "modes": {}})
            t["modes"][r["mode"]] = {"status": r["status"], "scanned_at": r["scanned_at"],
                                     "bias": (info.get("bias") or {}).get(r["mode"]),
                                     "levels": (info.get("levels") or {}).get(r["mode"]),
                                     "key_levels": info.get("key_levels"),
                                     "indicators": info.get("indicators"),
                                     "error": r.get("error")}
            if r["status"] == "DATA_UNAVAILABLE":
                unavailable.append({"ticker": r["symbol"], "mode": r["mode"],
                                    "status": "DATA_UNAVAILABLE", "error": r.get("error"),
                                    "scanned_at": r["scanned_at"]})
            for row in r.get("rows") or []:
                row["scanned_at"] = r["scanned_at"]
                rows.append(row)
        rows.sort(key=rank_key)
        out = filter_rows(rows, **filters)
        scanned = {r["symbol"] for r in results}
        return {"rows": out, "total_rows": len(rows), "unavailable": unavailable,
                "tickers": sorted(tickers.values(), key=lambda t: t["ticker"]),
                "universe_size": len(members), "symbols_with_results": len(scanned),
                "strategies": sorted({r["strategy"] for r in rows}),
                "is_demo": self.svc.is_demo}

    def symbol_detail(self, symbol: str) -> dict | None:
        res = [r for r in self.db.universe_results([symbol.upper()])]
        return {"symbol": symbol.upper(), "results": res} if res else None

    def explain(self, symbol: str, mode: str, setup_id: str | None) -> dict:
        """Rules-based explanation of a universe setup, from the universe engine."""
        from ..explain import explain_setup
        u = self.db.latest_universe()
        members = [m["symbol"] for m in u["members"]] if u else []
        if symbol.upper() not in members:
            raise KeyError(f"{symbol} is not in the current universe")
        eng = self.engine(members)
        return explain_setup(eng, symbol.upper(), mode, setup_id,
                             scanner_row=self._row(setup_id))

    def _row(self, setup_id: str | None) -> dict | None:
        if not setup_id:
            return None
        for r in self.db.universe_results():
            for row in r.get("rows") or []:
                if row["id"] == setup_id:
                    return row
        return None


def next_wait(scanner: UniverseScanner) -> float:
    """Seconds the scheduler sleeps between due() checks."""
    return max(30.0, min(300.0, scanner.interval / 4)) if scanner.interval > 0 else 300.0

