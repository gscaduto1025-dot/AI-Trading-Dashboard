"""An in-process fake of Alpaca's options endpoints (TEST DATA ONLY, never served to users).

Response shapes follow the official alpaca-py SDK:
  GET /v2/options/contracts                -> {"option_contracts": [...], "next_page_token"}
  GET /v1beta1/options/snapshots/{under}   -> {"snapshots": {OCC: {latestQuote, latestTrade,
                                               dailyBar, prevDailyBar, impliedVolatility,
                                               greeks}}, "next_page_token"}
"""

from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone

import httpx


def occ(root: str, exp: date, kind: str, strike: float) -> str:
    return f"{root}{exp:%y%m%d}{'C' if kind == 'call' else 'P'}{int(round(strike * 1000)):08d}"


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class FakeOptions:
    def __init__(self, now: datetime, underlying: str = "SPY", price: float = 100.0,
                 expirations: list[date] | None = None, strikes: list[float] | None = None,
                 page_size: int = 7, delta_per_strike: float = 0.08) -> None:
        self.now, self.underlying, self.price = now, underlying, price
        today = now.date()
        self.expirations = expirations or [today + timedelta(days=d) for d in (3, 10, 31, 59)]
        self.strikes = strikes or [price + d for d in range(-6, 7)]
        self.page_size = page_size
        self.delta_per_strike = delta_per_strike
        self.requests: list[httpx.Request] = []
        self.status: dict[str, int] = {}  # path prefix -> forced HTTP status
        self.contract_rows: dict[str, dict] = {}
        self.snapshots: dict[str, dict] = {}
        for exp in self.expirations:
            for k in self.strikes:
                for kind in ("call", "put"):
                    self._add(exp, k, kind)

    def _add(self, exp: date, k: float, kind: str) -> None:
        sym = occ(self.underlying, exp, kind, k)
        call = kind == "call"
        d_call = max(0.02, min(0.98, 0.5 - (k - self.price) * self.delta_per_strike))
        delta = d_call if call else d_call - 1
        intrinsic = max(0.0, (self.price - k) if call else (k - self.price))
        mid = round(intrinsic + 1.2 + 0.05 * (exp - self.now.date()).days ** 0.5, 2)
        self.contract_rows[sym] = {
            "id": f"id-{sym}", "symbol": sym, "name": f"{self.underlying} {exp} {kind} {k}",
            "status": "active", "tradable": True, "expiration_date": exp.isoformat(),
            "root_symbol": self.underlying, "underlying_symbol": self.underlying,
            "underlying_asset_id": "00000000-0000-0000-0000-000000000000", "type": kind,
            "style": "american", "strike_price": f"{k:.2f}", "size": "100",
            "open_interest": "2500", "open_interest_date": (self.now.date() - timedelta(days=1)).isoformat(),
            "close_price": f"{mid:.2f}", "close_price_date": (self.now.date() - timedelta(days=1)).isoformat(),
        }
        t = iso(self.now - timedelta(seconds=5))
        self.snapshots[sym] = {
            "latestQuote": {"t": t, "bp": round(mid - 0.03, 2), "bs": 20, "bx": "C",
                            "ap": round(mid + 0.03, 2), "as": 25, "ax": "C", "c": "A"},
            "latestTrade": {"t": t, "p": mid, "s": 3, "x": "C", "c": "I"},
            "dailyBar": {"t": iso(self.now.replace(hour=4, minute=0)), "o": mid, "h": mid,
                         "l": mid, "c": mid, "v": 900, "n": 120, "vw": mid},
            "prevDailyBar": {"t": iso(self.now.replace(hour=4, minute=0) - timedelta(days=1)),
                             "o": mid, "h": mid, "l": mid, "c": mid, "v": 1500, "n": 200,
                             "vw": mid},
            "impliedVolatility": 0.24,
            "greeks": {"delta": round(delta, 4), "gamma": 0.06, "rho": 0.01,
                       "theta": -0.04, "vega": 0.09},
        }

    def symbol(self, exp_index: int, strike: float, kind: str = "call") -> str:
        return occ(self.underlying, self.expirations[exp_index], kind, strike)

    def patch(self, sym: str, **changes) -> None:
        """Deep-ish override of one snapshot: quote={...}, greeks=None, drop=True, ..."""
        if changes.pop("drop", False):
            self.snapshots.pop(sym, None)
            return
        snap = self.snapshots[sym]
        for k, v in changes.items():
            key = {"quote": "latestQuote", "trade": "latestTrade", "bar": "dailyBar",
                   "iv": "impliedVolatility"}.get(k, k)
            if isinstance(v, dict) and isinstance(snap.get(key), dict):
                snap[key] = {**snap[key], **v}
            else:
                snap[key] = v

    # ---------------------------------------------------------------- transport

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, q = request.url.path, dict(request.url.params)
        for prefix, code in self.status.items():
            if path.startswith(prefix):
                return httpx.Response(code, json={"message": "subscription does not permit "
                                                             "querying recent OPRA data"})
        if path == "/v2/options/contracts":
            return self._contracts(q)
        if path.startswith("/v1beta1/options/snapshots/"):
            return self._chain(path.rsplit("/", 1)[1], q)
        return httpx.Response(404, json={"message": "not found"})

    def _match(self, row_exp: str, kind: str, strike: float, q: dict) -> bool:
        if q.get("type") and q["type"] != kind:
            return False
        if q.get("expiration_date_gte") and row_exp < q["expiration_date_gte"]:
            return False
        if q.get("expiration_date_lte") and row_exp > q["expiration_date_lte"]:
            return False
        if q.get("strike_price_gte") and strike < float(q["strike_price_gte"]):
            return False
        if q.get("strike_price_lte") and strike > float(q["strike_price_lte"]):
            return False
        return True

    def _page(self, items: list, q: dict) -> tuple[list, str | None]:
        start = int(q.get("page_token") or 0)
        n = min(int(q.get("limit") or 100), self.page_size)
        nxt = start + n
        return items[start:nxt], (str(nxt) if nxt < len(items) else None)

    def _contracts(self, q: dict) -> httpx.Response:
        rows = [copy.deepcopy(r) for r in self.contract_rows.values()
                if r["underlying_symbol"] in q.get("underlying_symbols", "").split(",")
                and self._match(r["expiration_date"], r["type"], float(r["strike_price"]), q)]
        page, token = self._page(rows, q)
        return httpx.Response(200, json={"option_contracts": page, "next_page_token": token})

    def _chain(self, underlying: str, q: dict) -> httpx.Response:
        items = []
        for sym, snap in self.snapshots.items():
            r = self.contract_rows[sym]
            if r["underlying_symbol"] == underlying and \
                    self._match(r["expiration_date"], r["type"], float(r["strike_price"]), q):
                items.append((sym, copy.deepcopy(snap)))
        page, token = self._page(items, q)
        return httpx.Response(200, json={"snapshots": dict(page), "next_page_token": token})
