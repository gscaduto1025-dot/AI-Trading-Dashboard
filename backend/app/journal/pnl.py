"""Trade-log P&L (average-cost method), for the user's ACTUAL and PAPER trades.

A trade is a direction plus a list of fills:
- LONG:  BUY fills open / add, SELL fills reduce / close.
- SHORT: SELL fills open / add, BUY fills cover.
A fill may never take the position past flat (reversing needs a new trade). Fees are
per fill and always reduce P&L.

Realized P&L of a closing fill = (exit - average entry) x quantity x direction. The average
entry changes only when the position is increased. R multiple = net realized P&L / initial
risk, where initial risk = |first entry price - planned stop| x the largest position held.
"""

from __future__ import annotations

from dataclasses import dataclass

EPS = 1e-9


class JournalError(ValueError):
    pass


@dataclass(frozen=True)
class Fill:
    side: str          # BUY | SELL
    quantity: float
    price: float
    fees: float
    executed_at: int
    id: int | None = None


def validate_fill(f: Fill) -> None:
    if f.side not in ("BUY", "SELL"):
        raise JournalError("side must be BUY or SELL")
    if not f.quantity > 0 or not f.price > 0:
        raise JournalError("quantity and price must be positive")
    if f.fees < 0:
        raise JournalError("fees cannot be negative")


def compute(direction: str, fills: list[Fill], planned_stop: float | None = None,
            mark: float | None = None) -> dict:
    """P&L summary. Raises JournalError if the fills are inconsistent."""
    if direction not in ("LONG", "SHORT"):
        raise JournalError("direction must be LONG or SHORT")
    sgn = 1 if direction == "LONG" else -1
    open_side = "BUY" if direction == "LONG" else "SELL"
    pos = 0.0           # shares held (always >= 0)
    avg = 0.0           # average entry of the open position
    realized_gross = 0.0
    fees = 0.0
    max_pos = 0.0
    first_entry = None
    entries_qty = exits_qty = 0.0
    entry_notional = exit_notional = 0.0
    exits = []
    # Same-timestamp fills keep their recorded order: stored fills by id, then new fills
    # (no id yet) in the order given - so an exit is never sorted before its entry.
    order = sorted(range(len(fills)), key=lambda i: (
        fills[i].executed_at, fills[i].id if fills[i].id is not None else float("inf"), i))
    for f in (fills[i] for i in order):
        validate_fill(f)
        fees += f.fees
        if f.side == open_side:
            if first_entry is None:
                first_entry = f.price
            avg = (avg * pos + f.price * f.quantity) / (pos + f.quantity)
            pos += f.quantity
            entries_qty += f.quantity
            entry_notional += f.price * f.quantity
            max_pos = max(max_pos, pos)
        else:
            if pos <= EPS:
                raise JournalError(f"{f.side} fill at {f.executed_at} has no open {direction} "
                                   f"position to close")
            if f.quantity > pos + EPS:
                raise JournalError(f"{f.side} of {f.quantity:g} exceeds the open position of "
                                   f"{pos:g}; a reversal must be recorded as a new trade")
            pnl = (f.price - avg) * f.quantity * sgn
            realized_gross += pnl
            pos -= f.quantity
            exits_qty += f.quantity
            exit_notional += f.price * f.quantity
            exits.append({"quantity": f.quantity, "price": f.price, "pnl": round(pnl, 4),
                          "at": f.executed_at})
            if pos <= EPS:
                pos = 0.0
    status = "open" if pos > EPS else ("closed" if entries_qty else "empty")
    net = realized_gross - fees
    risk = abs(first_entry - planned_stop) * max_pos \
        if first_entry is not None and planned_stop is not None else None
    unrealized = (mark - avg) * pos * sgn if mark is not None and pos > EPS else None
    times = [f.executed_at for f in fills]
    return {
        "status": status,
        "open_quantity": round(pos, 6),
        "avg_entry": round(avg, 6) if pos > EPS else
        (round(entry_notional / entries_qty, 6) if entries_qty else None),
        "avg_exit": round(exit_notional / exits_qty, 6) if exits_qty else None,
        "entered_quantity": round(entries_qty, 6), "exited_quantity": round(exits_qty, 6),
        "max_position": round(max_pos, 6),
        "partial_exits": max(0, len(exits) - (1 if status == "closed" else 0)),
        "exits": exits,
        "realized_gross": round(realized_gross, 4),
        "fees": round(fees, 4),
        "realized_net": round(net, 4),
        "unrealized": round(unrealized, 4) if unrealized is not None else None,
        "initial_risk": round(risk, 4) if risk else None,
        "r_multiple": round(net / risk, 4) if risk and status == "closed" else None,
        "opened_at": min(times) if times else None,
        "closed_at": max(times) if status == "closed" else None,
    }


def stats(trades: list[dict]) -> dict:
    """Closed-trade statistics for one account (actual OR paper, never mixed)."""
    closed = [t for t in trades if t["pnl"]["status"] == "closed"]
    n = len(closed)
    nets = [t["pnl"]["realized_net"] for t in closed]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    rs = [t["pnl"]["r_multiple"] for t in closed if t["pnl"]["r_multiple"] is not None]
    gl = -sum(losses)
    return {
        "closed_trades": n, "open_trades": sum(t["pnl"]["status"] == "open" for t in trades),
        "win_rate": round(100 * len(wins) / n, 2) if n else None,
        "net_pnl": round(sum(nets), 2), "fees": round(sum(t["pnl"]["fees"] for t in trades), 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": round(sum(wins) / gl, 3) if gl > 0 else None,
        "expectancy": round(sum(nets) / n, 2) if n else None,
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None, "r_trades": len(rs),
        "sample_note": f"n={n}" + (": too few closed trades to judge" if n < 30 else ""),
    }
