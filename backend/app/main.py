"""TradeScope AI backend: FastAPI app exposing market data, analysis, setups, alerts and AI."""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import logging
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from .ai.claude import AIUnavailable, ClaudeAnalyst
from .analysis.engine import analyze
from .analysis.levels import Level as EngineLevel
from .config import Settings, load_settings
from .data.base import (DEFAULT_TIMEFRAME, MODE_TIMEFRAMES, TIMEFRAMES, DataError,
                        InvalidRequest)
from .data.demo import DemoSource
from .db import Database
from .market.alpaca_client import AlpacaClient, RateLimiter
from .market.calendar import NY
from .market.service import CandleSet, MarketDataService
from .market.sources import AlpacaSource
from .levels.engine import InsufficientData, compute_key_levels
from .levels.model import FETCH_LIMIT, MODE_FRAMES
from .market.timeframes import SPECS, lookback_start, spec
from .strategies.common import TRIGGERED, signal_id
from .strategies.config import DAY_TRADING_UNIVERSE, StrategyConfig, load_strategy_config
from .strategies.engine import STRATEGIES, evaluate_symbol
from .planning.config import PlanningConfig, load_planning_config
from .swing.config import SwingConfig, load_swing_config
from .swing.context import fmt_day
from .swing.engine import SORT_KEYS, SWING_STRATEGIES, evaluate_swing_symbol, sort_setups

log = logging.getLogger("tradescope")

BAR_LIMIT = 300
DISCLAIMER = ("Educational analysis only — not financial advice. "
              "TradeScope AI does not place trades.")


def build_market(settings: Settings) -> MarketDataService:
    """Alpaca when credentials are configured, otherwise clearly-labelled demo data."""
    if settings.alpaca_enabled:
        client = AlpacaClient(
            settings.alpaca_key_id, settings.alpaca_secret_key,
            data_url=settings.alpaca_data_url, trading_url=settings.alpaca_trading_url,
            rate_limiter=RateLimiter(settings.alpaca_rate_limit_per_min),
            max_retries=settings.alpaca_max_retries, timeout=settings.alpaca_timeout_seconds)
        source = AlpacaSource(client, settings.alpaca_feed, settings.alpaca_adjustment)
    else:
        source = DemoSource()
    return MarketDataService(source, symbols=settings.watchlist)


