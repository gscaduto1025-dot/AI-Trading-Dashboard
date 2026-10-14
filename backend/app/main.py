"""TradeScope AI backend: FastAPI app exposing market data, analysis, setups, alerts and AI."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

from .ai.claude import AIUnavailable, ClaudeAnalyst
from .analysis.engine import analyze
from .config import Settings, load_settings
from .data.alpaca import AlpacaDataProvider
from .data.base import (DEFAULT_TIMEFRAME, MODE_TIMEFRAMES, TIMEFRAMES, Bar, DataError,
                        MarketDataProvider)
from .data.demo import DemoDataProvider
from .db import Database

log = logging.getLogger("tradescope")

BAR_LIMIT = 300
DISCLAIMER = ("Educational analysis only — not financial advice. "
              "TradeScope AI does not place trades.")


class Service:
    """Holds the provider, database and caches shared by the routes."""

    def __init__(self, settings: Settings, provider: MarketDataProvider | None = None,
                 db: Database | None = None, analyst: ClaudeAnalyst | None = None) -> None:
        self.settings = settings
        if provider is None:
            provider = (AlpacaDataProvider(settings.alpaca_key_id, settings.alpaca_secret_key,
                                           settings.alpaca_data_url, settings.alpaca_feed)
                        if settings.alpaca_enabled else DemoDataProvider())
        self.provider = provider
        self.db = db or Database(settings.database_path)
        self.analyst = analyst or ClaudeAnalyst(settings.claude_model, settings.claude_effort)
        self.ai_enabled = settings.anthropic_api_key_present or analyst is not None
        self._bar_cache: dict[tuple[str, str], tuple[float, list[Bar]]] = {}
        self._lock = threading.Lock()
        self.last_scan: int | None = None

    @property
    def is_demo(self) -> bool:
        return self.provider.is_demo

    def bars(self, symbol: str, timeframe: str) -> list[Bar]:
        ttl = 3600.0 if self.is_demo else 20.0
        key = (symbol, timeframe)
        with self._lock:
            hit = self._bar_cache.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
        bars = self.provider.get_bars(symbol, timeframe, BAR_LIMIT)
        with self._lock:
            self._bar_cache[key] = (time.time(), bars)
        return bars

    def analysis(self, symbol: str, mode: str, timeframe: str) -> dict:
        result = analyze(symbol, self.bars(symbol, timeframe), mode, timeframe)
        result["is_demo"] = self.is_demo
        return result

    def scan(self, modes: tuple[str, ...] = ("day", "swing")) -> dict:
        results, alerts, errors = [], [], []
        for mode in modes:
            timeframe = DEFAULT_TIMEFRAME[mode]
            for symbol in self.settings.watchlist:
                try:
                    a = self.analysis(symbol, mode, timeframe)
                except (DataError, ValueError) as exc:
                    errors.append({"symbol": symbol, "mode": mode, "error": str(exc)})
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


def _check_mode_timeframe(mode: str, timeframe: str | None) -> str:
    if mode not in MODE_TIMEFRAMES:
        raise HTTPException(400, f"mode must be one of {list(MODE_TIMEFRAMES)}")
    timeframe = timeframe or DEFAULT_TIMEFRAME[mode]
    if timeframe not in TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {list(TIMEFRAMES)}")
    return timeframe


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

    app = FastAPI(title="TradeScope AI", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                       allow_methods=["*"], allow_headers=["*"])

    def svc() -> Service:
        return app.state.service

    @app.get("/api/health")
    def health() -> dict:
        s = svc()
        return {
            "status": "ok",
            "data_mode": s.provider.name,
            "is_demo": s.is_demo,
            "ai_enabled": s.ai_enabled,
            "ai_model": s.settings.claude_model if s.ai_enabled else None,
            "watchlist": s.settings.watchlist,
            "timeframes": MODE_TIMEFRAMES,
            "default_timeframes": DEFAULT_TIMEFRAME,
            "last_scan": s.last_scan,
            "scan_interval_seconds": s.settings.scan_interval_seconds,
            "live_trading": False,
            "disclaimer": DISCLAIMER,
        }

    @app.get("/api/watchlist")
    def watchlist() -> dict:
        s = svc()
        try:
            quotes = s.provider.get_quotes(s.settings.watchlist)
        except DataError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"is_demo": s.is_demo, "quotes": [q.to_dict() for q in quotes]}

    @app.get("/api/bars/{symbol}")
    def bars(symbol: str, timeframe: str = "5Min") -> dict:
        s = svc()
        if timeframe not in TIMEFRAMES:
            raise HTTPException(400, f"timeframe must be one of {list(TIMEFRAMES)}")
        try:
            data = s.bars(symbol.upper(), timeframe)
        except DataError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"symbol": symbol.upper(), "timeframe": timeframe, "is_demo": s.is_demo,
                "bars": [b.to_dict() for b in data]}

    @app.get("/api/analysis/{symbol}")
    def analysis(symbol: str, mode: str = "day", timeframe: str | None = None) -> dict:
        tf = _check_mode_timeframe(mode, timeframe)
        try:
            return svc().analysis(symbol.upper(), mode, tf)
        except DataError as exc:
            raise HTTPException(502, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

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
            a = s.analysis(symbol.upper(), mode, tf)
        except (DataError, ValueError) as exc:
            raise HTTPException(502, str(exc)) from exc
        cache_key = f"{a['symbol']}|{mode}|{tf}|{a['as_of']}|{s.analyst.model}|{s.is_demo}"
        if not refresh and (cached := s.db.get_ai(cache_key)):
            return {"available": True, "cached": True, "as_of": a["as_of"], **cached}
        recent = [b.to_dict() for b in s.bars(a["symbol"], tf)[-30:]]
        try:
            result = s.analyst.explain(a, recent, s.is_demo)
        except AIUnavailable as exc:
            return {"available": False, "reason": str(exc)}
        result["engine_status"] = a["setup"]["status"]
        result["is_demo"] = s.is_demo
        s.db.put_ai(cache_key, result)
        return {"available": True, "cached": False, "as_of": a["as_of"], **result}

    @app.post("/api/scan")
    def scan() -> dict:
        return svc().scan()

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
