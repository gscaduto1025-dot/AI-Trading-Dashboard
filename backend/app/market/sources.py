"""Raw data sources behind MarketDataService: Alpaca (real) and Demo (simulated).

Sources only fetch; session filtering, aggregation, validation, completion and freshness
are applied uniformly by MarketDataService.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone

from ..data.base import Bar, DataError, InvalidRequest
from .alpaca_client import AlpacaClient


@dataclass(frozen=True)
class FeedInfo:
    id: str  # what the user configured: iex | sip | delayed_sip | demo
    label: str
    bars_feed: str  # `feed` parameter used for historical bars
    snapshot_feed: str  # `feed` parameter used for snapshots
    delay_seconds: int
    latency: str  # real-time | delayed | demo
    coverage: str

    def to_dict(self) -> dict:
        return asdict(self)


FEEDS: dict[str, FeedInfo] = {
    "iex": FeedInfo(
        "iex", "IEX", "iex", "iex", 0, "real-time",
        "Investors Exchange only (one venue). Prices are real IEX trades in real time; volume "
        "covers IEX only, a small share of consolidated volume. Included in Alpaca's free plan."),
    "sip": FeedInfo(
        "sip", "SIP", "sip", "sip", 0, "real-time",
        "All US exchanges (consolidated tape). Real-time SIP requires Alpaca's Algo Trader Plus "
        "subscription; without it, recent requests are rejected."),
    "delayed_sip": FeedInfo(
        "delayed_sip", "SIP (15-min delayed)", "sip", "delayed_sip", 15 * 60, "delayed",
        "All US exchanges, delayed 15 minutes. Works on the free plan, which cannot query the "
        "most recent 15 minutes of SIP data."),
}
DEMO_FEED = FeedInfo("demo", "DEMO", "demo", "demo", 0, "demo",
                     "Simulated prices generated locally. Not market data.")


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


class AlpacaSource:
    name = "alpaca"
    is_demo = False

    def __init__(self, client: AlpacaClient, feed: str = "iex", adjustment: str = "split") -> None:
        if feed not in FEEDS:
            raise ValueError(f"ALPACA_FEED must be one of {list(FEEDS)}, got {feed!r}")
        if adjustment not in ("raw", "split", "dividend", "all"):
            raise ValueError("ALPACA_ADJUSTMENT must be raw, split, dividend or all")
        self.client = client
        self.feed = FEEDS[feed]
        self.adjustment = adjustment

    def fetch_bars(self, symbol: str, timeframe: str, start: datetime, end: datetime,
                   max_items: int = 50_000, feed: str | None = None) -> list[Bar]:
        """`feed` overrides the configured bars feed (used for consolidated SIP history,
        which the free plan allows for data older than 15 minutes)."""
        params = {
            "symbols": symbol,
            "timeframe": timeframe,
            "start": _rfc3339(start),
            "end": _rfc3339(end),
            "adjustment": self.adjustment,
            "feed": feed or self.feed.bars_feed,
            "sort": "asc",
        }
        bars: list[Bar] = []
        while len(bars) < max_items:
            params["limit"] = min(10_000, max_items - len(bars))
            page = self.client.get_data("/v2/stocks/bars", params)
            raw = (page.get("bars") or {}).get(symbol) or []
            for r in raw:
                try:
                    bars.append(Bar(parse_time(r["t"]), float(r["o"]), float(r["h"]),
                                    float(r["l"]), float(r["c"]), float(r["v"])))
                except (KeyError, TypeError, ValueError) as exc:
                    raise DataError(f"Malformed bar from Alpaca for {symbol}: {exc}") from exc
            token = page.get("next_page_token")
            if not token or not raw:
                break
            params["page_token"] = token
        return bars

    def fetch_snapshots(self, symbols: list[str]) -> dict[str, dict]:
        data = self.client.get_data("/v2/stocks/snapshots",
                                    {"symbols": ",".join(symbols), "feed": self.feed.snapshot_feed})
        if not isinstance(data, dict):
            raise DataError("Unexpected snapshot response from Alpaca")
        return data

    def fetch_calendar(self, start: date, end: date) -> list[dict]:
        rows = self.client.get_trading("/v2/calendar",
                                       {"start": start.isoformat(), "end": end.isoformat()})
        if not isinstance(rows, list):
            raise InvalidRequest("Unexpected calendar response from Alpaca")
        return rows
