"""The user's trade log: ACTUAL trades and PAPER trades, each kept apart, and both kept
apart from simulated backtest trades (which live only in backtest runs).

Entries are recorded manually. TradeScope never sends an order: no fill here is ever
executed by the app.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from ..data.base import DataError
from .pnl import Fill, JournalError, compute, stats

ACCOUNTS = ("actual", "paper")
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")


def check_account(account: str) -> str:
    if account not in ACCOUNTS:
        raise JournalError(
            f"account must be 'actual' or 'paper'. Backtest trades are simulated and are kept "
            f"separately in backtest runs; they cannot be added to the trade log.")
    return account


def _ts(v) -> int:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v)
    if isinstance(v, str):
        try:
            return int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    raise JournalError("executed_at must be a unix time or an ISO-8601 timestamp")


def _num(v, name, required=False):
    if v is None or v == "":
        if required:
            raise JournalError(f"{name} is required")
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise JournalError(f"{name} must be a number") from None
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
        try:
            pnl = compute(t["direction"], _fills(t["fills"]), t["planned_stop"],
                          marks.get(t["ticker"]))
            problem = None
        except JournalError as exc:  # stored data no longer consistent (should not happen)
            pnl, problem = {"status": "invalid"}, str(exc)
        return t | {"pnl": pnl, "problem": problem,
                    "mark_note": None if t["ticker"] in marks else
                    "No live price (demo mode, stale feed or not on the watchlist): unrealized "
                    "P&L is not shown."}

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
        fills = [_fill(f) for f in body.get("fills") or []]
        if not fills:
            raise JournalError("record at least the opening fill")
        compute(direction, _fills(fills), _num(body.get("planned_stop"), "planned_stop"))
        keys = {(f["side"], f["quantity"], f["price"], f["executed_at"]) for f in fills}
        if len(keys) != len(fills):
            raise JournalError("the same fill appears twice")
        row = {"ticker": ticker, "direction": direction,
               "strategy": (str(body["strategy"])[:60] if body.get("strategy") else None),
               "setup_id": (str(body["setup_id"])[:40] if body.get("setup_id") else None),
               "planned_stop": _num(body.get("planned_stop"), "planned_stop"),
               "planned_target": _num(body.get("planned_target"), "planned_target"),
               "notes": str(body.get("notes") or "")[:2000]}
        tid = self.db.journal_insert_trade(account, row, fills)
        return self._view(self.db.journal_trade(account, tid), self._marks())

    def add_fill(self, account: str, trade_id: int, body: dict) -> dict:
        account = check_account(account)
        t = self.db.journal_trade(account, trade_id)
        if t is None:
            raise KeyError("trade not found")
        f = _fill(body)
        compute(t["direction"], _fills(t["fills"] + [f]), t["planned_stop"])  # validates
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
        compute(t["direction"], _fills(rest), t["planned_stop"])  # e.g. an exit left orphaned
        self.db.journal_delete_fill(account, trade_id, fill_id)
        return self._view(self.db.journal_trade(account, trade_id), self._marks())

    def update(self, account: str, trade_id: int, body: dict) -> dict:
        account = check_account(account)
        fields = {}
        for k in ("planned_stop", "planned_target"):
            if k in body:
                fields[k] = _num(body[k], k)
        for k, n in (("notes", 2000), ("strategy", 60), ("setup_id", 40)):
            if k in body:
                fields[k] = str(body[k] or "")[:n]
        if not self.db.journal_update_trade(account, trade_id, fields) and \
                self.db.journal_trade(account, trade_id) is None:
            raise KeyError("trade not found")
        return self._view(self.db.journal_trade(account, trade_id), self._marks())

    def delete(self, account: str, trade_id: int) -> bool:
        return self.db.journal_delete_trade(check_account(account), trade_id)
