"""The user's trade log: ACTUAL trades and PAPER trades, each kept apart, and both kept
apart from simulated backtest trades (which live only in backtest runs).

Entries are recorded manually. TradeScope never sends an order: no fill here is ever
executed by the app.
"""

from __future__ import annotations

import math
import re
import sqlite3
import time
from datetime import datetime

from ..data.base import DataError
from ..market.calendar import NY
from ..validation import for_strategy, registry
from .pnl import Fill, JournalError, compute, stats

ACCOUNTS = ("actual", "paper")
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")


def check_account(account: str) -> str:
    if account not in ACCOUNTS:
        raise JournalError(
            f"account must be 'actual' or 'paper'. Backtest trades are simulated and are kept "
            f"separately in backtest runs; they cannot be added to the trade log.")
    return account


TS_MIN = 631_152_000  # 1990-01-01: earlier (or millisecond) times are input mistakes
MAX_FILLS = 500


def _ts(v) -> int:
    ts = None
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
        ts = int(v)
    elif isinstance(v, str):
        try:
            ts = int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    if ts is None:
        raise JournalError("executed_at must be a unix time or an ISO-8601 timestamp")
    # Stored times must stay displayable: one out-of-range fill used to break the whole
    # journal listing, CSV export and performance view.
    if not TS_MIN <= ts <= time.time() + 86_400:
        raise JournalError("executed_at must be between 1990 and now, in seconds "
                           "(not milliseconds)")
    return ts


def _num(v, name, required=False):
    if v is None or v == "":
        if required:
            raise JournalError(f"{name} is required")
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise JournalError(f"{name} must be a number") from None
    if not math.isfinite(x):
        raise JournalError(f"{name} must be a finite number")
    return x


def _fill(raw: dict) -> dict:
    side = str(raw.get("side", "")).upper()
    f = {"side": side, "quantity": _num(raw.get("quantity"), "quantity", True),
         "price": _num(raw.get("price"), "price", True),
         "fees": _num(raw.get("fees"), "fees") or 0.0,
         "executed_at": _ts(raw.get("executed_at")), "note": str(raw.get("note") or "")[:200]}
    if side not in ("BUY", "SELL"):
        raise JournalError("side must be BUY or SELL")
    if f["quantity"] <= 0 or f["price"] <= 0 or f["fees"] < 0:
        raise JournalError("quantity and price must be positive and fees non-negative")
    return f


def _fills(rows: list[dict]) -> list[Fill]:
    return [Fill(r["side"], r["quantity"], r["price"], r.get("fees") or 0.0, r["executed_at"],
                 r.get("id")) for r in rows]


