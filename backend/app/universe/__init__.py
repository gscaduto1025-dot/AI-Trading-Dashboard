"""Configurable scan universe: liquid US stocks ranked by consolidated dollar volume.

Built from real Alpaca data only (read-only endpoints; no order endpoint is ever called):
  1. /v2/assets (trading API): active, tradable US equities on NYSE, NASDAQ, NYSE Arca,
     Cboe BZX and NYSE American. ETFs/ETNs are excluded by name unless
     UNIVERSE_INCLUDE_ETFS=true (Alpaca does not flag ETFs; the name test is a heuristic).
  2. /v2/stocks/snapshots (IEX feed), in batches: a pre-screen on the previous session's
     price and IEX dollar volume, keeping the top UNIVERSE_PRESCREEN_FACTOR x size.
  3. /v2/stocks/bars 1Day, feed=sip (consolidated, completed sessions only), in batches:
     the 20-session average dollar volume and last close. Members must close >= MIN_PRICE
     and average >= MIN_ADV_USD; the top UNIVERSE_SIZE by average dollar volume are kept.
UNIVERSE_INCLUDE always joins (if it has data), UNIVERSE_EXCLUDE never does. The result
and every number used are stored with timestamps. In demo mode the universe is the demo
watchlist, labelled DEMO.
"""

from __future__ import annotations

import logging
import os
import re
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..data.base import DataError
from ..market.calendar import NY

log = logging.getLogger("tradescope.universe")

EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "BATS", "AMEX"}
ETF_NAME = re.compile(
    r"\b(ETF|ETN|ETP|Exchange[- ]Traded|Fund|Index|Portfolio|Notes due)\b|Trust,? Series|"
    r"\b(iShares|SPDR|ProShares|Direxion|Invesco|Vanguard|WisdomTree|VanEck|Global X|"
    r"First Trust|Schwab|Grayscale|ProFunds|GraniteShares|Leverage Shares|Tradr|Defiance|"
    r"YieldMax|Roundhill|Amplify|Xtrackers|Franklin|JPMorgan|Simplify|T-Rex|MicroSectors)\b",
    re.I)
SYMBOL_OK = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
SNAPSHOT_BATCH = 200
BARS_BATCH = 100
ADV_SESSIONS = 20


@dataclass(frozen=True)
class UniverseConfig:
    size: int = 100
    min_price: float = 5.0
    min_adv_usd: float = 50_000_000.0
    include_etfs: bool = False
    prescreen_factor: int = 4
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_size: int = 500  # rate-limit and memory ceiling (see docs/SCANNER.md)

    @classmethod
    def from_env(cls) -> UniverseConfig:
        env = os.environ.get
        lst = lambda k: tuple(s.strip().upper() for s in env(k, "").split(",")  # noqa: E731
                              if s.strip())
        size = int(env("UNIVERSE_SIZE", "100"))
        return cls(size=max(1, min(size, cls.max_size)),
                   min_price=float(env("UNIVERSE_MIN_PRICE", "5")),
                   min_adv_usd=float(env("UNIVERSE_MIN_ADV_USD", "50000000")),
                   include_etfs=env("UNIVERSE_INCLUDE_ETFS", "false").lower() == "true",
                   prescreen_factor=max(2, int(env("UNIVERSE_PRESCREEN_FACTOR", "4"))),
                   include=lst("UNIVERSE_INCLUDE"), exclude=lst("UNIVERSE_EXCLUDE"))

    def to_dict(self) -> dict:
        return {"size": self.size, "min_price": self.min_price, "min_adv_usd": self.min_adv_usd,
                "include_etfs": self.include_etfs, "prescreen_factor": self.prescreen_factor,
                "include": list(self.include), "exclude": list(self.exclude),
                "max_size": self.max_size}


@dataclass
class Universe:
    members: list[dict]
    built_at: str
    source: str
    config: dict
    stats: dict = field(default_factory=dict)

    @property
    def symbols(self) -> list[str]:
        return [m["symbol"] for m in self.members]

    def to_dict(self) -> dict:
        return {"members": self.members, "built_at": self.built_at, "source": self.source,
                "config": self.config, "stats": self.stats, "size": len(self.members)}


CORPORATE = re.compile(r"\b(Inc|Corp|Corporation|Ltd|Limited|plc|PLC|Group|Holdings?|N\.V|"
                       r"S\.A|AG|SE|LLC|L\.P)\b\.?|&\s*Co\b|\bCo\.", re.I)
ETF_ALWAYS = re.compile(r"\b(ETF|ETN|ETP)\b|Trust,? Series", re.I)


def is_etf(name: str) -> bool:
    """Heuristic (Alpaca does not flag ETFs): 'ETF/ETN/ETP' always counts; a fund family
    or fund word counts only when the name has no corporate suffix (so Charles Schwab
    Corporation or Invesco Ltd stay in, Schwab U.S. Dividend Equity ETF does not)."""
    name = name or ""
    if ETF_ALWAYS.search(name):
        return True
    return bool(ETF_NAME.search(name)) and not CORPORATE.search(name)


