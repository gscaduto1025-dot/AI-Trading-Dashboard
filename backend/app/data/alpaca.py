"""Alpaca Market Data API v2 provider (read-only market data; no trading endpoints)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from .base import Bar, DataError, Quote, validate_timeframe

NY = ZoneInfo("America/New_York")

# How far back to request so `limit` regular-session bars are available.
_LOOKBACK = {
    "1Min": timedelta(days=5),
    "5Min": timedelta(days=15),
    "15Min": timedelta(days=40),
    "1Hour": timedelta(days=120),
    "1Day": timedelta(days=600),
}


def _parse_time(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def _in_regular_session(ts: int, timeframe: str) -> bool:
    """Drop pre/post-market bars for intraday timeframes (VWAP and levels assume RTH)."""
    if timeframe == "1Day":
        return True
    t = datetime.fromtimestamp(ts, NY)
    minute = t.hour * 60 + t.minute
    if timeframe == "1Hour":
        return 9 * 60 <= minute < 16 * 60
    return 9 * 60 + 30 <= minute < 16 * 60


class AlpacaDataProvider:
    name = "alpaca"
    is_demo = False

    def __init__(self, key_id: str, secret_key: str, base_url: str = "https://data.alpaca.markets",
                 feed: str = "iex", client: httpx.Client | None = None) -> None:
        self._feed = feed
        self._client = client or httpx.Client(
            base_url=base_url,
            headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key},
            timeout=15.0,
        )

    def _get(self, path: str, params: dict) -> dict:
        try:
            resp = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise DataError(f"Alpaca request failed: {exc}") from exc
        if resp.status_code != 200:
            raise DataError(f"Alpaca returned {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def get_bars(self, symbol: str, timeframe: str, limit: int = 300) -> list[Bar]:
        validate_timeframe(timeframe)
        start = datetime.now(timezone.utc) - _LOOKBACK[timeframe]
        params = {
            "timeframe": timeframe,
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 10000,
            "feed": self._feed,
            "adjustment": "split",
            "sort": "desc",
        }
        data = self._get(f"/v2/stocks/{symbol.upper()}/bars", params)
        bars = []
        for raw in data.get("bars") or []:
            ts = _parse_time(raw["t"])
            if _in_regular_session(ts, timeframe):
                bars.append(Bar(ts, float(raw["o"]), float(raw["h"]), float(raw["l"]),
                                float(raw["c"]), float(raw["v"])))
            if len(bars) >= limit:
                break
        if not bars:
            raise DataError(f"No bars returned for {symbol} {timeframe}")
        bars.reverse()
        return bars

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        data = self._get("/v2/stocks/snapshots",
                         {"symbols": ",".join(s.upper() for s in symbols), "feed": self._feed})
        quotes = []
        for sym in symbols:
            snap = data.get(sym.upper())
            if not snap:
                continue
            trade = snap.get("latestTrade") or {}
            daily = snap.get("dailyBar") or {}
            prev = snap.get("prevDailyBar") or {}
            price = float(trade.get("p") or daily.get("c") or 0)
            prev_close = float(prev.get("c") or price)
            change = price - prev_close
            as_of = _parse_time(trade["t"]) if trade.get("t") else int(datetime.now().timestamp())
            quotes.append(Quote(sym.upper(), round(price, 2), round(prev_close, 2), round(change, 2),
                                round(change / prev_close * 100, 2) if prev_close else 0.0, as_of))
        return quotes