class Service:
    """Holds the market data service, database and AI analyst shared by the routes."""

    def __init__(self, settings: Settings, market: MarketDataService | None = None,
                 db: Database | None = None, analyst: ClaudeAnalyst | None = None) -> None:
        self.settings = settings
        self.market = market or build_market(settings)
        self.db = db or Database(settings.database_path)
        self.analyst = analyst or ClaudeAnalyst(settings.claude_model, settings.claude_effort)
        self.ai_enabled = settings.anthropic_api_key_present or analyst is not None
        self.last_scan: int | None = None
        self.strategy_config: StrategyConfig = load_strategy_config()
        self._strategy_cache: dict[tuple, list[dict]] = {}
        self.swing_config: SwingConfig = load_swing_config()
        self.planning_config: PlanningConfig = load_planning_config()
        self._swing_cache: dict[tuple, list[dict]] = {}

    @property
    def is_demo(self) -> bool:
        return self.market.source.is_demo

    def candles_for_analysis(self, symbol: str, timeframe: str) -> CandleSet:
        """Regular-session candles that are safe to analyse, or raise AnalysisBlocked."""
        candles = self.market.get_candles(symbol, timeframe, "regular", BAR_LIMIT)
        block = candles.analysis_block()
        if block:
            raise AnalysisBlocked(*block, candles.status)
        return candles

    def key_levels(self, symbol: str, mode: str, chart_tf: str | None = None,
                   as_of: datetime | None = None) -> dict:
        """Multi-timeframe key levels, point-in-time at `as_of` (default: the feed's cutoff).

        Live: uses the cached candle sets and refuses stale/invalid/unavailable frames.
        Replay (`as_of` given): fetches history ending at `as_of`; the engine then drops any
        candle that had not closed by then, so the output contains no look-ahead.
        """
        if mode not in MODE_FRAMES:
            raise InvalidRequest(f"mode must be one of {list(MODE_FRAMES)}")
        market = self.market
        symbol = market.check_symbol(symbol)
        frames: dict = {}
        chart_bars = None
        statuses = {}
        if as_of is None:
            for tf in MODE_FRAMES[mode]:
                candles = market.get_candles(symbol, tf, "regular", FETCH_LIMIT[tf])
                block = candles.analysis_block()
                if block and block[0] != "INSUFFICIENT_DATA":
                    raise AnalysisBlocked(block[0], f"{SPECS[tf].label} data: {block[1]}",
                                          candles.status)
                frames[tf] = candles.completed_bars
                statuses[tf] = candles.status
            if chart_tf:
                chart_bars = market.get_candles(symbol, chart_tf, "regular", 300).completed_bars
            when = market.cutoff()
        else:
            when = min(as_of, market.cutoff())
            cal = market.calendar
            for tf in set(MODE_FRAMES[mode]) | ({chart_tf} if chart_tf else set()):
                start = lookback_start(spec(tf), cal, when, FETCH_LIMIT.get(tf, 300), "regular")
                frames[tf] = market.get_history(symbol, tf, start, when).bars
            chart_bars = frames.get(chart_tf) if chart_tf else None
            frames = {tf: frames[tf] for tf in MODE_FRAMES[mode]}
        report = compute_key_levels(symbol, mode, frames, when, market.calendar, chart_tf,
                                    chart_bars, adjustment=market.feed_info().get("adjustment"))
        report["is_demo"] = self.is_demo
        report["feed"] = market.feed_info()
        report["replay"] = as_of is not None
        return report

    @staticmethod
    def engine_levels(report: dict) -> list[EngineLevel]:
        """Valid horizontal key levels as setup-engine levels (zones by their near edge)."""
        price = report["last_price"]
        out = []
        for lv in report["levels"]:
            if not lv["valid"] or lv["category"] not in ("reference", "zone", "swing"):
                continue
            if lv["is_zone"]:
                if lv["low"] <= price <= lv["high"]:
                    continue
                value = lv["low"] if lv["low"] > price else lv["high"]
            else:
                value = lv["price"]
            out.append(EngineLevel(value, "", f"{lv['label']}", touches=1,
                                   strength=lv["strength"]["score"] / 100))
        return out

    def analysis(self, symbol: str, mode: str, timeframe: str) -> dict:
        candles = self.candles_for_analysis(symbol, timeframe)
        # Strategies confirm on candle closes, so only completed candles are analysed.
        bars = candles.completed_bars
        extra, summary, levels_error = [], [], None
        try:
            report = self.key_levels(symbol, mode)
            extra = self.engine_levels(report)
            ranked = sorted((l for l in report["levels"] if l["valid"]),
                            key=lambda l: -l["strength"]["score"])[:12]
            summary = [{k: l[k] for k in ("label", "type", "timeframe", "low", "high", "side")}
                       | {"strength": l["strength"]["score"], "evidence": l["evidence"]["summary"]}
                       for l in ranked]
        except (AnalysisBlocked, DataError, InsufficientData) as exc:
            levels_error = getattr(exc, "message", str(exc))
        result = analyze(candles.symbol, bars, mode, timeframe, extra_levels=extra)
        result["is_demo"] = self.is_demo
        result["data_status"] = candles.status
        result["analyzed_through"] = bars[-1].time
        result["excluded_forming_candle"] = len(bars) < len(candles.bars)
        result["key_levels"] = summary
        result["key_levels_error"] = levels_error
        return result

    # ------------------------------------------------------- day-trading strategies

    STRATEGY_FRAMES = {"5Min": 600, "15Min": 400, "1Hour": 500, "1Day": 500, "1Week": 260}

    @property
    def day_universe(self) -> list[str]:
        return [s for s in DAY_TRADING_UNIVERSE if s in self.market.symbols]

    def strategy_setups(self, symbols: list[str] | None = None,
                        as_of: datetime | None = None, record: bool = True) -> dict:
        """Evaluate every strategy for the day-trading universe at `as_of` (default: the
        feed's data cutoff). Results for a symbol are cached until a new 5- or 15-minute
        candle closes, so repeated polling is cheap and identical (deterministic)."""
        market = self.market
        symbols = [market.check_symbol(s) for s in (symbols or self.day_universe)]
        when = market.cutoff() if as_of is None else min(as_of, market.cutoff())
        setups: list[dict] = []
        per_symbol: dict[str, dict] = {}
        new_alerts: list[dict] = []
        for sym in symbols:
            try:
                frames, problems = self._strategy_frames(sym, when, as_of is not None)
            except DataError as exc:
                per_symbol[sym] = {"status": "unavailable", "error": str(exc), "code": exc.code}
                continue
            key = (sym, when.timestamp() if as_of is not None else None,
                   *(frames[tf][-1].time if frames.get(tf) else 0 for tf in ("5Min", "15Min")),
                   when < market.calendar.last_session_started(when, "regular").close,
                   tuple(problems))
            cached = self._strategy_cache.get(key)
            if cached is None:
                ctx, result = evaluate_symbol(sym, frames, when, market.calendar,
                                              self.strategy_config, problems,
                                              planning=self.planning_config)
                cached = [s.to_dict() for s in result]
                self._strategy_cache = {k: v for k, v in self._strategy_cache.items()
                                        if k[0] != sym}  # keep only the newest per symbol
                self._strategy_cache[key] = cached
            for s in cached:
                s["is_demo"] = self.is_demo
            setups += cached
            per_symbol[sym] = {"status": "ok", "gate_reasons": next(
                (s["reasons"] for s in cached if s["status"] == "NO_TRADE" and problems), []),
                "triggered": sum(s["status"] == TRIGGERED for s in cached),
                "watching": sum(s["status"] == "WATCHING" for s in cached)}
            if record and as_of is None:
                new_alerts += self._record_signals(cached)
        order = {"TRIGGERED": 0, "WATCHING": 1, "NO_TRADE": 2}
        setups.sort(key=lambda s: (order[s["status"]], -(s["confirmed_at"] or 0), s["ticker"],
                                   s["strategy"], s["timeframe"], s["direction"]))
        return {"evaluated_at": when.astimezone(NY).isoformat(), "replay": as_of is not None,
                "universe": symbols, "timeframes": ["5Min", "15Min"],
                "context_timeframes": ["1Hour", "1Day"], "symbols": per_symbol,
                "is_demo": self.is_demo, "setups": setups, "new_alerts": new_alerts,
                "strategies": [{"id": st.id, "name": st.name} for st in STRATEGIES]}

    def _strategy_frames(self, sym: str, when: datetime, replay: bool):
        market = self.market
        frames, problems = {}, []
        for tf, limit in self.STRATEGY_FRAMES.items():
            if replay:
                start = lookback_start(spec(tf), market.calendar, when, limit, "regular")
                frames[tf] = market.get_history(sym, tf, start, when).bars
                continue
            candles = market.get_candles(sym, tf, "regular", limit)
            block = candles.analysis_block()
            if block and block[0] != "INSUFFICIENT_DATA":
                problems.append(f"{SPECS[tf].label} data: {block[1]}")
            frames[tf] = candles.bars
        return frames, problems

    def _record_signals(self, setups: list[dict]) -> list[dict]:
        """Append every accepted trigger to the immutable signal log (alerts on new ones)."""
        names = {st.id: st.name for st in STRATEGIES}
        alerts = []
        for s in setups:
            for t in s.get("triggers_today", []):
                if not t["accepted"]:
                    continue
                sid = signal_id(s["ticker"], s["strategy"], s["timeframe"], s["direction"],
                                s["session_date"], t["time"])
                payload = s if s["trigger_time"] == t["time"] else {**s, "note": "earlier trigger"}
                alert = self.db.record_signal(sid, payload, t, self.is_demo, names[s["strategy"]])
                if alert:
                    alerts.append(alert)
        return alerts

    # ------------------------------------------------------- swing-trading strategies

    SWING_FRAMES = {"1Day": 500, "1Week": 260, "4Hour": 200, "1Hour": 300}
    SWING_REQUIRED = ("1Day", "1Week")

    def swing_setups(self, symbols: list[str] | None = None, as_of: datetime | None = None,
                     sort: str = "quality", record: bool = True) -> dict:
        """Evaluate every swing strategy (daily + weekly, 4H/1H supporting) for the watchlist
        at `as_of` (default: the feed's cutoff). Cached per symbol until a new daily, weekly
        or 4-hour candle closes; the output is deterministic for the same candles."""
        if sort not in SORT_KEYS:
            raise InvalidRequest(f"sort must be one of {', '.join(SORT_KEYS)}")
        market = self.market
        symbols = [market.check_symbol(s) for s in (symbols or market.symbols)]
        when = market.cutoff() if as_of is None else min(as_of, market.cutoff())
        setups: list[dict] = []
        per_symbol: dict[str, dict] = {}
        new_alerts: list[dict] = []
        for sym in symbols:
            try:
                frames, problems = self._swing_frames(sym, when, as_of is not None)
            except DataError as exc:
                per_symbol[sym] = {"status": "unavailable", "error": str(exc), "code": exc.code}
                continue
            key = (sym, when.timestamp() if as_of is not None else None,
                   *(frames[tf][-1].time if frames.get(tf) else 0 for tf in self.SWING_FRAMES),
                   tuple(problems))
            cached = self._swing_cache.get(key)
            if cached is None:
                _, cached = evaluate_swing_symbol(sym, frames, when, market.calendar,
                                                  self.swing_config, problems,
                                                  is_demo=self.is_demo,
                                                  planning=self.planning_config)
                self._swing_cache = {k: v for k, v in self._swing_cache.items() if k[0] != sym}
                self._swing_cache[key] = cached
            setups += cached
            per_symbol[sym] = {"status": "ok",
                               "gate_reasons": next((s["reasons"] for s in cached
                                                     if s["status"] == "NO_TRADE" and problems), []),
                               "triggered": sum(s["status"] == TRIGGERED for s in cached),
                               "watching": sum(s["status"] == "WATCHING" for s in cached)}
            if record and as_of is None:
                new_alerts += self._record_swing_signals(cached)
        return {"evaluated_at": when.astimezone(NY).isoformat(), "replay": as_of is not None,
                "mode": "swing", "universe": symbols, "timeframes": list(self.SWING_REQUIRED),
                "supporting_timeframes": ["4Hour", "1Hour"], "symbols": per_symbol,
                "sort": sort, "sort_keys": list(SORT_KEYS), "is_demo": self.is_demo,
                "setups": sort_setups(setups, sort), "new_alerts": new_alerts,
                "strategies": [{"id": st.id, "name": st.name} for st in SWING_STRATEGIES]}

    def _swing_frames(self, sym: str, when: datetime, replay: bool):
        market = self.market
        frames, problems = {}, []
        for tf, limit in self.SWING_FRAMES.items():
            if replay:
                start = lookback_start(spec(tf), market.calendar, when, limit, "regular")
                try:
                    frames[tf] = market.get_history(sym, tf, start, when).bars
                except DataError:
                    if tf in self.SWING_REQUIRED:
                        raise
                    frames[tf] = []
                continue
            try:
                candles = market.get_candles(sym, tf, "regular", limit)
            except DataError:
                if tf in self.SWING_REQUIRED:
                    raise
                frames[tf] = []  # supporting timeframes are optional context
                continue
            block = candles.analysis_block()
            if block and block[0] != "INSUFFICIENT_DATA":
                if tf in self.SWING_REQUIRED:
                    problems.append(f"{SPECS[tf].label} data: {block[1]}")
                else:
                    frames[tf] = []
                    continue
            frames[tf] = candles.bars
        return frames, problems

    def _record_swing_signals(self, setups: list[dict]) -> list[dict]:
        """Log each fresh, accepted swing trigger once (immutable) and alert on new ones."""
        names = {st.id: st.name for st in SWING_STRATEGIES}
        alerts = []
        for s in setups:
            if s["status"] != TRIGGERED:
                continue
            t = next(t for t in s["triggers"] if t["time"] == s["trigger_time"])
            alert = self.db.record_signal(s["id"], {**s, "session_date": fmt_day(t["time"])}, t,
                                          self.is_demo, names[s["strategy"]], mode="swing")
            if alert:
                alerts.append(alert)
        return alerts

    def plans_for(self, symbol: str, mode: str, as_of: datetime | None = None) -> dict:
        """Every trade plan for one symbol in one mode, active plans first by quality.
        Day mode covers the day-trading universe only; other symbols get an explanation."""
        symbol = self.market.check_symbol(symbol)
        if mode == "swing":
            res = self.swing_setups([symbol], as_of, record=False)
        elif mode == "day":
            if symbol not in self.day_universe:
                return {"symbol": symbol, "mode": mode, "plans": [], "status": "NO_TRADE",
                        "reason": f"{symbol} is not in the day-trading universe "
                                  f"({', '.join(self.day_universe)}): no day setups are scanned.",
                        "is_demo": self.is_demo}
            res = self.strategy_setups([symbol], as_of, record=False)
        else:
            raise InvalidRequest("mode must be day or swing")
        sym_state = res["symbols"].get(symbol, {})
        if sym_state.get("status") == "unavailable":
            return {"symbol": symbol, "mode": mode, "plans": [], "status": "NO_TRADE",
                    "reason": f"MARKET DATA UNAVAILABLE: {sym_state.get('error')}",
                    "is_demo": self.is_demo}
        order = {"TRIGGERED": 0, "WATCHING": 1, "NO_TRADE": 2}
        rows = [r for r in res["setups"] if r["ticker"] == symbol and r.get("plan")]
        rows.sort(key=lambda r: (order[r["status"]], r["plan"]["entry_trigger"] is None,
                                 -(r["plan"]["quality"]["score"])))
        plans = [{"setup_id": r["id"], **r["plan"]} for r in rows]
        best = plans[0]["status"] if plans else "NO_TRADE"
        reason = None if best != "NO_TRADE" else (
            "No setup passes every planning rule. " +
            (plans[0]["reasons"][0] if plans and plans[0]["reasons"] else ""))
        return {"symbol": symbol, "mode": mode, "plans": plans, "status": best,
                "reason": reason, "evaluated_at": res["evaluated_at"],
                "min_rr": self.planning_config.min_rr_for(mode), "is_demo": self.is_demo}

    def strategy_summary(self, symbol: str, mode: str) -> tuple[list[dict], str]:
        """Compact trade plans for one symbol (context for the AI explanation) and the best
        plan status, which caps the AI verdict."""
        try:
            res = self.plans_for(symbol, mode)
        except DataError:
            return [], "NO_TRADE"
        keys = ("strategy_name", "direction", "timeframe", "status", "risk_per_share",
                "reward_per_share_t1", "risk_reward_t1", "risk_reward_t2", "reasons")
        out = []
        for p in res["plans"]:
            if p["status"] == "NO_TRADE" and len(out) >= 3:
                continue
            row = {k: p.get(k) for k in keys}
            for k in ("entry_trigger", "entry", "stop", "target1", "target2"):
                row[k] = {"price": p[k]["price"], "basis": p[k]["basis"]} if p.get(k) else None
            row["quality_score"] = p["quality"]["score"] if p.get("quality") else None
            out.append(row)
            if len(out) >= 6:
                break
        return out, res["status"]

    def scan(self, modes: tuple[str, ...] = ("day", "swing")) -> dict:
        results, alerts, errors = [], [], []
        try:
            alerts += self.strategy_setups()["new_alerts"]
        except DataError as exc:
            errors.append({"symbol": "*", "mode": "strategies", "code": exc.code,
                           "error": str(exc)})
        try:
            alerts += self.swing_setups()["new_alerts"]
        except DataError as exc:
            errors.append({"symbol": "*", "mode": "swing_strategies", "code": exc.code,
                           "error": str(exc)})
        for mode in modes:
            timeframe = DEFAULT_TIMEFRAME[mode]
            for symbol in self.settings.watchlist:
                try:
                    a = self.analysis(symbol, mode, timeframe)
                except AnalysisBlocked as exc:
                    errors.append({"symbol": symbol, "mode": mode, "code": exc.code,
                                   "error": exc.message})
                    continue
                except (DataError, ValueError) as exc:
                    errors.append({"symbol": symbol, "mode": mode,
                                   "code": getattr(exc, "code", "ERROR"), "error": str(exc)})
                    continue
                alert = self.db.record_setup(a, self.is_demo)
                if alert:
                    alerts.append(alert)
                s = a["setup"]
                results.append({"symbol": symbol, "mode": mode, "timeframe": timeframe,
                                "status": s["status"], "title": s["title"],
                                "direction": s["direction"], "risk_reward": s["risk_reward"],
                                "confidence": s["confidence"]})
        self.last_scan = int(time.time())
        return {"scanned_at": self.last_scan, "results": results, "new_alerts": alerts,
                "errors": errors, "is_demo": self.is_demo}


