"""TradeScope AI backend: FastAPI app exposing market data, analysis, setups, alerts and AI."""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import logging
import re
import threading
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

from .ai.claude import AIUnavailable, ClaudeAnalyst
from .ai.facts import build_facts, cache_key as ai_cache_key, engine_status
from .ai.validate import ExplanationInvalid, validate_explanation
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
from .strategies.config import StrategyConfig, load_strategy_config
from .strategies.engine import STRATEGIES, evaluate_symbol
from .planning.config import PlanningConfig, load_planning_config
from .alerts import ALERT_TYPES, AlertEngine, PrefsError, WebhookNotifier
from .scanner.engine import OpportunityScanner, filter_rows
from .swing.config import SwingConfig, load_swing_config
from .swing.context import fmt_day
from .swing.engine import SORT_KEYS, SWING_STRATEGIES, evaluate_swing_symbol, sort_setups

log = logging.getLogger("tradescope")
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}(?:[.-][A-Z]{1,2})?$")

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
        self._ai_locks: dict[str, threading.Lock] = {}
        saved = self.db.get_watchlist()
        if saved:
            self.market.set_symbols(saved)
        else:
            self.db.set_watchlist(self.market.symbols)
        notifier = (WebhookNotifier(settings.alert_webhook_url)
                    if settings.alert_webhook_url else None)
        self.alerts = AlertEngine(self.db, notifier=notifier)
        self.scanner = OpportunityScanner(self)
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
            summary = [{k: l[k] for k in ("label", "type", "category", "timeframe", "low",
                                          "high", "side")}
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
        return list(self.market.symbols)  # every watchlist ticker is scanned for day setups

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
        new_signals: list[str] = []
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
                new_signals += self._record_signals(cached)
        order = {"TRIGGERED": 0, "WATCHING": 1, "NO_TRADE": 2}
        setups.sort(key=lambda s: (order[s["status"]], -(s["confirmed_at"] or 0), s["ticker"],
                                   s["strategy"], s["timeframe"], s["direction"]))
        return {"evaluated_at": when.astimezone(NY).isoformat(), "replay": as_of is not None,
                "universe": symbols, "timeframes": ["5Min", "15Min"],
                "context_timeframes": ["1Hour", "1Day"], "symbols": per_symbol,
                "is_demo": self.is_demo, "setups": setups, "new_signals": new_signals,
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

    def _record_signals(self, setups: list[dict]) -> list[str]:
        """Append every accepted trigger to the immutable signal log; returns new ids."""
        new = []
        for s in setups:
            for t in s.get("triggers_today", []):
                if not t["accepted"]:
                    continue
                sid = signal_id(s["ticker"], s["strategy"], s["timeframe"], s["direction"],
                                s["session_date"], t["time"])
                payload = s if s["trigger_time"] == t["time"] else {**s, "note": "earlier trigger"}
                if self.db.record_signal(sid, payload, t, self.is_demo):
                    new.append(sid)
        return new

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
        new_signals: list[str] = []
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
                new_signals += self._record_swing_signals(cached)
        return {"evaluated_at": when.astimezone(NY).isoformat(), "replay": as_of is not None,
                "mode": "swing", "universe": symbols, "timeframes": list(self.SWING_REQUIRED),
                "supporting_timeframes": ["4Hour", "1Hour"], "symbols": per_symbol,
                "sort": sort, "sort_keys": list(SORT_KEYS), "is_demo": self.is_demo,
                "setups": sort_setups(setups, sort), "new_signals": new_signals,
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

    def _record_swing_signals(self, setups: list[dict]) -> list[str]:
        """Log each fresh, accepted swing trigger once (immutable); returns new ids."""
        new = []
        for s in setups:
            if s["status"] != TRIGGERED:
                continue
            t = next(t for t in s["triggers"] if t["time"] == s["trigger_time"])
            if self.db.record_signal(s["id"], {**s, "session_date": fmt_day(t["time"])}, t,
                                     self.is_demo, mode="swing"):
                new.append(s["id"])
        return new

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

    def setup_with_plan(self, symbol: str, mode: str, setup_id: str | None
                        ) -> tuple[dict | None, dict | None]:
        """The selected setup (by id) or the symbol's best plan in this mode."""
        symbol = self.market.check_symbol(symbol)
        if mode == "swing":
            rows = self.swing_setups([symbol], record=False)["setups"]
        elif symbol in self.day_universe:
            rows = self.strategy_setups([symbol], record=False)["setups"]
        else:
            return None, None
        rows = [r for r in rows if r["ticker"] == symbol and r.get("plan")]
        if setup_id:
            hit = next((r for r in rows if r["id"] == setup_id), None)
            if hit is None:
                raise InvalidRequest(f"setup {setup_id} is not a current {mode} setup for "
                                     f"{symbol}")
            return hit, hit["plan"]
        order = {"TRIGGERED": 0, "WATCHING": 1, "NO_TRADE": 2}
        rows.sort(key=lambda r: (order[r["status"]], r["plan"]["entry_trigger"] is None,
                                 -r["plan"]["quality"]["score"]))
        return (rows[0], rows[0]["plan"]) if rows else (None, None)

    def ai_explanation(self, symbol: str, mode: str, setup_id: str | None = None,
                       refresh: bool = False, generate: bool = True) -> dict:
        """Claude's explanation of one setup, validated against the verified fact sheet.

        The deterministic facts are always returned, so the technical analysis survives any
        AI failure. Explanations are cached in SQLite by a hash of the facts, model and
        effort: the same candles and plan never cost a second API call. With
        generate=False only the cache is consulted (no API call)."""
        if mode not in ("day", "swing"):
            raise InvalidRequest("mode must be day or swing")
        setup, plan = self.setup_with_plan(symbol, mode, setup_id)
        tf = plan["timeframe"] if plan else DEFAULT_TIMEFRAME[mode]
        try:
            analysis = self.analysis(symbol, mode, tf)
        except AnalysisBlocked as exc:
            return {"available": False, "facts": None, "status": "NO_TRADE",
                    "reason": f"Analysis paused: {exc.message}"}
        facts = build_facts(analysis, setup, plan, mode, self.is_demo)
        base = {"facts": facts, "status": engine_status(facts),
                "setup_id": setup["id"] if setup else None, "is_demo": self.is_demo}
        if not self.ai_enabled:
            return {**base, "available": False,
                    "reason": "AI explanation unavailable: Claude is not configured. Set "
                              "ANTHROPIC_API_KEY in backend/.env and restart the backend."}
        key = ai_cache_key(facts, self.analyst.model, self.analyst.effort)
        if not refresh and (cached := self.db.get_ai(key)):
            return {**base, "available": True, "cached": True, **cached}
        if not generate:
            return {**base, "available": False, "cached": False, "not_generated": True,
                    "reason": "Not generated yet for the current data."}
        with self._ai_locks.setdefault(key, threading.Lock()):
            if not refresh and (cached := self.db.get_ai(key)):  # filled while we waited
                return {**base, "available": True, "cached": True, **cached}
            try:
                raw, model = self.analyst.explain_setup(facts)
                checked = validate_explanation(raw, facts)
            except AIUnavailable as exc:
                return {**base, "available": False,
                        "reason": f"AI explanation unavailable: {exc}"}
            except ExplanationInvalid as exc:
                log.warning("Claude explanation rejected for %s %s: %s", symbol, mode, exc)
                return {**base, "available": False,
                        "reason": f"AI explanation unavailable: the response failed "
                                  f"validation ({exc})"}
            result = {**checked, "model": model,
                      "generated_at": datetime.now(NY).isoformat(timespec="seconds")}
            self.db.put_ai(key, result)
        return {**base, "available": True, "cached": False, **result}

    def scan(self) -> dict:
        """Run the opportunity scanner now (also records alerts and the recent-setups
        feed). Kept for /api/scan; the scheduled loop uses scanner.run()."""
        snap = self.scanner.run(force=True, trigger="manual")
        return {"scanned_at": self.last_scan, "results": snap["results"],
                "new_alerts": snap["new_alerts"], "errors": snap["errors"],
                "is_demo": self.is_demo, "scan_id": snap["scan_id"]}

    # ------------------------------------------------------------------ watchlist

    MAX_WATCHLIST = 30  # keeps a full scan inside the market-data rate limit

    def add_symbol(self, symbol: str) -> list[str]:
        sym = symbol.strip().upper()
        if not SYMBOL_RE.match(sym):
            raise InvalidRequest(f"{symbol!r} is not a valid ticker symbol.")
        if sym in self.market.symbols:
            raise InvalidRequest(f"{sym} is already on the watchlist.")
        if len(self.market.symbols) >= self.MAX_WATCHLIST:
            raise InvalidRequest(f"The watchlist is limited to {self.MAX_WATCHLIST} tickers.")
        self.market.probe_symbol(sym)  # rejects tickers the feed has no data for
        self._set_watchlist(self.market.symbols + [sym])
        return self.market.symbols

    def remove_symbol(self, symbol: str) -> list[str]:
        sym = self.market.check_symbol(symbol)
        if len(self.market.symbols) <= 1:
            raise InvalidRequest("The watchlist needs at least one ticker.")
        self._set_watchlist([s for s in self.market.symbols if s != sym])
        return self.market.symbols

    def _set_watchlist(self, symbols: list[str]) -> None:
        self.market.set_symbols(symbols)
        self.db.set_watchlist(symbols)
        keep = set(symbols)
        self._strategy_cache = {k: v for k, v in self._strategy_cache.items() if k[0] in keep}
        self._swing_cache = {k: v for k, v in self._swing_cache.items() if k[0] in keep}


    # ------------------------------------------------------------- user price levels

    def add_price_level(self, ticker: str, price: float, condition: str | None,
                        note: str) -> dict:
        """One-shot alert when price reaches `price`. `condition` (above/below) defaults to
        the side of the current price; a level that is already satisfied is rejected."""
        sym = self.market.check_symbol(ticker)
        if not (price > 0):
            raise InvalidRequest("price must be positive")
        if condition not in (None, "above", "below"):
            raise InvalidRequest("condition must be 'above' or 'below'")
        try:
            current = next((q.get("price") for q in self.market.get_quotes()
                            if q["symbol"] == sym), None)
        except DataError:
            current = None
        if current is None and condition is None:
            raise InvalidRequest("No current price is available; specify condition above/below.")
        if condition is None:
            condition = "above" if price > current else "below"
        if current is not None and (current >= price if condition == "above"
                                    else current <= price):
            raise InvalidRequest(f"{sym} is already {condition} {price:.2f} "
                                 f"(price {current:.2f}).")
        return self.db.add_price_level(sym, round(price, 4), condition, note.strip()[:120])


class WatchlistChange(BaseModel):
    symbol: str


class PriceLevelIn(BaseModel):
    ticker: str
    price: float
    condition: str | None = None
    note: str = ""


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
        """Server-side scheduler: every SCAN_INTERVAL_SECONDS, rescan only if a new eligible
        candle can exist (or the watchlist changed). No browser tab is needed."""
        while True:
            try:
                if svc.scanner.due():
                    await run_in_threadpool(svc.scanner.run, False, "scheduled")
                svc.scanner.last_error = None
            except Exception as exc:  # keep the loop alive; errors are surfaced per-symbol
                svc.scanner.last_error = str(exc)
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
            "watchlist": s.market.symbols,
            "all_timeframes": [{"key": k, "label": v.label} for k, v in SPECS.items()],
            "timeframes": MODE_TIMEFRAMES,
            "default_timeframes": DEFAULT_TIMEFRAME,
            "last_scan": s.last_scan,
            "scan_interval_seconds": s.settings.scan_interval_seconds,
            "scanner": {"scan_count": s.scanner.scan_count, "due": s.scanner.due(),
                        "last_error": s.scanner.last_error,
                        "last_finished_at": (s.scanner.snapshot or {}).get("finished_at")},
            "alerts": {**s.db.alert_counts(), "webhook": s.alerts.notifier is not None,
                       "last_run": ({k: v for k, v in s.alerts.last_run.items()
                                     if k != "suppressed"}
                                    | {"suppressed": len(s.alerts.last_run["suppressed"])}
                                    if s.alerts.last_run else None)},
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

    @app.post("/api/watchlist")
    def add_to_watchlist(body: WatchlistChange) -> dict:
        try:
            symbols = svc().add_symbol(body.symbol)
        except DataError as exc:
            raise _data_error(exc) from exc
        return {"watchlist": symbols}

    @app.delete("/api/watchlist/{symbol}")
    def remove_from_watchlist(symbol: str) -> dict:
        try:
            symbols = svc().remove_symbol(symbol)
        except DataError as exc:
            raise _data_error(exc) from exc
        return {"watchlist": symbols}

    @app.get("/api/scanner")
    def scanner(mode: str | None = None, side: str | None = None, status: str | None = None,
                min_rr: float | None = None, min_quality: float | None = None,
                strategy: str | None = None, include_no_trade: bool = True) -> dict:
        """Latest opportunity-scanner snapshot (computed server-side), filtered."""
        s = svc()
        if mode not in (None, "day", "swing") or side not in (None, "bullish", "bearish"):
            raise HTTPException(400, {"code": "INVALID_REQUEST",
                                      "message": "mode must be day|swing, side bullish|bearish"})
        snap = s.scanner.snapshot or s.scanner.run(trigger="first request")
        rows = filter_rows(snap["rows"], mode=mode, side=side, status=status, min_rr=min_rr,
                           min_quality=min_quality, strategy=strategy,
                           include_no_trade=include_no_trade)
        hidden = ("rows", "results", "new_alerts", "setup_states")
        return {**{k: v for k, v in snap.items() if k not in hidden},
                "rows": rows, "total_rows": len(snap["rows"]),
                "due": s.scanner.due(), "scan_count": s.scanner.scan_count,
                "poll_seconds": s.settings.scan_interval_seconds,
                "last_error": s.scanner.last_error}

    @app.post("/api/scanner/run")
    def scanner_run() -> dict:
        snap = svc().scanner.run(force=True, trigger="manual")
        return {"scan_id": snap["scan_id"], "finished_at": snap["finished_at"],
                "rows": len(snap["rows"]), "duration_ms": snap["duration_ms"],
                "new_alerts": len(snap["new_alerts"])}

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

    @app.get("/api/ai/{symbol}")
    def ai_cached(symbol: str, mode: str = "day", setup_id: str | None = None) -> dict:
        """Cached explanation for the current data, if any (never calls Claude)."""
        try:
            return svc().ai_explanation(symbol, mode, setup_id, generate=False)
        except DataError as exc:
            raise _data_error(exc) from exc

    @app.post("/api/ai/{symbol}")
    def ai_generate(symbol: str, mode: str = "day", setup_id: str | None = None,
                    refresh: bool = False) -> dict:
        """Generate (or return the cached) Claude explanation for a setup."""
        try:
            return svc().ai_explanation(symbol, mode, setup_id, refresh=refresh)
        except DataError as exc:
            raise _data_error(exc) from exc

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
    def alerts(limit: int = Query(50, ge=1, le=500), unacknowledged: bool = False,
               ticker: str | None = None, type: str | None = None,
               after_id: int | None = None) -> dict:
        """Alerts produced by the server-side alert engine (newest first)."""
        s = svc()
        types = [t.strip() for t in type.split(",")] if type else None
        if types and not set(types) <= set(ALERT_TYPES):
            raise HTTPException(400, f"type must be among {', '.join(ALERT_TYPES)}")
        return {"alerts": s.db.alerts(limit, unacknowledged, ticker.upper() if ticker else None,
                                      types, after_id),
                "counts": s.db.alert_counts(),
                "last_run": s.alerts.last_run, "is_demo": s.is_demo}

    @app.post("/api/alerts/ack-all")
    def ack_all() -> dict:
        return {"ok": True, "acknowledged": svc().db.acknowledge_all_alerts()}

    @app.post("/api/alerts/{alert_id}/ack")
    def ack(alert_id: int) -> dict:
        if not svc().db.acknowledge_alert(alert_id):
            raise HTTPException(404, "Alert not found")
        return {"ok": True}

    def _prefs_body(s: Service, prefs: dict) -> dict:
        n = s.alerts.notifier
        return {"preferences": prefs, "types": ALERT_TYPES,
                "webhook": {"configured": n is not None, "host": n.host if n else None}}

    @app.get("/api/alerts/preferences")
    def alert_prefs() -> dict:
        s = svc()
        return _prefs_body(s, s.alerts.prefs())

    @app.put("/api/alerts/preferences")
    def update_alert_prefs(body: dict = Body(...)) -> dict:
        s = svc()
        try:
            return _prefs_body(s, s.alerts.update_prefs(body))
        except PrefsError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/alerts/preferences/reset")
    def reset_alert_prefs() -> dict:
        s = svc()
        return _prefs_body(s, s.alerts.reset_prefs())

    @app.get("/api/alerts/levels")
    def price_levels() -> dict:
        return {"levels": svc().db.price_levels()}

    @app.post("/api/alerts/levels")
    def add_price_level(body: PriceLevelIn) -> dict:
        try:
            return {"level": svc().add_price_level(body.ticker, body.price, body.condition,
                                                   body.note)}
        except InvalidRequest as exc:
            raise HTTPException(400, str(exc)) from None

    @app.delete("/api/alerts/levels/{level_id}")
    def delete_price_level(level_id: int) -> dict:
        if not svc().db.delete_price_level(level_id):
            raise HTTPException(404, "Level not found")
        return {"ok": True}

    return app


app = create_app()