def _now_iso() -> str:
    return datetime.now(NY).isoformat(timespec="seconds")


def build_universe(market, cfg: UniverseConfig, watchlist: list[str]) -> Universe:
    """Rank the eligible universe. Raises DataError if Alpaca cannot be reached."""
    if market.source.is_demo:
        return Universe([{"symbol": s, "rank": i + 1, "name": None, "adv_usd": None,
                          "close": None, "basis": "DEMO watchlist"}
                         for i, s in enumerate(watchlist)], _now_iso(), "DEMO", cfg.to_dict(),
                        {"note": "Demo mode: the universe is the demo watchlist."})
    client = market.source.client
    t0 = time.monotonic()
    req0 = getattr(client, "requests_made", 0)
    assets = client.get_trading("/v2/assets", {"status": "active", "asset_class": "us_equity"})
    if not isinstance(assets, list):
        raise DataError("Unexpected /v2/assets response from Alpaca")
    names, excluded = {}, {"exchange": 0, "not_tradable": 0, "etf": 0, "symbol": 0}
    for a in assets:
        sym = a.get("symbol", "")
        if a.get("exchange") not in EXCHANGES:
            excluded["exchange"] += 1
        elif not a.get("tradable"):
            excluded["not_tradable"] += 1
        elif not SYMBOL_OK.match(sym):
            excluded["symbol"] += 1
        elif not cfg.include_etfs and is_etf(a.get("name", "")) and sym not in cfg.include:
            excluded["etf"] += 1
        else:
            names[sym] = a.get("name")
    for s in cfg.exclude:
        names.pop(s, None)
    # 2. pre-screen on the previous session (IEX snapshot volume is a fraction of the
    # market, so it only orders candidates; the final ranking uses consolidated volume).
    pre: list[tuple[float, str]] = []
    syms = sorted(names)
    for i in range(0, len(syms), SNAPSHOT_BATCH):
        snaps = market.source.fetch_snapshots(syms[i:i + SNAPSHOT_BATCH])
        for s, snap in snaps.items():
            bar = (snap or {}).get("prevDailyBar") or {}
            c, v = bar.get("c"), bar.get("v")
            if c and v and c >= cfg.min_price:
                pre.append((c * v, s))
    pre.sort(reverse=True)
    keep = [s for _, s in pre[:cfg.size * cfg.prescreen_factor]]
    keep += [s for s in cfg.include if s not in keep]
    # 3. consolidated 20-session average dollar volume (completed sessions only).
    end = datetime.now(timezone.utc) - timedelta(minutes=16)
    start = end - timedelta(days=45)
    daily: dict[str, list[dict]] = {}
    for i in range(0, len(keep), BARS_BATCH):
        token = None
        while True:
            q = {"symbols": ",".join(keep[i:i + BARS_BATCH]), "timeframe": "1Day",
                 "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "feed": "sip",
                 "adjustment": "split", "limit": 10000}
            if token:
                q["page_token"] = token
            data = client.get_data("/v2/stocks/bars", q)
            for s, bars in (data.get("bars") or {}).items():
                daily.setdefault(s, []).extend(bars)
            token = data.get("next_page_token")
            if not token:
                break
    today = datetime.now(NY).date().isoformat()
    ranked = []
    for s in keep:
        bars = [b for b in daily.get(s, []) if b["t"][:10] < today][-ADV_SESSIONS:]
        if len(bars) < 10:
            continue
        adv = statistics.fmean(b["c"] * b["v"] for b in bars)
        close = bars[-1]["c"]
        forced = s in cfg.include
        if not forced and (close < cfg.min_price or adv < cfg.min_adv_usd):
            continue
        ranked.append({"symbol": s, "name": names.get(s), "adv_usd": round(adv),
                       "close": close, "sessions": len(bars),
                       "last_session": bars[-1]["t"][:10], "forced": forced})
    ranked.sort(key=lambda m: -m["adv_usd"])
    forced = [m for m in ranked if m["forced"]]
    rest = [m for m in ranked if not m["forced"]][:max(0, cfg.size - len(forced))]
    members = sorted(forced + rest, key=lambda m: -m["adv_usd"])
    for i, m in enumerate(members, 1):
        m["rank"] = i
        m["basis"] = f"{ADV_SESSIONS}-session average consolidated (SIP) dollar volume"
    stats = {"assets": len(assets), "candidates": len(names), "excluded": excluded,
             "prescreened": len(keep), "passed_filters": len(ranked),
             "requests": getattr(client, "requests_made", 0) - req0,
             "seconds": round(time.monotonic() - t0, 1)}
    log.info("Universe built: %d members (%s)", len(members), stats)
    return Universe(members, _now_iso(), "Alpaca (assets, IEX snapshots, SIP daily bars)",
                    cfg.to_dict(), stats)