class AnalysisBlocked(Exception):
    def __init__(self, code: str, message: str, data_status: dict) -> None:
        super().__init__(message)
        self.code, self.message, self.data_status = code, message, data_status


def _data_error(exc: DataError) -> HTTPException:
    status = 400 if isinstance(exc, InvalidRequest) else 503
    return HTTPException(status, {"code": exc.code, "message": str(exc),
                                  "display": "MARKET DATA UNAVAILABLE"})


def _blocked(exc: AnalysisBlocked) -> HTTPException:
    display = "MARKET DATA UNAVAILABLE" if exc.code == "MARKET_DATA_UNAVAILABLE" else \
        "ANALYSIS PAUSED"
    return HTTPException(503, {"code": exc.code, "message": exc.message, "display": display,
                               "data_status": exc.data_status})


def _check_mode_timeframe(mode: str, timeframe: str | None) -> str:
    if mode not in MODE_TIMEFRAMES:
        raise HTTPException(400, f"mode must be one of {list(MODE_TIMEFRAMES)}")
    timeframe = timeframe or DEFAULT_TIMEFRAME[mode]
    if timeframe not in TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {list(TIMEFRAMES)}")
    return timeframe


def _parse_dt(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, f"Invalid date/time {value!r}; use ISO 8601") from None
    return dt if dt.tzinfo else dt.replace(tzinfo=NY)  # naive inputs are New York time


