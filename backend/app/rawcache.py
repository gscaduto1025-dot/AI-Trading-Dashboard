"""Warm restarts and faster cold starts for the market-data raw store, from OUTSIDE the
frozen market-data code (app/market is part of the frozen rule surface and is not edited).

1. persist/restore: the provider bars the market-data service downloaded (its raw store)
   are saved to disk (gzip JSON) and restored at the next start with their ages. A restored
   series is then indistinguishable from one the service fetched that many seconds ago,
   so the service's OWN rules decide what to re-download: an incremental refresh with
   its overlap once the TTL passes, and a full re-fetch once a series is 6 hours old
   (RAW_FULL_REFRESH_SECONDS). Series past that age are not restored at all. The data are
   exactly what was downloaded; nothing is synthesised.
2. warm_up: fetch the candles every engine needs for many symbols in parallel threads
   (the shared rate limiter still caps the request rate). It only calls the service's
   public get_candles(), the same path the engines use.

Only real provider data is cached; demo sources are never persisted.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from .data.base import Bar, DataError

log = logging.getLogger("tradescope.rawcache")
FORMAT = 1
_SAVE_LOCKS: dict[str, threading.Lock] = {}
_SAVE_LOCKS_GUARD = threading.Lock()


def cache_dir() -> Path:
    return Path(os.environ.get("RAW_CACHE_DIR",
                               Path(__file__).resolve().parents[1] / "data" / "cache"))


def enabled() -> bool:
    return os.environ.get("RAW_CACHE_PERSIST", "true").lower() == "true"


def _identity(market) -> dict:
    feed = market.source.feed
    return {"source": market.source.name, "feed": getattr(feed, "label", str(feed)),
            "adjustment": getattr(market.source, "adjustment", None)}


def save(market, path: Path) -> dict:
    """Write the raw store to `path` atomically. Returns {series, bars, bytes}."""
    if market.source.is_demo:
        return {"series": 0, "bars": 0, "bytes": 0, "skipped": "demo source"}
    mono, wall = market._mono(), time.time()
    with market._lock:
        items = list(market._raw.items())
    series = []
    nbars = 0
    for (symbol, tf), ent in items:
        bars = list(ent["bars"])
        nbars += len(bars)
        series.append({"symbol": symbol, "tf": tf, "start": ent["start"].isoformat(),
                       "cutoff": ent["cutoff"].isoformat(),
                       "fetched_age": mono - ent["fetched"], "full_age": mono - ent["full_at"],
                       "bars": [[b.time, b.open, b.high, b.low, b.close, b.volume]
                                for b in bars]})
    doc = {"format": FORMAT, "saved_at": wall, "identity": _identity(market), "series": series}
    path.parent.mkdir(parents=True, exist_ok=True)
    # One writer per file at a time, each with its own temp name: concurrent saves (the
    # periodic save, a universe run and shutdown) used to share "<name>.tmp", so one rename
    # failed (ENOENT) and two writers could interleave in the same temp file.
    with _SAVE_LOCKS_GUARD:
        lock = _SAVE_LOCKS.setdefault(str(path), threading.Lock())
    with lock:
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with gzip.open(tmp, "wt") as f:
                json.dump(doc, f, separators=(",", ":"))
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        return {"series": len(series), "bars": nbars, "bytes": path.stat().st_size}


def restore(market, path: Path) -> dict:
    """Load series saved by save() into an EMPTY slot of the raw store (never overwriting a
    series already fetched in this process). Returns {restored, skipped_stale, ...}."""
    out = {"restored": 0, "skipped_stale": 0, "bars": 0}
    if market.source.is_demo or not path.exists():
        return out | {"reason": "demo source" if market.source.is_demo else "no cache file"}
    try:
        with gzip.open(path, "rt") as f:
            doc = json.load(f)
    except (OSError, ValueError) as exc:
        log.warning("Raw cache %s unreadable (%s); ignored", path, exc)
        return out | {"reason": f"unreadable: {exc}"}
    if doc.get("format") != FORMAT or doc.get("identity") != _identity(market):
        return out | {"reason": "different data source, feed or adjustment"}
    elapsed = max(0.0, time.time() - doc["saved_at"])
    mono = market._mono()
    limit = market.RAW_FULL_REFRESH_SECONDS
    with market._lock:
        for s in doc["series"]:
            full_age = s["full_age"] + elapsed
            if full_age >= limit:
                out["skipped_stale"] += 1
                continue
            k = (s["symbol"], s["tf"])
            if k in market._raw:
                continue
            bars = [Bar(*b) for b in s["bars"]]
            market._raw[k] = {"start": datetime.fromisoformat(s["start"]),
                              "cutoff": datetime.fromisoformat(s["cutoff"]), "bars": bars,
                              "fetched": mono - (s["fetched_age"] + elapsed),
                              "full_at": mono - full_age}
            out["restored"] += 1
            out["bars"] += len(bars)
    return out | {"age_seconds": round(elapsed)}


def warm_up(market, symbols: list[str], frames: dict[str, int], workers: int = 4) -> dict:
    """Fetch every (symbol, timeframe) the engines need, `workers` at a time."""
    t0 = time.monotonic()
    jobs = [(s, tf, n) for s in symbols for tf, n in frames.items()]
    errors: list[str] = []

    def one(job):
        s, tf, n = job
        try:
            market.get_candles(s, tf, "regular", n + 1)
        except DataError as exc:
            errors.append(f"{s} {tf}: {exc}")

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(one, jobs))
    return {"symbols": len(symbols), "series": len(jobs), "errors": errors,
            "seconds": round(time.monotonic() - t0, 1)}