class TradeLog:
    def __init__(self, db, market) -> None:
        self.db = db
        self.market = market

    def _marks(self) -> dict[str, float]:
        """Latest prices for unrealized P&L - real data only, never demo prices."""
        if self.market.source.is_demo:
            return {}
        try:
            return {q["symbol"]: q["price"] for q in self.market.get_quotes()
                    if q.get("available") and q.get("price") is not None and not q.get("stale")}
        except DataError:
            return {}

    def _view(self, t: dict, marks: dict) -> dict:
        mark = marks.get(t["ticker"]) if t.get("asset_type", "stock") == "stock" else None
        try:
            pnl = compute(t["direction"], _fills(t["fills"]), t["planned_stop"], mark,
                          t.get("multiplier") or 1.0)
            problem = None
        except JournalError as exc:  # stored data no longer consistent (should not happen)
            pnl, problem = {"status": "invalid"}, str(exc)
        return t | {"pnl": pnl, "problem": problem,
                    "trade_date": (datetime.fromtimestamp(pnl["opened_at"], NY).date().isoformat()
                                   if pnl.get("opened_at") else None),
                    "mark_note": None if mark is not None else
                    ("Options: no live option price is used; unrealized P&L is not shown."
                     if t.get("asset_type") == "option" else
                     "No live price (demo mode, stale feed or not on the watchlist): "
                     "unrealized P&L is not shown.")}

    def list(self, account: str) -> dict:
        account = check_account(account)
        marks = self._marks()
        trades = [self._view(t, marks) for t in self.db.journal_trades(account)]
        return {"account": account, "trades": trades,
                "stats": stats([t for t in trades if t["pnl"]["status"] != "invalid"]),
                "simulated": False, "order_execution": False}

    def create(self, account: str, body: dict) -> dict:
        account = check_account(account)
        ticker = str(body.get("ticker", "")).strip().upper()
        if not SYMBOL_RE.match(ticker):
            raise JournalError("ticker is not a valid symbol")
        direction = str(body.get("direction", "")).upper()
        if len(body.get("fills") or []) > MAX_FILLS:
            raise JournalError(f"at most {MAX_FILLS} fills per request")
        fills = [_fill(f) for f in body.get("fills") or []]
        if not fills:
            raise JournalError("record at least the opening fill")
        asset_type, option_symbol, multiplier = _asset(body)
        compute(direction, _fills(fills), _num(body.get("planned_stop"), "planned_stop"),
                None, multiplier)
        keys = {(f["side"], f["quantity"], f["price"], f["executed_at"]) for f in fills}
        if len(keys) != len(fills):
            raise JournalError("the same fill appears twice")
        row = {"ticker": ticker, "direction": direction,
               "strategy": (str(body["strategy"])[:60] if body.get("strategy") else None),
               "setup_id": (str(body["setup_id"])[:40] if body.get("setup_id") else None),
               "planned_stop": _num(body.get("planned_stop"), "planned_stop"),
               "planned_target": _num(body.get("planned_target"), "planned_target"),
               "planned_target2": _num(body.get("planned_target2"), "planned_target2"),
               "notes": str(body.get("notes") or "")[:2000],
               "mode": _mode(body.get("mode")), "asset_type": asset_type,
               "option_symbol": option_symbol, "multiplier": multiplier,
               "quality": _quality(body.get("quality")),
               **_evidence(body.get("strategy"))}
        tid = self.db.journal_insert_trade(account, row, fills)
        return self._view(self.db.journal_trade(account, tid), self._marks())

    def add_fill(self, account: str, trade_id: int, body: dict) -> dict:
        account = check_account(account)
        t = self.db.journal_trade(account, trade_id)
        if t is None:
            raise KeyError("trade not found")
        f = _fill(body)
        compute(t["direction"], _fills(t["fills"] + [f]), t["planned_stop"], None,
                t.get("multiplier") or 1.0)  # validates
        try:
            self.db.journal_add_fill(trade_id, f)
        except sqlite3.IntegrityError:
            raise FileExistsError("this exact fill is already recorded") from None
        return self._view(self.db.journal_trade(account, trade_id), self._marks())

    def delete_fill(self, account: str, trade_id: int, fill_id: int) -> dict:
        account = check_account(account)
        t = self.db.journal_trade(account, trade_id)
        if t is None:
            raise KeyError("trade not found")
        rest = [f for f in t["fills"] if f["id"] != fill_id]
        if len(rest) == len(t["fills"]):
            raise KeyError("fill not found")
        if not rest:
            raise JournalError("a trade needs at least one fill; delete the trade instead")
        compute(t["direction"], _fills(rest), t["planned_stop"], None,
                t.get("multiplier") or 1.0)  # e.g. an exit left orphaned
        self.db.journal_delete_fill(account, trade_id, fill_id)
        return self._view(self.db.journal_trade(account, trade_id), self._marks())

    def update(self, account: str, trade_id: int, body: dict) -> dict:
        account = check_account(account)
        fields = {}
        for k in ("planned_stop", "planned_target", "planned_target2"):
            if k in body:
                fields[k] = _num(body[k], k)
        for k, n in (("notes", 2000), ("strategy", 60), ("setup_id", 40),
                     ("option_symbol", 40)):
            if k in body:
                fields[k] = str(body[k] or "")[:n] or None
        if "notes" in fields:
            fields["notes"] = fields["notes"] or ""
        if "mode" in body:
            fields["mode"] = _mode(body["mode"])
        if "quality" in body:
            fields["quality"] = _quality(body["quality"])
        if "strategy" in body:
            fields |= _evidence(fields["strategy"])
        if "planned_stop" in fields:
            t = self.db.journal_trade(account, trade_id)
            if t is not None:  # the stop drives R: validate with the trade's fills
                compute(t["direction"], _fills(t["fills"]), fields["planned_stop"], None,
                        t.get("multiplier") or 1.0)
        if not self.db.journal_update_trade(account, trade_id, fields) and \
                self.db.journal_trade(account, trade_id) is None:
            raise KeyError("trade not found")
        return self._view(self.db.journal_trade(account, trade_id), self._marks())

    def close(self, account: str, trade_id: int, body: dict) -> dict:
        """Close the whole open position with one exit fill at `price`."""
        account = check_account(account)
        t = self.db.journal_trade(account, trade_id)
        if t is None:
            raise KeyError("trade not found")
        pnl = compute(t["direction"], _fills(t["fills"]), t["planned_stop"], None,
                      t.get("multiplier") or 1.0)
        if pnl["status"] != "open":
            raise JournalError("the trade is already closed")
        side = "SELL" if t["direction"] == "LONG" else "BUY"
        return self.add_fill(account, trade_id, {
            "side": side, "quantity": pnl["open_quantity"], "price": body.get("price"),
            "fees": body.get("fees"), "executed_at": body.get("executed_at"),
            "note": body.get("note") or "close"})

    def delete(self, account: str, trade_id: int) -> bool:
        return self.db.journal_delete_trade(check_account(account), trade_id)


def _mode(v) -> str | None:
    if v in (None, ""):
        return None
    if v not in ("day", "swing"):
        raise JournalError("mode must be day or swing")
    return v


def _quality(v) -> float | None:
    q = _num(v, "quality")
    if q is not None and not 0 <= q <= 100:
        raise JournalError("quality must be between 0 and 100")
    return q


def _asset(body: dict) -> tuple[str, str | None, float]:
    asset = str(body.get("asset_type") or "stock").lower()
    if asset not in ("stock", "option"):
        raise JournalError("asset_type must be stock or option")
    if asset == "stock":
        return "stock", None, 1.0
    sym = str(body.get("option_symbol") or "").strip().upper()[:40] or None
    mult = _num(body.get("multiplier"), "multiplier") or 100.0
    if mult <= 0:
        raise JournalError("multiplier must be positive")
    return "option", sym, mult


def _evidence(strategy) -> dict:
    """Snapshot of the strategy's validation status when the trade is logged (a later
    registry change does not rewrite history)."""
    if not strategy or strategy not in registry().get("strategies", {}):
        return {"evidence_status": None, "evidence_label": None}
    v = for_strategy(strategy)
    return {"evidence_status": v["status"], "evidence_label": v["label"]}