def create_app(service: Service | None = None, run_scanner: bool = True) -> FastAPI:
    settings = service.settings if service else load_settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.service = service or Service(settings)
        task = None
        if run_scanner and settings.scan_interval_seconds > 0:
            task = asyncio.create_task(_scanner(app.state.service))
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _scanner(svc: Service) -> None:
        while True:
            try:
                await run_in_threadpool(svc.scan)
            except Exception:  # keep the loop alive; errors are surfaced per-symbol
                log.exception("Background scan failed")
            await asyncio.sleep(svc.settings.scan_interval_seconds)

    app = FastAPI(title="TradeScope AI", version="0.2.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                       allow_methods=["*"], allow_headers=["*"])

    def svc() -> Service:
        return app.state.service

    @app.get("/api/health")
    def health() -> dict:
        s = svc()
        return {
            "status": "ok",
            "data_mode": s.market.source.name,
            "is_demo": s.is_demo,
            "feed": s.market.feed_info(),
            "ai_enabled": s.ai_enabled,
            "ai_model": s.settings.claude_model if s.ai_enabled else None,
            "watchlist": s.settings.watchlist,
            "all_timeframes": [{"key": k, "label": v.label} for k, v in SPECS.items()],
            "timeframes": MODE_TIMEFRAMES,
            "default_timeframes": DEFAULT_TIMEFRAME,
            "last_scan": s.last_scan,
            "scan_interval_seconds": s.settings.scan_interval_seconds,
            "live_trading": False,
            "disclaimer": DISCLAIMER,
        }

    @app.get("/api/market/status")
    def market_status() -> dict:
        s = svc()
        return {"market": s.market.market_status(), "feed": s.market.feed_info(),
                "is_demo": s.is_demo}

    @app.get("/api/watchlist")
    def watchlist() -> dict:
        s = svc()
        try:
            quotes = s.market.get_quotes()
        except DataError as exc:
            raise _data_error(exc) from exc
        return {"is_demo": s.is_demo, "feed": s.market.feed_info(), "quotes": quotes,
                "market": s.market.market_status()}

    @app.get("/api/bars/{symbol}")
    def bars(symbol: str, timeframe: str = "5Min", session: str = "regular",
             limit: int = Query(BAR_LIMIT, ge=1, le=5000)) -> dict:
        s = svc()
        if timeframe not in TIMEFRAMES:
            raise HTTPException(400, f"timeframe must be one of {list(TIMEFRAMES)}")
        try:
            candles = s.market.get_candles(symbol, timeframe, session, limit)
        except DataError as exc:
            raise _data_error(exc) from exc
        return {"is_demo": s.is_demo, **candles.to_dict()}

    @app.get("/api/history/{symbol}")
    def history(symbol: str, timeframe: str, start: str, end: str | None = None,
                session: str = "regular", format: str = "json"):
        """Historical candles over an explicit range (paginated upstream) for backtesting."""
        s = svc()
        if timeframe not in TIMEFRAMES:
            raise HTTPException(400, f"timeframe must be one of {list(TIMEFRAMES)}")
        start_dt = _parse_dt(start)
        end_dt = _parse_dt(end) if end else datetime.now(timezone.utc)
        try:
            candles = s.market.get_history(symbol, timeframe, start_dt, end_dt, session)
        except DataError as exc:
            raise _data_error(exc) from exc
        if format == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["time_utc", "time_ny", "open", "high", "low", "close", "volume",
                        "complete"])
            for b, done in zip(candles.bars, candles.complete):
                w.writerow([datetime.fromtimestamp(b.time, timezone.utc).isoformat(),
                            datetime.fromtimestamp(b.time, NY).isoformat(),
                            b.open, b.high, b.low, b.close, b.volume, done])
            name = f"{candles.symbol}_{timeframe}_{candles.status['feed']}.csv"
            return Response(buf.getvalue(), media_type="text/csv",
                            headers={"Content-Disposition": f'attachment; filename="{name}"'})
        return {"is_demo": s.is_demo, **candles.to_dict()}

    @app.get("/api/analysis/{symbol}")
    def analysis(symbol: str, mode: str = "day", timeframe: str | None = None) -> dict:
        tf = _check_mode_timeframe(mode, timeframe)
        try:
            return svc().analysis(symbol, mode, tf)
        except AnalysisBlocked as exc:
            raise _blocked(exc) from exc
        except DataError as exc:
            raise _data_error(exc) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/levels/{symbol}")
    def key_levels(symbol: str, mode: str = "day", timeframe: str | None = None,
                   as_of: str | None = None) -> dict:
        """Automatic key levels (multi-timeframe). `as_of` replays a past moment."""
        if timeframe is not None and timeframe not in TIMEFRAMES:
            raise HTTPException(400, f"timeframe must be one of {list(TIMEFRAMES)}")
        when = _parse_dt(as_of) if as_of else None
        try:
            return svc().key_levels(symbol, mode, timeframe, when)
        except AnalysisBlocked as exc:
            raise _blocked(exc) from exc
        except DataError as exc:
            raise _data_error(exc) from exc
        except InsufficientData as exc:
            raise HTTPException(503, {"code": "INSUFFICIENT_DATA", "message": str(exc),
                                      "display": "ANALYSIS PAUSED"}) from exc

    @app.post("/api/ai/{symbol}")
    def ai_analysis(symbol: str, mode: str = "day", timeframe: str | None = None,
                    refresh: bool = False) -> dict:
        s = svc()
        tf = _check_mode_timeframe(mode, timeframe)
        if not s.ai_enabled:
            return {"available": False,
                    "reason": "Claude AI is not configured. Set ANTHROPIC_API_KEY in "
                              "backend/.env and restart the backend."}
        try:
            a = s.analysis(symbol, mode, tf)
            recent = [b.to_dict() for b in
                      s.market.get_candles(symbol, tf, "regular", BAR_LIMIT).completed_bars[-30:]]
        except AnalysisBlocked as exc:
            return {"available": False, "reason": f"Analysis paused: {exc.message}"}
        except (DataError, ValueError) as exc:
            return {"available": False, "reason": f"MARKET DATA UNAVAILABLE: {exc}"}
        a["strategy_setups"], a["plan_status"] = s.strategy_summary(a["symbol"], mode)
        feed = s.market.feed.id
        sig = "|".join(f"{x['strategy_name']}:{x['direction']}:{x['timeframe']}:{x['status']}"
                       for x in a["strategy_setups"])
        cache_key = (f"{a['symbol']}|{mode}|{tf}|{a['analyzed_through']}|{s.analyst.model}|{feed}"
                     f"|{sig}")
        if not refresh and (cached := s.db.get_ai(cache_key)):
            return {"available": True, "cached": True, "as_of": a["as_of"], **cached}
        try:
            result = s.analyst.explain(a, recent, s.is_demo)
        except AIUnavailable as exc:
            return {"available": False, "reason": str(exc)}
        result["engine_status"] = a["plan_status"]
        result["is_demo"] = s.is_demo
        s.db.put_ai(cache_key, result)
        return {"available": True, "cached": False, "as_of": a["as_of"], **result}

    @app.post("/api/scan")
    def scan() -> dict:
        return svc().scan()

    @app.get("/api/strategies/setups")
    def strategy_setups(symbol: str | None = None, as_of: str | None = None) -> dict:
        """Current day-trading setups (TRIGGERED / WATCHING / NO_TRADE) for the universe."""
        when = _parse_dt(as_of) if as_of else None
        try:
            return svc().strategy_setups([symbol] if symbol else None, when)
        except DataError as exc:
            raise _data_error(exc) from exc

    @app.get("/api/strategies/config")
    def strategy_config() -> dict:
        import inspect
        from .strategies import engine as eng
        return {"config": svc().strategy_config.to_dict(),
                "universe": svc().day_universe,
                "definitions": [{"id": s.id, "name": s.name,
                                 "rules": inspect.getmodule(s).__doc__.strip()}
                                for s in eng.STRATEGIES]}

    @app.get("/api/swing/setups")
    def swing_setups(symbol: str | None = None, as_of: str | None = None,
                     sort: str = "quality") -> dict:
        """Swing-trading scanner: setups on daily/weekly candles for the whole watchlist.
        sort: quality | ticker | strategy | direction | risk_reward | distance."""
        when = _parse_dt(as_of) if as_of else None
        try:
            return svc().swing_setups([symbol] if symbol else None, when, sort)
        except DataError as exc:
            raise _data_error(exc) from exc

    @app.get("/api/swing/config")
    def swing_config() -> dict:
        import inspect
        return {"config": svc().swing_config.to_dict(), "universe": svc().market.symbols,
                "timeframes": {"primary": ["1Day", "1Week"], "supporting": ["4Hour", "1Hour"]},
                "definitions": [{"id": st.id, "name": st.name,
                                 "rules": inspect.getmodule(st).__doc__.strip()}
                                for st in SWING_STRATEGIES]}

    @app.get("/api/plans/{symbol}")
    def trade_plans(symbol: str, mode: str = "day", as_of: str | None = None) -> dict:
        """Complete conditional trade plans for every setup on one symbol in one mode."""
        when = _parse_dt(as_of) if as_of else None
        try:
            return svc().plans_for(symbol, mode, when)
        except DataError as exc:
            raise _data_error(exc) from exc

    @app.get("/api/planning/config")
    def planning_config() -> dict:
        from .planning import plan as plan_mod
        return {"config": svc().planning_config.to_dict(),
                "rules": plan_mod.__doc__.strip(),
                "quality_note": "Setup quality is a transparent ranking of supporting "
                                "evidence (weights above), not a predicted win probability."}

    @app.get("/api/strategies/signals")
    def strategy_signals(limit: int = Query(100, ge=1, le=500), ticker: str | None = None) -> dict:
        return {"signals": svc().db.signals(limit, ticker.upper() if ticker else None)}

    @app.get("/api/setups/recent")
    def recent_setups(limit: int = Query(25, ge=1, le=200), mode: str | None = None) -> dict:
        return {"setups": svc().db.recent_setups(limit, mode)}

    @app.get("/api/alerts")
    def alerts(limit: int = Query(50, ge=1, le=200), unacknowledged: bool = False) -> dict:
        return {"alerts": svc().db.alerts(limit, unacknowledged)}

    @app.post("/api/alerts/{alert_id}/ack")
    def ack(alert_id: int) -> dict:
        if not svc().db.acknowledge_alert(alert_id):
            raise HTTPException(404, "Alert not found")
        return {"ok": True}

    return app


app = create_app()
