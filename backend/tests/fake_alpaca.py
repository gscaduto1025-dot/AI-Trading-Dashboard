"""An in-process fake of the Alpaca endpoints TradeScope uses, for httpx.MockTransport.

It mimics the documented response shapes:
  GET /v2/stocks/bars       -> {"bars": {SYM: [{t,o,h,l,c,v,n,vw}]}, "next_page_token": ...}
  GET /v2/stocks/snapshots  -> {SYM: {latestTrade, latestQuote, minuteBar, dailyBar, prevDailyBar}}
  GET /v2/calendar          -> [{date, open, close, session_open, session_close}]
Bars cover 04:00-20:00 ET on trading days (extended hours included, like Alpaca).
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone

import httpx

from app.market.calendar import NY, MarketCalendar

STEP = {"1Min": 60, "5Min": 300, "15Min": 900, "30Min": 1800}


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def price_at(ts: int) -> float:
    return round(100 + 5 * math.sin(ts / 20000) + 0.5 * math.sin(ts / 900), 4)


class FakeAlpaca:
    def __init__(self, now: datetime, *, key_id: str = "PKTESTKEY", secret: str = "SECRETVALUE",
                 data_until: datetime | None = None, sip_allowed: bool = True) -> None:
        self.now = now
        self.key_id, self.secret = key_id, secret
        self.data_until = data_until  # simulate a lagging/stalled feed
        self.sip_allowed = sip_allowed
        self.calendar = MarketCalendar()
        self.requests: list[httpx.Request] = []
        self.fail_next: list[httpx.Response] = []  # queued responses for /v2/stocks/* calls
        self.corrupt_at: int | None = None  # make one bar's high < low
        self.page_size: int = 10_000  # server-side cap per page
        self.daily_bar_from_regular_open = False  # dailyBar for today appears only at 09:30

    # ---------------------------------------------------------------- transport

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_next and request.url.path.startswith("/v2/stocks/"):
            return self.fail_next.pop(0)
        if (request.headers.get("APCA-API-KEY-ID") != self.key_id
                or request.headers.get("APCA-API-SECRET-KEY") != self.secret):
            return httpx.Response(401, json={"message": "unauthorized."})
        path = request.url.path
        q = dict(request.url.params)
        if path == "/v2/stocks/bars":
            return self._bars(q)
        if path == "/v2/stocks/snapshots":
            return self._snapshots(q)
        if path == "/v2/calendar":
            return self._calendar(q)
        return httpx.Response(404, json={"message": "not found"})

    # -------------------------------------------------------------------- data

    def _bar(self, ts: int, step: int, volume: float = 1000.0) -> dict:
        o, c = price_at(ts), price_at(ts + step)
        h, l = max(o, c) + 0.05, min(o, c) - 0.05
        if self.corrupt_at == ts:
            h, l = l - 1, h + 1
        return {"t": iso(ts), "o": o, "h": round(h, 4), "l": round(l, 4), "c": c, "v": volume,
                "n": 10, "vw": round((o + c) / 2, 4)}

    def series(self, timeframe: str, start: datetime, end: datetime) -> list[dict]:
        end_ts = end.timestamp()
        if self.data_until is not None:
            end_ts = min(end_ts, self.data_until.timestamp())
        out = []
        d = start.astimezone(NY).date()
        last_day = datetime.fromtimestamp(end_ts, NY).date()
        if timeframe in STEP:
            step = STEP[timeframe]
            while d <= last_day:
                sd = self.calendar.day(d)
                if sd:
                    ts = int(sd.session_open.timestamp())
                    while ts < sd.session_close.timestamp():
                        if start.timestamp() <= ts <= end_ts:
                            out.append(self._bar(ts, step))
                        ts += step
                d += timedelta(days=1)
        elif timeframe == "1Day":
            while d <= last_day:
                sd = self.calendar.day(d)
                if sd:
                    ts = int(datetime.combine(d, time(0), NY).timestamp())
                    if start.timestamp() <= ts <= end_ts:
                        out.append(self._daily(sd))
                d += timedelta(days=1)
        elif timeframe == "1Week":
            d -= timedelta(days=d.weekday())
            while d <= last_day:
                days = self.calendar.trading_days(d, d + timedelta(days=4))
                ts = int(datetime.combine(d, time(0), NY).timestamp())
                if days and start.timestamp() <= ts + 5 * 86400 and ts <= end_ts:
                    first, last = self._daily(days[0]), self._daily(days[-1])
                    out.append({"t": iso(ts), "o": first["o"], "h": max(first["h"], last["h"]) + 1,
                                "l": min(first["l"], last["l"]) - 1, "c": last["c"], "v": 5e6})
                d += timedelta(days=7)
        return out

    def _daily(self, sd) -> dict:
        o, c = price_at(int(sd.open.timestamp())), price_at(int(sd.close.timestamp()))
        return {"t": iso(int(datetime.combine(sd.day, time(0), NY).timestamp())), "o": o,
                "h": round(max(o, c) + 1, 4), "l": round(min(o, c) - 1, 4), "c": c, "v": 1e6}

    def _bars(self, q: dict) -> httpx.Response:
        start = datetime.fromisoformat(q["start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(q["end"].replace("Z", "+00:00"))
        if q.get("feed") == "sip" and not self.sip_allowed and end > self.now - timedelta(minutes=15):
            return httpx.Response(403, json={"message": "subscription does not permit querying "
                                                        "recent SIP data"})
        symbol = q["symbols"]
        bars = self.series(q["timeframe"], start, end)
        offset = int(q.get("page_token") or 0)
        limit = min(int(q.get("limit") or 1000), self.page_size)
        page = bars[offset:offset + limit]
        token = str(offset + limit) if offset + limit < len(bars) else None
        return httpx.Response(200, json={"bars": {symbol: page} if page else {},
                                         "next_page_token": token})

    def _snapshots(self, q: dict) -> httpx.Response:
        out = {}
        today = self.now.astimezone(NY).date()
        sd = self.calendar.day(today)
        trade_ts = int(self.now.timestamp()) - 3
        for sym in q["symbols"].split(","):
            prev_day = self.calendar.day(self.calendar.previous_trading_day(today))
            starts = sd.open if self.daily_bar_from_regular_open else (sd.session_open if sd else None)
            daily_sd = sd if sd and self.now >= starts else prev_day
            before = self.calendar.day(self.calendar.previous_trading_day(daily_sd.day))
            out[sym] = {
                "latestTrade": {"p": price_at(trade_ts), "t": iso(trade_ts), "x": "V", "s": 100},
                "dailyBar": self._daily(daily_sd),
                "prevDailyBar": self._daily(before),
            }
        return httpx.Response(200, json=out)

    def _calendar(self, q: dict) -> httpx.Response:
        rows = []
        for sd in self.calendar.trading_days(date.fromisoformat(q["start"]),
                                             date.fromisoformat(q["end"])):
            rows.append({"date": sd.day.isoformat(), "open": sd.open.strftime("%H:%M"),
                         "close": sd.close.strftime("%H:%M"),
                         "session_open": sd.session_open.strftime("%H%M"),
                         "session_close": sd.session_close.strftime("%H%M")})
        return httpx.Response(200, json=rows)
