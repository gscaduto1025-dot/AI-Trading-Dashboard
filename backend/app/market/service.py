"""MarketDataService: the one place the app gets candles and quotes from.

Responsibilities, applied identically to the Alpaca and Demo sources:
* compute fetch windows from the NYSE calendar and the feed's delay (no data newer than the
  feed allows is requested);
* validate OHLCV integrity and timestamp order (see validation.py);
* filter to the regular or extended session and build session-anchored 1H/4H candles;
* mark which candles are complete (closed) at the feed's data cutoff;
* label every response with the feed, its latency (real-time / delayed / historical / demo)
  and freshness (fresh / stale / unavailable);
* cache responses with timeframe-aware TTLs and briefly cache failures so errors don't
  hammer the provider.

It never substitutes or invents data: if the source fails, the error propagates and the
caller reports MARKET DATA UNAVAILABLE.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from ..data.base import Bar, DataError, InvalidRequest
from .calendar import NY, MarketCalendar, calendar_from_alpaca
from .sources import FeedInfo, parse_time
from .timeframes import (TimeframeSpec, aggregate, bar_end, filter_session, lookback_start,
                         ny_date, spec)
from .validation import QualityReport, count_intraday_gaps, validate_bars

log = logging.getLogger(__name__)

COMPLETION_GRACE_SECONDS = 5  # allow late prints to land before calling a candle final
MAX_HISTORY_BARS = 50_000
MIN_ANALYSIS_BARS = 60


class Source(Protocol):
    name: str
    is_demo: bool
    feed: FeedInfo

    def fetch_bars(self, symbol: str, timeframe: str, start: datetime, end: datetime,
                   max_items: int = ...) -> list[Bar]: ...

    def fetch_snapshots(self, symbols: list[str]) -> dict[str, dict]: ...


@dataclass
class CandleSet:
    symbol: str
    timeframe: str
    session: str
    bars: list[Bar]
    complete: list[bool]
    status: dict
    quality: QualityReport = field(default_factory=QualityReport)

    @property
    def completed_bars(self) -> list[Bar]:
        return [b for b, done in zip(self.bars, self.complete) if done]

    def analysis_block(self) -> tuple[str, str] | None:
        """(code, message) explaining why this data must not be analysed, or None."""
        if self.status["freshness"] == "unavailable":
            return "MARKET_DATA_UNAVAILABLE", self.status.get("reason") or "No market data."
        if self.status["freshness"] == "stale":
            return "STALE_DATA", self.status.get("reason") or "Market data is stale."
        if not self.quality.ok:
            return "DATA_INTEGRITY", "Market data failed integrity checks: " + \
                "; ".join(self.quality.errors[:3])
        if len(self.completed_bars) < MIN_ANALYSIS_BARS:
            return "INSUFFICIENT_DATA", (f"Only {len(self.completed_bars)} completed candles "
                                         f"available; at least {MIN_ANALYSIS_BARS} are required.")
        return None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "session": self.session,
            "bars": [b.to_dict() for b in self.bars],
            "last_bar_complete": bool(self.complete and self.complete[-1]),
            "completed_count": sum(self.complete),
            "status": self.status,
            "quality": self.quality.to_dict(),
        }


class _TTLCache:
    def __init__(self, max_entries: int = 512) -> None:
        self._data: OrderedDict[tuple, tuple[float, object]] = OrderedDict()
        self._lock = threading.Lock()
        self.max_entries = max_entries

    def get(self, key: tuple, now: float):
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            if hit[0] < now:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return hit[1]

    def put(self, key: tuple, value: object, ttl: float, now: float) -> None:
        with self._lock:
            self._data[key] = (now + ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)


def _iso(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, NY).isoformat() if ts else None


class MarketDataService:
    def __init__(self, source: Source, *, symbols: list[str],
                 now_fn: Callable[[], datetime] | None = None,
                 calendar: MarketCalendar | None = None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.source = source
        self.feed = source.feed
        self.symbols = [s.upper() for s in symbols]
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._fixed_calendar = calendar
        self._calendar: MarketCalendar = calendar or MarketCalendar()
        self._calendar_day: date | None = None
        self.calendar_error: str | None = None
        self._cache = _TTLCache()
        self._raw: dict[tuple, dict] = {}  # shared provider bars (see _raw_bars)
        self._raw_locks: dict[tuple, threading.Lock] = {}
        self._raw_depth: dict[str, timedelta] = {}  # deepest lookback seen per timeframe
        self._mono = monotonic  # cache clock (injectable so tests can move time)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ time & calendar

    def now(self) -> datetime:
        return self._now_fn()

    def cutoff(self, now: datetime | None = None) -> datetime:
        """Newest moment the configured feed may legitimately provide data for."""
        return (now or self.now()) - timedelta(seconds=self.feed.delay_seconds)

    @property
    def calendar(self) -> MarketCalendar:
        """Alpaca's calendar when available (refreshed daily), NYSE rules otherwise."""
        fetch = getattr(self.source, "fetch_calendar", None)
        if self._fixed_calendar is not None or fetch is None:
            return self._calendar
        today = self.now().astimezone(NY).date()
        with self._lock:
            if self._calendar_day != today:
                start, end = today - timedelta(days=550), today + timedelta(days=60)
                try:
                    self._calendar = calendar_from_alpaca(fetch(start, end), start, end)
                    self.calendar_error = None
                except DataError as exc:
                    log.warning("Alpaca calendar unavailable, using NYSE rules: %s", exc)
                    self._calendar = MarketCalendar()
                    self.calendar_error = str(exc)
                self._calendar_day = today
        return self._calendar

    def market_status(self) -> dict:
        now = self.now()
        status = self.calendar.status(now)
        status["data_cutoff"] = self.cutoff(now).astimezone(NY).isoformat()
        status["calendar_error"] = self.calendar_error
        return status

    def feed_info(self) -> dict:
        info = self.feed.to_dict()
        info["source"] = self.source.name
        info["adjustment"] = getattr(self.source, "adjustment", None)
        return info

    # ------------------------------------------------------------------------ candles

    def set_symbols(self, symbols: list[str]) -> None:
        """Replace the watchlist (candle caches are per symbol and stay valid)."""
        self.symbols = [s.upper() for s in symbols]

    def probe_symbol(self, symbol: str) -> None:
        """Raise InvalidRequest unless the feed has market data for `symbol`. Demo data
        simulates any ticker (clearly labelled DEMO DATA)."""
        if self.source.is_demo:
            return
        snaps = self.source.fetch_snapshots([symbol])
        snap = snaps.get(symbol) or {}
        if not (snap.get("latestTrade") or snap.get("dailyBar")):
            raise InvalidRequest(f"No market data for {symbol!r} on the {self.feed.label} feed.")

    def check_symbol(self, symbol: str) -> str:
        symbol = symbol.upper()
        if symbol not in self.symbols:
            raise InvalidRequest(f"Unsupported symbol {symbol!r}; choose one of {self.symbols}")
        return symbol

    def _ttl(self, tf: TimeframeSpec, phase: str, session: str = "extended",
             until_open: float | None = None) -> float:
        live = phase in ("regular", "pre-market", "after-hours")
        if self.source.is_demo:
            return 3600.0
        if tf.intraday:
            if live and session == "regular" and phase != "regular":
                # Pre-market / after-hours: no regular-session candle can form before the
                # next open, so regular-session candles are re-fetched only every 5 min -
                # but never cached past the open.
                return max(5.0, min(300.0, until_open if until_open is not None else 300.0))
            return min(30.0, max(5.0, tf.minutes * 60 / 6)) if live else 300.0
        return 60.0 if live else 900.0

    def get_candles(self, symbol: str, timeframe: str, session: str = "regular",
                    limit: int = 300) -> CandleSet:
        symbol = self.check_symbol(symbol)
        tf = spec(timeframe)
        session = session if tf.intraday else "regular"
        if session not in ("regular", "extended"):
            raise InvalidRequest("session must be 'regular' or 'extended'")
        limit = max(1, min(limit, 5000))
        now = self.now()
        cal = self.calendar
        key = ("candles", symbol, tf.key, session, limit)
        cached = self._cache.get(key, self._mono())
        if isinstance(cached, DataError):
            raise cached
        if cached is not None:
            return cached

        cutoff = self.cutoff(now)
        start = lookback_start(tf, cal, cutoff, limit, session)
        status = cal.status(now)
        phase = status["phase"]
        nxt = status.get("next_open")
        until_open = ((datetime.fromisoformat(nxt) - now).total_seconds()
                      if isinstance(nxt, str) else None)
        ttl = self._ttl(tf, phase, session, until_open)
        try:
            raw = self._raw_bars(symbol, tf.fetch, start, cutoff, ttl)
        except DataError as exc:
            self._cache.put(key, exc, 10.0, self._mono())  # brief negative cache
            raise
        result = self._build(symbol, tf, session, raw, now, cal, limit)
        self._cache.put(key, result, ttl, self._mono())
        return result

    def reserve_lookback(self, timeframe: str, limit: int, session: str = "regular") -> None:
        """Declare a candle length the app will request, so the first full download of
        the underlying provider timeframe already covers it (see _raw_bars)."""
        tf = spec(timeframe)
        cutoff = self.cutoff()
        start = lookback_start(tf, self.calendar, cutoff, max(1, min(limit, 5000)), session)
        with self._lock:
            cur = self._raw_depth.get(tf.fetch, timedelta(0))
            self._raw_depth[tf.fetch] = max(cur, cutoff - start + timedelta(days=1))

    RAW_FULL_REFRESH_SECONDS = 6 * 3600  # full re-fetch: picks up split re-adjustments
    RAW_OVERLAP = {True: timedelta(days=1), False: timedelta(days=14)}  # intraday / daily+

    def _raw_bars(self, symbol: str, fetch_tf: str, start: datetime, cutoff: datetime,
                  ttl: float) -> list[Bar]:
        """Provider bars for [start, cutoff), shared by every candle request that needs the
        same Alpaca timeframe (e.g. 1Hour and 4Hour candles of any length all come from
        30Min bars). Alpaca pages intraday bars in small chunks (a few hundred per page),
        so re-downloading the whole history for each candle length and every refresh
        exhausted the request quota. Instead:
          * a request inside a fresh series (within `ttl`) is served by slicing it;
          * a stale series is refreshed incrementally: only bars from the last stored bar
            minus an overlap (1 day intraday, 14 days daily/weekly, so late corrections
            and the forming bar are re-read) are fetched again;
          * a request reaching further back, or a series older than 6 hours, is fetched in
            full.
        Returned bars are exactly what a direct fetch of [start, cutoff) would return."""
        k = (symbol, fetch_tf)
        with self._lock:
            lock = self._raw_locks.setdefault(k, threading.Lock())
            # Remember the deepest history any caller needed for this timeframe, so a full
            # fetch for the next symbol covers every candle length at once instead of being
            # re-downloaded wider each time a longer lookback is requested.
            depth = max(self._raw_depth.get(fetch_tf, timedelta(0)), cutoff - start)
            self._raw_depth[fetch_tf] = depth
        with lock:
            mono = self._mono()
            ent = self._raw.get(k)
            if ent and (ent["start"] > start
                        or mono - ent["full_at"] > self.RAW_FULL_REFRESH_SECONDS):
                ent = None
            if ent is None:
                full_start = min(start, cutoff - depth)
                bars = self.source.fetch_bars(symbol, fetch_tf, full_start, cutoff,
                                              max_items=MAX_HISTORY_BARS)
                ent = {"start": full_start, "cutoff": cutoff, "bars": bars, "fetched": mono,
                       "full_at": mono}
                self._raw[k] = ent
            elif mono - ent["fetched"] >= ttl:
                intraday = spec(fetch_tf).intraday
                last = ent["bars"][-1].time if ent["bars"] else int(ent["start"].timestamp())
                since = max(ent["start"], datetime.fromtimestamp(last, timezone.utc)
                            - self.RAW_OVERLAP[intraday])
                new = self.source.fetch_bars(symbol, fetch_tf, since, cutoff,
                                             max_items=MAX_HISTORY_BARS)
                since_ts = int(since.timestamp())
                ent["bars"] = [b for b in ent["bars"] if b.time < since_ts] + new
                ent["cutoff"], ent["fetched"] = cutoff, mono
            lo, hi = int(start.timestamp()), int(cutoff.timestamp())
            return [b for b in ent["bars"] if lo <= b.time < hi]

    def get_history(self, symbol: str, timeframe: str, start: datetime, end: datetime,
                    session: str = "regular", any_symbol: bool = False) -> CandleSet:
        """Full candle history between `start` and `end` (paginated) for analysis/backtests.
        `any_symbol` allows tickers outside the watchlist (point-in-time backtest universes,
        including delisted names); the caller validates the symbol format."""
        symbol = symbol.upper() if any_symbol else self.check_symbol(symbol)
        tf = spec(timeframe)
        session = session if tf.intraday else "regular"
        now = self.now()
        end = min(end, self.cutoff(now))
        if start >= end:
            raise InvalidRequest("start must be before end (and before the feed's data cutoff)")
        key = ("history", symbol, tf.key, session, int(start.timestamp()), int(end.timestamp()))
        cached = self._cache.get(key, self._mono())
        if cached is not None and not isinstance(cached, DataError):
            return cached
        raw = self.source.fetch_bars(symbol, tf.fetch, start, end, max_items=MAX_HISTORY_BARS)
        result = self._build(symbol, tf, session, raw, now, self.calendar, None)
        ttl = 86400.0 if result.complete and all(result.complete) else 60.0
        self._cache.put(key, result, ttl, self._mono())
        return result

    LIQUIDITY_SESSIONS = 20

    def consolidated_liquidity(self, symbol: str) -> dict | None:
        """Average daily dollar volume (close x volume) over the last 20 completed sessions
        before the current one, from consolidated (SIP) daily bars.

        Only for the IEX feed, whose volume is IEX's own trades - typically 1-6% of
        consolidated volume - so a dollar-volume floor meant for market-wide liquidity
        cannot be applied to it. Those sessions are all more than 15 minutes old, which
        Alpaca's free plan allows for SIP. Returns None when not applicable or unavailable
        (callers then fall back to the feed's own volume and say so)."""
        if self.source.is_demo or self.feed.bars_feed != "iex":
            return None
        now = self.now()
        current = self.calendar.last_session_started(self.cutoff(now), "regular").day
        key = ("liquidity", symbol.upper(), current.isoformat())
        cached = self._cache.get(key, self._mono())
        if cached is not None:
            return None if isinstance(cached, DataError) else cached
        end = datetime.combine(current, datetime.min.time(), NY)
        try:
            raw = self.source.fetch_bars(symbol.upper(), "1Day", end - timedelta(days=45), end,
                                         feed="sip")
        except DataError as exc:
            log.warning("Consolidated volume unavailable for %s: %s", symbol, exc)
            self._cache.put(key, exc, 600.0, self._mono())
            return None
        recent = [b for b in raw if ny_date(b.time) < current][-self.LIQUIDITY_SESSIONS:]
        if len(recent) < self.LIQUIDITY_SESSIONS // 2:
            self._cache.put(key, DataError("too few consolidated sessions"), 600.0, self._mono())
            return None
        out = {"adv": sum(b.close * b.volume for b in recent) / len(recent),
               "sessions": len(recent), "basis": "consolidated volume (SIP)",
               "feed": "sip", "feed_label": "SIP (consolidated, historical daily bars)",
               "first_session": ny_date(recent[0].time).isoformat(),
               "last_session": ny_date(recent[-1].time).isoformat(),
               "excludes_session": current.isoformat(),
               "fetched_at": now.astimezone(NY).isoformat(timespec="seconds")}
        self._cache.put(key, out, 6 * 3600.0, self._mono())
        return out

    SIP_EMBARGO_SECONDS = 15 * 60 + 60  # free plan: SIP older than 15 min (+ 1 min grace)
    OVERLAY_STEP = {"5Min": 300, "15Min": 900, "1Day": 3600, "1Week": 3600}

    @property
    def volume_scope(self) -> str:
        """What the feed's volume represents: demo, partial (IEX only) or consolidated."""
        if self.source.is_demo:
            return "demo"
        return "partial" if self.feed.bars_feed == "iex" else "consolidated"

    def volume_overlay(self, symbol: str, tf: str, bars: list[Bar],
                       until: datetime | None = None):
        """Consolidated (SIP) volume for `bars` (IEX candles of `tf`) wherever Alpaca's
        free plan provides it: candles ending at least 16 minutes ago (and no later than
        `until`, for point-in-time replays). Returns a strategies.volume.VolumeOverlay, or
        None when not applicable (consolidated/demo feed) or unavailable (the caller then
        labels relative volume LIMITED/UNVERIFIED instead of treating IEX as the market).

        Coverage is floored to the timeframe's step so results change only when a new
        candle becomes covered (strategy caches stay valid between candles)."""
        from ..strategies.volume import VolumeOverlay
        step = self.OVERLAY_STEP.get(tf)
        if self.volume_scope != "partial" or step is None or not bars:
            return None
        limit = self.now() - timedelta(seconds=self.SIP_EMBARGO_SECONDS)
        if until is not None:
            limit = min(limit, until)
        covered = int(limit.timestamp()) // step * step
        first = min(b.time for b in bars)
        start = datetime.fromtimestamp(first, NY).replace(hour=0, minute=0, second=0)
        if first >= covered:
            return VolumeOverlay({}, covered)
        key = ("volume_overlay", symbol.upper(), tf, int(start.timestamp()), covered)
        cached = self._cache.get(key, self._mono())
        if cached is not None:
            return None if isinstance(cached, DataError) else cached
        try:
            raw = self.source.fetch_bars(symbol.upper(), spec(tf).fetch, start,
                                         datetime.fromtimestamp(covered, timezone.utc),
                                         feed="sip")
        except DataError as exc:
            log.warning("Consolidated %s volume unavailable for %s: %s", tf, symbol, exc)
            self._cache.put(key, exc, 600.0, self._mono())
            return None
        out = VolumeOverlay({b.time: b.volume for b in raw}, covered)
        self._cache.put(key, out, max(float(step), 300.0) * 2, self._mono())
        return out

    def _build(self, symbol: str, tf: TimeframeSpec, session: str, raw: list[Bar],
               now: datetime, cal: MarketCalendar, limit: int | None) -> CandleSet:
        cutoff_ts = int(self.cutoff(now).timestamp())
        align = tf.fetch_minutes * 60 if tf.intraday else None
        clean, quality = validate_bars(raw, align_seconds=align, now=int(now.timestamp()))
        if tf.intraday:
            clean = filter_session(clean, cal, session)
            def bounds(ts: int) -> tuple[int, int] | None:
                sd = cal.day(ny_date(ts))
                return (int(sd.bounds(session)[0].timestamp()),
                        int(sd.bounds(session)[1].timestamp())) if sd else None

            gaps = count_intraday_gaps(clean, tf.fetch_minutes * 60, bounds)
            if gaps:
                quality.info.append(f"{gaps} {tf.fetch} interval(s) with no trades on this feed")
            bars = aggregate(clean, tf, cal, session)
        else:
            bars = clean
        if limit is not None:
            bars = bars[-limit:]
        complete = [bar_end(b, tf, cal, session) + COMPLETION_GRACE_SECONDS <= cutoff_ts
                    for b in bars]
        status = self._status(tf, session, bars, complete, now, cal)
        return CandleSet(symbol, tf.key, session, bars, complete, status, quality)

    def _status(self, tf: TimeframeSpec, session: str, bars: list[Bar], complete: list[bool],
                now: datetime, cal: MarketCalendar) -> dict:
        cutoff = self.cutoff(now)
        base = {
            "source": self.source.name,
            "feed": self.feed.id,
            "feed_label": self.feed.label,
            "coverage": self.feed.coverage,
            "latency": "unavailable",
            "freshness": "unavailable",
            "reason": None,
            "as_of": now.astimezone(NY).isoformat(),
            "data_cutoff": cutoff.astimezone(NY).isoformat(),
            "latest_bar_time": _iso(bars[-1].time) if bars else None,
            "latest_bar_complete": bool(complete and complete[-1]),
        }
        if not bars:
            base["reason"] = "The provider returned no candles for this request."
            return base
        if self.source.is_demo:
            base.update(latency="demo", freshness="demo",
                        reason="Simulated DEMO DATA, not real market prices.")
            return base

        last = bars[-1]
        if tf.intraday:
            sd = cal.last_session_started(cutoff, session)
            s_start, s_end = (int(x.timestamp()) for x in sd.bounds(session))
            in_session = s_start <= cutoff.timestamp() < s_end
            extended_phase = not (sd.open.timestamp() <= cutoff.timestamp() < sd.close.timestamp())
            slack = max(3 * tf.minutes * 60, 1800 if extended_phase else 300)
            if in_session:
                base["latency"] = self.feed.latency
                age = cutoff.timestamp() - last.time
                if age > slack + tf.minutes * 60:
                    base.update(freshness="stale", reason=(
                        f"Latest {tf.label} candle is {age / 60:.0f} min old during an open "
                        "session; the feed may be lagging or the symbol isn't trading."))
                    return base
            else:
                base["latency"] = "historical"
                if bar_end(last, tf, cal, session) < s_end - slack:
                    base.update(freshness="stale", reason=(
                        f"Data ends {_iso(last.time)}, before the end of the last session "
                        f"({sd.day.isoformat()})."))
                    return base
        else:
            in_regular = cal.status(cutoff)["phase"] == "regular"
            base["latency"] = self.feed.latency if in_regular else "historical"
            expected = cal.last_completed_session(cutoff).day
            last_day = ny_date(last.time)
            lag_days = (expected - last_day).days
            if (tf.key == "1Day" and last_day < expected) or (tf.key == "1Week" and lag_days > 6):
                base.update(freshness="stale", reason=(
                    f"Latest {tf.label.lower()} candle is from {last_day.isoformat()}, but the "
                    f"last completed session was {expected.isoformat()}."))
                return base
        base["freshness"] = "fresh"
        return base

    # ------------------------------------------------------------------------- quotes

    def get_quotes(self) -> list[dict]:
        """Latest price, change vs prior close and session volume for every symbol."""
        now = self.now()
        cal = self.calendar
        phase = cal.status(now)["phase"]
        key = ("quotes", tuple(self.symbols))
        cached = self._cache.get(key, self._mono())
        if isinstance(cached, DataError):
            raise cached
        if cached is not None:
            return cached
        try:
            snaps = self.source.fetch_snapshots(self.symbols)
        except DataError as exc:
            self._cache.put(key, exc, 10.0, self._mono())
            raise
        cutoff = self.cutoff(now)
        current_day = cal.last_session_started(cutoff, "extended").day
        quotes = [self._quote(sym, snaps.get(sym), current_day, now, phase) for sym in self.symbols]
        ttl = 3600.0 if self.source.is_demo else (5.0 if phase != "closed" else 60.0)
        self._cache.put(key, quotes, ttl, self._mono())
        return quotes

    def _quote(self, symbol: str, snap: dict | None, current_day: date, now: datetime,
               phase: str) -> dict:
        out = {"symbol": symbol, "available": False, "price": None, "prev_close": None,
               "change": None, "change_pct": None, "volume": None, "price_time": None,
               "day_high": None, "day_low": None,
               "session_date": current_day.isoformat(), "stale": False, "reason": None,
               "latency": "demo" if self.source.is_demo else self.feed.latency}
        if not snap:
            out["reason"] = "No snapshot returned for this symbol on the configured feed."
            return out
        try:
            trade = snap.get("latestTrade") or {}
            daily = snap.get("dailyBar") or {}
            prev = snap.get("prevDailyBar") or {}
            price = float(trade["p"]) if trade.get("p") else None
            price_ts = parse_time(trade["t"]) if trade.get("t") else None
            daily_day = ny_date(parse_time(daily["t"])) if daily.get("t") else None
            if daily_day == current_day:
                # Today's daily bar exists: compare with the previous session's close.
                ref_close = float(prev["c"]) if prev.get("c") else None
                volume = float(daily.get("v") or 0)
                if daily.get("h") and daily.get("l"):
                    out["day_high"], out["day_low"] = float(daily["h"]), float(daily["l"])
            else:
                # No bar for today yet (e.g. early pre-market): the latest daily bar *is*
                # the prior session, so compare with its close; no volume today yet.
                ref_close = float(daily["c"]) if daily.get("c") else None
                volume = 0.0
        except (KeyError, TypeError, ValueError):
            out["reason"] = "Malformed snapshot from provider."
            return out
        if price is None or price <= 0 or ref_close is None or ref_close <= 0:
            out["reason"] = "No valid last trade available."
            return out
        change = price - ref_close
        out.update(available=True, price=round(price, 4), prev_close=round(ref_close, 4),
                   change=round(change, 4), change_pct=round(change / ref_close * 100, 2),
                   volume=volume, price_time=_iso(price_ts))
        if self.source.is_demo:
            return out
        age = self.cutoff(now).timestamp() - (price_ts or 0)
        if phase == "regular" and age > 15 * 60:
            out["stale"] = True
            out["reason"] = f"Last trade is {age / 60:.0f} min old during regular hours."
        if phase == "closed":
            out["latency"] = "historical"
        return out
