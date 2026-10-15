"""An IEX-like source for parity tests: demo prices and IEX volume, plus a different
consolidated (SIP) volume series - so relative volume read on IEX and on SIP disagree, as
on real data - served through the same interface as AlpacaSource (feed="sip")."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime

from app.data.demo import DemoSource
from app.market.sources import FEEDS


def sip_volume(t: int, v: float) -> float:
    return v * 30.0 * (1.4 + math.sin(t / 5400.0) * 0.9)  # 0.5x .. 2.3x of a 30x tape


class IexLikeSource(DemoSource):
    name = "alpaca"
    is_demo = False
    feed = FEEDS["iex"]
    adjustment = "split"

    def __init__(self, *a, delayed: bool = False, **k) -> None:
        super().__init__(*a, **k)
        if delayed:
            self.feed = FEEDS["delayed_sip"]
        self.sip_requests: list[tuple[str, str, datetime, datetime]] = []

    def fetch_bars(self, symbol, timeframe, start, end, max_items=50_000, feed=None):
        bars = super().fetch_bars(symbol, timeframe, start, end, max_items)
        if feed == "sip" or (feed is None and self.feed.bars_feed == "sip"):
            if feed == "sip":
                self.sip_requests.append((symbol, timeframe, start, end))
            return [replace(b, volume=sip_volume(b.time, b.volume)) for b in bars]
        return bars
