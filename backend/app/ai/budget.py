"""Cost controls for Claude calls.

- Calls are made only on an explicit user request (POST /api/ai/...); the scanner, the
  scheduler and alerts never call Claude.
- Identical facts are answered from the SQLite cache (app/main.py), so the same candles and
  plan never cost a second call.
- Hard limits: at most CLAUDE_MAX_CALLS_PER_MINUTE and CLAUDE_MAX_CALLS_PER_DAY calls, and
  at most CLAUDE_MAX_DAILY_COST_USD of estimated spend per (New York) day. When a limit is
  reached the analysis stays available and the explanation says why it was not generated.
- Every call's token usage and estimated cost is recorded (ai_usage table) and reported by
  GET /api/ai/usage.

Settings are read from the environment here (not app/config.py, which is part of the
frozen strategy rule surface).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime

from ..market.calendar import NY

# USD per million tokens: (input, output, cache read, 5-minute cache write).
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5),
}


@dataclass(frozen=True)
class Limits:
    per_minute: int = 6
    per_day: int = 100
    daily_cost_usd: float = 5.0

    @classmethod
    def from_env(cls) -> Limits:
        env = os.environ.get
        return cls(per_minute=int(env("CLAUDE_MAX_CALLS_PER_MINUTE", "6")),
                   per_day=int(env("CLAUDE_MAX_CALLS_PER_DAY", "100")),
                   daily_cost_usd=float(env("CLAUDE_MAX_DAILY_COST_USD", "5")))


def estimate_cost(model: str, usage: dict) -> float | None:
    """Estimated USD cost of one response's usage, or None for an unknown model."""
    p = PRICES.get(model)
    if not p:
        return None
    return round((usage.get("input_tokens", 0) * p[0] + usage.get("output_tokens", 0) * p[1]
                  + usage.get("cache_read_input_tokens", 0) * p[2]
                  + usage.get("cache_creation_input_tokens", 0) * p[3]) / 1e6, 6)


def day_start(now: float | None = None) -> int:
    d = datetime.fromtimestamp(now or time.time(), NY)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def check(db, limits: Limits, now: float | None = None) -> str | None:
    """None when another call is allowed, else the reason it is not."""
    now = now or time.time()
    minute = db.ai_usage_since(int(now) - 60)
    if minute["calls"] >= limits.per_minute:
        return (f"Claude call limit reached ({limits.per_minute} per minute). Try again in a "
                f"minute.")
    day = db.ai_usage_since(day_start(now))
    if day["calls"] >= limits.per_day:
        return f"Daily Claude call limit reached ({limits.per_day} calls). Resets at midnight ET."
    if day["cost_usd"] >= limits.daily_cost_usd:
        return (f"Daily Claude spend limit reached (estimated ${day['cost_usd']:.2f} of "
                f"${limits.daily_cost_usd:.2f}). Resets at midnight ET.")
    return None


def summary(db, limits: Limits, now: float | None = None) -> dict:
    now = now or time.time()
    day = db.ai_usage_since(day_start(now))
    return {"today": day, "limits": {"per_minute": limits.per_minute,
                                     "per_day": limits.per_day,
                                     "daily_cost_usd": limits.daily_cost_usd},
            "remaining_calls_today": max(0, limits.per_day - day["calls"]),
            "remaining_budget_usd": round(max(0.0, limits.daily_cost_usd - day["cost_usd"]), 4),
            "recent": db.ai_usage_recent(10),
            "prices_usd_per_mtok": {m: dict(zip(("input", "output", "cache_read",
                                                 "cache_write_5m"), p))
                                    for m, p in PRICES.items()}}
