"""Evaluate swing-strategy behaviour on the real daily fixtures (AAPL 2015-2017, GOOG 2004-2013).

    cd backend && .venv/bin/python -m scripts.swing_backtest

For every accepted trigger it follows later candles (informational only, exactly like the
engine's `outcome`): T1 before stop, stopped first, or still open at the end of the data.
If one candle touches both the stop and a target, the stop is assumed first. R is measured
as +reward/risk at T1, -1 at the stop, and mark-to-market at the last close when open.
No costs, slippage, gaps through stops, position sizing or portfolio effects - this is a
sanity check of the rules on real price paths, not a performance claim.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.strategies.common import outcome_after  # noqa: E402
from app.swing.config import SwingConfig  # noqa: E402
from app.swing.context import build_swing_context  # noqa: E402
from app.swing.engine import MIN_SCAN_INDEX, SWING_STRATEGIES  # noqa: E402
from tests.test_key_levels import AAPL, CAL, GOOG, at, weekly  # noqa: E402

DATASETS = [("AAPL", AAPL, at(2017, 12, 29)), ("GOOG", GOOG, at(2013, 12, 31))]


def main() -> None:
    cfg = SwingConfig()
    stats: dict = defaultdict(lambda: {"signals": 0, "accepted": 0, "t1": 0, "stopped": 0,
                                       "open": 0, "r": 0.0})
    for name, bars, end in DATASETS:
        ctx = build_swing_context(name, {"1Day": bars, "1Week": weekly(bars)}, end, CAL, cfg)
        for strat in SWING_STRATEGIES:
            for tf, d in ctx.tfs.items():
                for direction in ("LONG", "SHORT"):
                    res = strat.evaluate(ctx, d, direction, MIN_SCAN_INDEX)
                    for t in res.triggers:
                        row = stats[(strat.name, tf)]
                        row["signals"] += 1
                        if t.blocked:
                            continue
                        row["accepted"] += 1
                        o = outcome_after(d, t.index, direction, t.stop, t.target1, t.target2)
                        risk = abs(t.entry - t.stop)
                        if o["state"] in ("target1", "target2", "target1_then_stopped"):
                            row["t1"] += 1
                            row["r"] += t.risk_reward
                        elif o["state"] == "stopped":
                            row["stopped"] += 1
                            row["r"] -= 1
                        else:
                            row["open"] += 1
                            sg = 1 if direction == "LONG" else -1
                            row["r"] += sg * (d.bars[-1].close - t.entry) / risk
    print(f"{'strategy':28} {'tf':6} {'signals':>7} {'accepted':>8} {'T1 first':>8} "
          f"{'stopped':>7} {'open':>5} {'avg R':>6}")
    for (strat, tf), r in sorted(stats.items()):
        acc = r["accepted"]
        avg = r["r"] / acc if acc else 0.0
        print(f"{strat:28} {tf:6} {r['signals']:7d} {acc:8d} {r['t1']:8d} {r['stopped']:7d} "
              f"{r['open']:5d} {avg:6.2f}")
    print("\nSignals = completed-candle setups that met the pattern rules; accepted = also passed "
          "the shared risk rules (R:R, stop width, no chasing, abnormal candle).")


if __name__ == "__main__":
    main()
