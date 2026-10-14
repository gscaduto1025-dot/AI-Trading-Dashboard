"""Alpaca options data: the contract master list and the option-chain snapshots.

Endpoints (Alpaca, checked against the official alpaca-py SDK):
- GET {trading}/v2/options/contracts: contracts with type, strike, expiration, style,
  multiplier, tradable flag, **open interest** and its date. Paginated
  (`limit`, `page_token` -> `next_page_token`).
- GET {data}/v1beta1/options/snapshots/{underlying}: per contract `latestQuote`
  {t, bp, bs, bx, ap, as, ax, c}, `latestTrade` {t, p, s, x, c}, `dailyBar`
  {t, o, h, l, c, v, n, vw}, `prevDailyBar`, `minuteBar`, `impliedVolatility` and `greeks`
  {delta, gamma, theta, vega, rho}. Filters: `feed`, `type`, `strike_price_gte/lte`,
  `expiration_date[_gte/_lte]`; up to 1000 per page.

Feeds (`feed` parameter, ALPACA_OPTIONS_FEED):
- `indicative`: free on every Alpaca account. Quotes are an indicative derivative of
  OPRA, not actual OPRA quotes, and trades are delayed 15 minutes. Usable for exploring
  contracts, never for a recommendation.
- `opra`: the real-time consolidated OPRA feed. Needs Alpaca's Algo Trader Plus
  subscription and a signed OPRA agreement; otherwise Alpaca answers 403.

Nothing is filled in or estimated here. A field Alpaca does not send stays None.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime

from ..data.base import DataError, InvalidRequest
from ..market.alpaca_client import AlpacaClient


@dataclass(frozen=True)
class OptionsFeed:
    id: str
    label: str
    real_time: bool
    actual_quotes: bool          # True only for real OPRA quotes
    trade_delay_seconds: int
    requirement: str
    coverage: str

    def to_dict(self) -> dict:
        return asdict(self)


OPTIONS_FEEDS = {
    "indicative": OptionsFeed(
        "indicative", "Indicative (free)", False, False, 15 * 60,
        "Included with every Alpaca account (Basic plan).",
        "Quotes are an indicative derivative of the OPRA feed, not actual OPRA quotes; "
        "trades are delayed 15 minutes. Suitable for exploring contracts, not for judging "
        "executable prices."),
    "opra": OptionsFeed(
        "opra", "OPRA (real-time)", True, True, 0,
        "Requires Alpaca's Algo Trader Plus subscription and a signed OPRA agreement.",
        "Real-time consolidated quotes and trades from all US options exchanges."),
}

OCC_RE = re.compile(r"^(?P<root>[A-Z0-9.]{1,6})(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")
CHAIN_PAGE, CHAIN_MAX_PAGES = 1000, 10
CONTRACT_PAGE, CONTRACT_MAX_PAGES = 1000, 20


def parse_occ(symbol: str) -> dict | None:
    """OCC option symbol -> root, expiration, type, strike (e.g. AAPL250117C00200000)."""
    m = OCC_RE.match(symbol or "")
    if not m:
        return None
    ymd = m["ymd"]
    try:
        exp = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:]))
    except ValueError:
        return None
    return {"root": m["root"], "expiration": exp.isoformat(),
            "type": "call" if m["cp"] == "C" else "put", "strike": int(m["strike"]) / 1000}


def _num(v) -> float | None:
    """A finite float, or None. Never substitutes a default."""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _ts(v) -> int | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def parse_snapshot(symbol: str, raw: dict) -> dict:
    q = raw.get("latestQuote") or {}
    t = raw.get("latestTrade") or {}
    bar = raw.get("dailyBar") or {}
    prev = raw.get("prevDailyBar") or {}
    g = raw.get("greeks")
    greeks = None
    if isinstance(g, dict):
        greeks = {k: _num(g.get(k)) for k in ("delta", "gamma", "theta", "vega", "rho")}
    return {
        "symbol": symbol,
        "bid": _num(q.get("bp")), "ask": _num(q.get("ap")),
        "bid_size": _num(q.get("bs")), "ask_size": _num(q.get("as")),
        "quote_time": _ts(q.get("t")),
        "last": _num(t.get("p")), "last_size": _num(t.get("s")), "last_time": _ts(t.get("t")),
        "volume": _num(bar.get("v")), "volume_time": _ts(bar.get("t")),
        "prev_volume": _num(prev.get("v")), "prev_volume_time": _ts(prev.get("t")),
        "iv": _num(raw.get("impliedVolatility")),
        "greeks": greeks,
    }


def parse_contract(raw: dict) -> dict | None:
    sym = raw.get("symbol")
    occ = parse_occ(sym) if sym else None
    strike = _num(raw.get("strike_price"))
    exp = raw.get("expiration_date") or (occ or {}).get("expiration")
    typ = raw.get("type") or (occ or {}).get("type")
    if not sym or strike is None or not exp or typ not in ("call", "put"):
        return None
    oi = _num(raw.get("open_interest"))
    size = _num(raw.get("size"))
    return {
        "symbol": sym, "type": typ, "strike": strike, "expiration": exp,
        "style": raw.get("style"), "multiplier": int(size) if size else None,
        "tradable": raw.get("tradable"), "status": raw.get("status"),
        "open_interest": int(oi) if oi is not None else None,
        "open_interest_date": raw.get("open_interest_date"),
        "close_price": _num(raw.get("close_price")),
        "close_price_date": raw.get("close_price_date"),
    }


class AlpacaOptionsSource:
    """Fetches and caches Alpaca options data. Chain snapshots are cached briefly (quotes
    move); the contract list, whose open interest updates once a day, for longer."""

    name = "alpaca"

    def __init__(self, client: AlpacaClient, feed: str = "indicative", *,
                 chain_ttl: float = 20.0, contracts_ttl: float = 900.0,
                 clock=time.monotonic) -> None:
        if feed not in OPTIONS_FEEDS:
            raise ValueError(f"ALPACA_OPTIONS_FEED must be one of {list(OPTIONS_FEEDS)}, "
                             f"got {feed!r}")
        self.client = client
        self.feed = OPTIONS_FEEDS[feed]
        self.chain_ttl, self.contracts_ttl = chain_ttl, contracts_ttl
        self._clock = clock
        self._cache: dict[tuple, tuple[float, object]] = {}

    def _cached(self, key: tuple, ttl: float, fetch):
        now = self._clock()
        hit = self._cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        value = fetch()
        self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
        self._cache[key] = (now + ttl, value)
        return value

    def contracts(self, underlying: str, exp_gte: date, exp_lte: date, kind: str | None = None,
                  strike_gte: float | None = None, strike_lte: float | None = None) -> dict:
        """{"contracts": [...], "truncated": bool}. Active contracts only."""
        params = {"underlying_symbols": underlying, "status": "active",
                  "expiration_date_gte": exp_gte.isoformat(),
                  "expiration_date_lte": exp_lte.isoformat(), "limit": CONTRACT_PAGE}
        if kind:
            params["type"] = kind
        if strike_gte is not None:
            params["strike_price_gte"] = f"{strike_gte:.2f}"
        if strike_lte is not None:
            params["strike_price_lte"] = f"{strike_lte:.2f}"

        def fetch():
            out, truncated, p = [], False, dict(params)
            for page in range(CONTRACT_MAX_PAGES):
                body = self.client.get_trading("/v2/options/contracts", p)
                if not isinstance(body, dict):
                    raise DataError("Unexpected option contracts response from Alpaca")
                for raw in body.get("option_contracts") or []:
                    c = parse_contract(raw)
                    if c:
                        out.append(c)
                token = body.get("next_page_token")
                if not token:
                    break
                p["page_token"] = token
                truncated = page == CONTRACT_MAX_PAGES - 1
            return {"contracts": out, "truncated": truncated}

        return self._cached(("contracts", tuple(sorted(params.items()))),
                            self.contracts_ttl, fetch)

    def chain(self, underlying: str, exp_gte: date, exp_lte: date, kind: str | None = None,
              strike_gte: float | None = None, strike_lte: float | None = None) -> dict:
        """{"snapshots": {OCC: parsed}, "truncated": bool, "fetched_at": unix}."""
        if not re.match(r"^[A-Z][A-Z0-9.]{0,9}$", underlying or ""):
            raise InvalidRequest(f"Invalid underlying symbol {underlying!r}")
        params = {"feed": self.feed.id, "limit": CHAIN_PAGE,
                  "expiration_date_gte": exp_gte.isoformat(),
                  "expiration_date_lte": exp_lte.isoformat()}
        if kind:
            params["type"] = kind
        if strike_gte is not None:
            params["strike_price_gte"] = round(strike_gte, 2)
        if strike_lte is not None:
            params["strike_price_lte"] = round(strike_lte, 2)

        def fetch():
            snaps, truncated, p = {}, False, dict(params)
            for page in range(CHAIN_MAX_PAGES):
                body = self.client.get_data(f"/v1beta1/options/snapshots/{underlying}", p)
                if not isinstance(body, dict):
                    raise DataError("Unexpected option chain response from Alpaca")
                for sym, raw in (body.get("snapshots") or {}).items():
                    if isinstance(raw, dict):
                        snaps[sym] = parse_snapshot(sym, raw)
                token = body.get("next_page_token")
                if not token:
                    break
                p["page_token"] = token
                truncated = page == CHAIN_MAX_PAGES - 1
            return {"snapshots": snaps, "truncated": truncated, "fetched_at": int(time.time())}

        return self._cached(("chain", underlying, tuple(sorted(params.items()))),
                            self.chain_ttl, fetch)


def capabilities(feed: OptionsFeed | None, configured: bool) -> dict:
    """What this installation can retrieve, and the provider's restrictions."""
    return {
        "provider": "Alpaca",
        "configured": configured,
        "feed": feed.to_dict() if feed else None,
        "feeds": {k: v.to_dict() for k, v in OPTIONS_FEEDS.items()},
        "fields": {
            "from_chain_snapshots": ["bid", "ask", "bid/ask size", "quote time",
                                     "last trade", "daily volume", "implied volatility",
                                     "delta", "gamma", "theta", "vega", "rho"],
            "from_contracts_api": ["call/put", "strike", "expiration", "exercise style",
                                   "multiplier", "tradable", "open interest (+ date)",
                                   "previous close (+ date)"],
        },
        "restrictions": [
            "Option data needs Alpaca API keys; demo mode never simulates option prices or "
            "Greeks.",
            "The free indicative feed's quotes are not actual OPRA quotes and its trades are "
            "delayed 15 minutes, so TradeScope never recommends a contract from it.",
            "Real-time OPRA data needs the Algo Trader Plus subscription and a signed OPRA "
            "agreement; without them Alpaca answers 403.",
            "Implied volatility and Greeks are model values computed by Alpaca from its quotes; "
            "they can be missing (e.g. no recent quote or deep in/out of the money).",
            "Open interest comes from the contracts API and is updated once per day "
            "(open_interest_date); it does not change intraday.",
            "Volume is the contract's volume in its latest daily bar (current session once it "
            "trades, otherwise the previous one).",
        ],
        "order_execution": False,
    }
