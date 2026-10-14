"""Options analysis for a validated stock setup.

Inputs are the stock setup and plan from the strategy/planning engines, the underlying's
latest trade, and Alpaca's contract list and chain snapshots. The output is a
transparent evaluation of each contract plus stock-level and option-level risk.

Principles
- Every option price, volume, open interest, IV and Greek is the provider's value. Nothing
  is filled in: a missing value stays None and blocks what depends on it.
- Option prices are never derived from the stock price. The cost of entry is the
  contract's actual ask; the Greek-based numbers (value near stock T1 / stop) are labelled
  estimates built from the provider's delta and gamma around the current mid.
- The stock stop is not an option loss limit. The maximum loss of a long option is the
  premium paid; the stop-scenario estimate is shown with the reasons it can be worse.
- A contract is a "candidate for review" only if every quote-quality, liquidity and Greek
  check passes, AND the feed provides actual quotes, the market is open, the underlying's
  data is fresh and the stock setup qualifies. Otherwise nothing is recommended and the
  reasons are listed. No order is ever placed.
"""

from __future__ import annotations

import math
from datetime import date, datetime

from ..market.calendar import NY
from .config import OptionsConfig
from .source import OptionsFeed

GREEKS = ("delta", "gamma", "theta", "vega")
QUALIFYING = ("TRIGGERED", "WATCHING")
OPRA_REQ = "requires Alpaca's Algo Trader Plus subscription and a signed OPRA agreement."

CHECK_LABELS = {
    "not_tradable": "Not tradable",
    "no_quote": "No usable quote",
    "crossed_quote": "Crossed quote",
    "zero_bid": "No bid",
    "wide_spread": "Wide bid/ask spread",
    "stale_quote": "Stale quote",
    "low_volume": "Insufficient trading volume",
    "low_open_interest": "Insufficient open interest",
    "missing_iv": "Implied volatility missing",
    "missing_greeks": "Greeks missing",
    "inconsistent_greeks": "Greeks inconsistent",
    "outside_dte_window": "Outside expiration window",
    "delta_out_of_band": "Delta outside target band",
    "stale_open_interest": "Open interest not current",
    "expires_today": "Expires today (0DTE)",
    "short_dated": "Short-dated",
    "heavy_decay": "Heavy time decay",
    "breakeven_beyond_target": "Breakeven beyond stock T1",
}


def _r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(x, nd)


def _check(out: list, cid: str, severity: str, detail: str) -> None:
    out.append({"id": cid, "label": CHECK_LABELS[cid], "severity": severity, "detail": detail})


def _ny_date(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, NY).date().isoformat() if ts else None


def _age(seconds: float) -> str:
    if seconds < 120:
        return f"{int(seconds)} s"
    if seconds < 7200:
        return f"{int(seconds // 60)} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"


def is_monthly(d: date) -> bool:
    """Standard monthly expiration: the third Friday of the month."""
    return d.weekday() == 4 and 15 <= d.day <= 21


# ------------------------------------------------------------------------- contracts

def evaluate_contract(c: dict, snap: dict | None, ctx: dict) -> dict:
    """One contract: provider data, derived metrics and every check."""
    cfg: OptionsConfig = ctx["cfg"]
    mode = ctx["mode"]
    s = ctx["price"]
    snap = snap or {}
    mult = c.get("multiplier") or 100
    exp = date.fromisoformat(c["expiration"])
    dte = (exp - ctx["today"]).days
    bid, ask = snap.get("bid"), snap.get("ask")
    greeks = snap.get("greeks") or {}
    delta, gamma, theta, vega = (greeks.get(k) for k in GREEKS)
    iv = snap.get("iv")
    checks: list[dict] = []

    # ---- quote quality and spread (actual provider quotes only)
    mid = spread = spread_pct = None
    if bid is None or ask is None or ask <= 0:
        _check(checks, "no_quote", "block",
               "Alpaca returned no bid/ask for this contract." if bid is None and ask is None
               else f"Incomplete quote: bid {bid}, ask {ask}.")
    elif bid > ask:
        _check(checks, "crossed_quote", "block", f"Bid {bid:.2f} is above ask {ask:.2f}.")
    else:
        mid = (bid + ask) / 2
        spread = ask - bid
        spread_pct = spread / mid if mid > 0 else None
        if bid <= 0:
            _check(checks, "zero_bid", "block",
                   f"No bid (ask {ask:.2f}): there may be no buyer to exit to.")
        elif spread > cfg.spread_floor and spread_pct > cfg.max_spread_pct[mode]:
            very = spread_pct > cfg.very_wide_spread_pct
            _check(checks, "wide_spread", "block",
                   f"{'Very wide' if very else 'Wide'} spread {spread:.2f} = "
                   f"{100 * spread_pct:.1f}% of mid (limit {100 * cfg.max_spread_pct[mode]:.0f}%"
                   f"). Crossing it costs {spread * mult:,.0f} USD per contract round trip.")
    qt = snap.get("quote_time")
    quote_age = ctx["now_ts"] - qt if qt else None
    if bid is not None or ask is not None:
        limit = cfg.max_quote_age_seconds[mode]
        if quote_age is None:
            _check(checks, "stale_quote", "block", "The quote has no timestamp.")
        elif quote_age > limit:
            _check(checks, "stale_quote", "block",
                   f"Quote is {_age(quote_age)} old (limit {_age(limit)})"
                   + ("; the market is closed." if not ctx["market_open"] else "."))

    # ---- activity: volume (latest daily bar) and open interest (contracts API)
    vol, vol_session = snap.get("volume"), _ny_date(snap.get("volume_time"))
    today_vol = vol
    if vol is not None and ctx["market_open"] and vol_session != ctx["today"].isoformat():
        today_vol = 0.0  # no trade yet in today's session
    if today_vol is None or today_vol < cfg.min_volume[mode]:
        detail = ("No volume data from Alpaca." if vol is None else
                  f"{int(today_vol)} contracts traded" +
                  (f" today (previous session {int(vol)} on {vol_session})"
                   if today_vol != vol else f" in the session of {vol_session}") +
                  f"; minimum {cfg.min_volume[mode]}.")
        _check(checks, "low_volume", "block", detail)
    oi, oi_date = c.get("open_interest"), c.get("open_interest_date")
    if oi is None or oi < cfg.min_open_interest[mode]:
        _check(checks, "low_open_interest", "block",
               "Open interest not reported." if oi is None else
               f"Open interest {oi} (minimum {cfg.min_open_interest[mode]}).")
    if oi is not None:
        try:
            oi_age = (ctx["today"] - date.fromisoformat(oi_date)).days if oi_date else None
        except ValueError:
            oi_age = None
        if oi_age is None or oi_age > cfg.max_oi_age_days:
            _check(checks, "stale_open_interest", "warn",
                   f"Open interest is dated {oi_date or 'unknown'}.")
    if c.get("tradable") is False:
        _check(checks, "not_tradable", "block", "Alpaca marks this contract as not tradable.")

    # ---- implied volatility and Greeks (provider values, sanity-checked, never filled)
    if iv is None:
        _check(checks, "missing_iv", "block", "Alpaca returned no implied volatility.")
    missing = [g for g in GREEKS if greeks.get(g) is None]
    if missing:
        _check(checks, "missing_greeks", "block",
               f"Missing {', '.join(missing)}: sensitivity cannot be estimated.")
    bad = []
    if delta is not None:
        if c["type"] == "call" and not 0 <= delta <= 1:
            bad.append(f"call delta {delta}")
        if c["type"] == "put" and not -1 <= delta <= 0:
            bad.append(f"put delta {delta}")
    if gamma is not None and gamma < 0:
        bad.append(f"gamma {gamma} < 0")
    if vega is not None and vega < 0:
        bad.append(f"vega {vega} < 0")
    if iv is not None and not 0 < iv <= cfg.max_iv:
        bad.append(f"IV {iv}")
    if bad:
        _check(checks, "inconsistent_greeks", "block",
               "Values outside their possible range: " + "; ".join(bad) + ".")

    # ---- fit for this setup
    lo, hi = cfg.dte_window[mode]
    if not lo <= dte <= hi:
        _check(checks, "outside_dte_window", "block",
               f"{dte} days to expiration; {mode} setups use {lo}-{hi} days.")
    dlo, dhi = cfg.delta_band[mode]
    if delta is not None and not dlo <= abs(delta) <= dhi:
        _check(checks, "delta_out_of_band", "block",
               f"|delta| {abs(delta):.2f} outside {dlo:.2f}-{dhi:.2f} for {mode} setups.")
    if dte <= 0:
        _check(checks, "expires_today", "warn",
               "Expires at today's close: value can go to zero within hours.")
    elif dte <= cfg.short_dated_dte:
        _check(checks, "short_dated", "warn", f"{dte} days to expiration.")
    theta_pct = abs(theta) / mid if theta is not None and mid else None
    if theta_pct is not None and theta_pct > cfg.heavy_decay_pct[mode]:
        _check(checks, "heavy_decay", "warn",
               f"Theta {theta:.3f} = {100 * theta_pct:.1f}% of the mid per day.")

    # ---- derived metrics (actual quotes; Greek estimates clearly separated)
    call = c["type"] == "call"
    k = c["strike"]
    intrinsic = max(0.0, (s - k) if call else (k - s)) if s else None
    breakeven = (k + ask if call else k - ask) if ask else None
    t1 = ctx.get("target1")
    if breakeven is not None and t1 is not None and \
            (breakeven > t1 if call else breakeven < t1):
        _check(checks, "breakeven_beyond_target", "warn",
               f"Breakeven at expiration {breakeven:.2f} lies beyond the stock T1 "
               f"{t1:.2f}: holding to expiration would lose money even if T1 is reached.")

    def est(level: float | None, label: str) -> dict | None:
        """Greek estimate of the option's value if the stock moved by the stock plan's
        distance from entry to `level` (stop or T1), applied from the current price, now,
        with IV and time unchanged. Delta follows gamma but is capped at +/-1 (an option
        cannot move more than the stock) and the value never falls below intrinsic. Not a
        price; reliability is "low" for moves above 5% of the stock price."""
        entry = ctx.get("entry")
        if None in (level, entry, s, mid, delta, gamma):
            return None
        ds = level - entry
        lo_d, hi_d = (0.0, 1.0) if call else (-1.0, 0.0)
        steps = 200
        dv = sum(min(hi_d, max(lo_d, delta + gamma * ds * (i + 0.5) / steps))
                 for i in range(steps)) * ds / steps
        s2 = s + ds
        floor = max(0.0, (s2 - k) if call else (k - s2))
        value = max(floor, mid + dv)
        return {"scenario": label, "underlying": _r(s2, 2), "move": _r(ds, 2),
                "value": _r(value, 2), "change_per_contract": _r((value - mid) * mult, 0),
                "pnl_from_ask_per_contract": _r((value - ask) * mult, 0) if ask else None,
                "reliability": "low" if abs(ds) > 0.05 * s else "moderate"}

    at_t1 = est(t1, "stock moves its planned reward (entry -> T1)")
    at_stop = est(ctx.get("stop"), "stock moves its planned risk (entry -> stop)")
    est_rr = None
    reliable = at_t1 and at_stop and "low" not in (at_t1["reliability"], at_stop["reliability"])
    if reliable and at_t1["pnl_from_ask_per_contract"] is not None:  # no ratio from shaky estimates
        gain, loss = at_t1["pnl_from_ask_per_contract"], -at_stop["pnl_from_ask_per_contract"]
        est_rr = _r(gain / loss, 2) if loss > 0 and gain > 0 else None

    blocks = [x for x in checks if x["severity"] == "block"]
    row = {
        "symbol": c["symbol"], "type": c["type"], "strike": k, "expiration": c["expiration"],
        "dte": dte, "monthly": is_monthly(exp), "style": c.get("style"), "multiplier": mult,
        "bid": bid, "ask": ask, "bid_size": snap.get("bid_size"), "ask_size": snap.get("ask_size"),
        "mid": _r(mid, 4), "spread": _r(spread, 4),
        "spread_pct": _r(100 * spread_pct, 2) if spread_pct is not None else None,
        "quote_time": qt, "quote_age_seconds": _r(quote_age, 0),
        "last": snap.get("last"), "last_time": snap.get("last_time"),
        "volume": vol, "volume_session": vol_session,
        "open_interest": oi, "open_interest_date": oi_date,
        "iv": iv, "delta": delta, "gamma": gamma, "theta": theta, "vega": vega,
        "rho": greeks.get("rho"),
        "moneyness": None if s is None else
        ("ATM" if abs(s - k) / s < 0.005 else
         "ITM" if (s > k) == call else "OTM"),
        "intrinsic": _r(intrinsic, 4), "extrinsic": _r(mid - intrinsic, 4)
        if mid is not None and intrinsic is not None else None,
        "breakeven": _r(breakeven, 2),
        "premium_per_contract": _r(ask * mult, 2) if ask else None,
        "spread_cost_per_contract": _r(spread * mult, 2) if spread is not None else None,
        "sensitivity": {
            "per_1_usd_move": _r(delta * mult, 2) if delta is not None else None,
            "delta_change_per_1_usd": _r(gamma * (1 if call else -1), 4)
            if gamma is not None else None,
            "theta_per_day_per_contract": _r(theta * mult, 2) if theta is not None else None,
            "theta_pct_of_mid_per_day": _r(100 * theta_pct, 2) if theta_pct is not None else None,
            "vega_per_vol_point_per_contract": _r(vega * mult, 2) if vega is not None else None,
            "at_stock_target1": at_t1, "at_stock_stop": at_stop,
            "estimated_option_rr": est_rr,
        },
        "checks": checks,
        "eligible": not blocks,
        "score": None,
        "has_data": bool(snap),
    }
    if row["eligible"]:
        row["score"] = score(row, ctx)
    return row


def score(row: dict, ctx: dict) -> dict:
    """Rules-based comparison score (0-100) with documented weights. Not a probability."""
    cfg: OptionsConfig = ctx["cfg"]
    mode = ctx["mode"]
    w = cfg.weights
    clamp = lambda x: max(0.0, min(1.0, x))  # noqa: E731
    parts = {}
    sp = (row["spread_pct"] or 0) / 100
    parts["spread"] = clamp(1 - max(0.0, sp - 0.02) / max(cfg.max_spread_pct[mode] - 0.02, 1e-9))

    def act(x, minimum):  # 0 at the minimum, full at 10x the minimum (log scale)
        return clamp(math.log10(x / minimum)) if x and minimum else 0.0

    parts["activity"] = 0.5 * act(row["volume"] or 0, cfg.min_volume[mode]) + \
        0.5 * act(row["open_interest"] or 0, cfg.min_open_interest[mode])
    dlo, dhi = cfg.delta_band[mode]
    target = cfg.target_delta[mode]
    half = max(target - dlo, dhi - target)
    parts["delta_fit"] = clamp(1 - abs(abs(row["delta"]) - target) / half)
    tp = (row["sensitivity"]["theta_pct_of_mid_per_day"] or 0) / 100
    parts["time_decay"] = clamp(1 - tp / cfg.heavy_decay_pct[mode])
    rr = row["sensitivity"]["estimated_option_rr"]
    parts["payoff"] = clamp((rr or 0) / 2)
    pts = {k: round(w[k] * v, 1) for k, v in parts.items()}
    return {"total": round(sum(pts.values()), 1), "points": pts, "weights": dict(w),
            "note": "Comparison score from documented rules; not a probability of profit."}


# --------------------------------------------------------------------------- analysis

def expirations_list(contracts: list[dict], today: date, cfg: OptionsConfig, mode: str) -> list:
    by: dict[str, dict] = {}
    lo, hi = cfg.dte_window[mode]
    for c in contracts:
        e = by.setdefault(c["expiration"], {"date": c["expiration"], "calls": 0, "puts": 0})
        e["calls" if c["type"] == "call" else "puts"] += 1
    out = []
    for e in sorted(by.values(), key=lambda x: x["date"]):
        d = date.fromisoformat(e["date"])
        dte = (d - today).days
        out.append({**e, "dte": dte, "weekday": d.strftime("%a"), "monthly": is_monthly(d),
                    "in_window": lo <= dte <= hi, "short_dated": dte <= cfg.short_dated_dte})
    return out


def strike_bounds(price: float, plan: dict, mode: str, cfg: OptionsConfig) -> tuple[float, float]:
    r = cfg.strike_range_pct[mode]
    lo, hi = price * (1 - r), price * (1 + r)
    for k in ("stop", "target1", "target2"):
        v = (plan.get(k) or {}).get("price") if plan else None
        if v:
            lo, hi = min(lo, v * 0.99), max(hi, v * 1.01)
    return lo, hi


def stock_risk(setup: dict, plan: dict) -> dict:
    p = lambda k: (plan.get(k) or {}).get("price")  # noqa: E731
    entry, stop = p("entry") or p("entry_trigger"), p("stop")
    risk = abs(entry - stop) if entry is not None and stop is not None else None
    return {
        "status": setup["status"], "direction": setup["direction"],
        "entry_trigger": p("entry_trigger"), "entry": p("entry"), "stop": stop,
        "target1": p("target1"), "target2": p("target2"),
        "risk_per_share": _r(risk, 4), "reward_per_share_t1": plan.get("reward_per_share_t1"),
        "risk_reward_t1": plan.get("risk_reward_t1"),
        "risk_per_100_shares": _r(risk * 100, 2) if risk is not None else None,
        "invalidation": plan.get("invalidation") or [],
        "note": "Stock-level risk assumes the stop can be executed near its price; gaps can "
                "fill beyond it.",
    }


def risk_notes(rows: list[dict], focus: dict | None, mode: str) -> dict:
    notes = {
        "stop_vs_option": [
            "The stock stop-loss does not limit the option's loss to an equivalent amount. A "
            "long option can lose up to the full premium paid.",
            "Option prices move by roughly delta (changing with gamma) per 1 USD of stock, "
            "plus time decay (theta) and implied-volatility changes (vega): the option can "
            "lose value even if the stock never reaches its stop.",
            "Exiting means selling at the bid: the spread is an immediate cost, and spreads "
            "often widen in fast markets and around the open and close.",
            "Gaps (overnight, news) can move the stock through the stop before any exit.",
        ],
        "time_decay": [
            "Theta is the provider's estimate of value lost per day with nothing else "
            "changing. Decay accelerates as expiration approaches, most for at-the-money "
            "contracts, and weekends and holidays count.",
        ],
        "short_dated": [
            "Options with 7 days or less to expiration have high gamma: delta swings "
            "quickly, so small stock moves cause large percentage moves in the option, both "
            "ways.",
            "Theta is large relative to the premium, so a stock that moves too slowly loses "
            "money even in the right direction.",
            "0DTE contracts can expire worthless within hours; in-the-money contracts are "
            "automatically exercised at expiration (by 0.01 USD or more), which requires the "
            "capital or margin to hold or deliver 100 shares per contract.",
            "Liquidity in short-dated strikes away from the money can disappear late in the "
            "day.",
        ],
        "iv": [
            "Implied volatility tends to fall after events (earnings, news) - an IV crush "
            "can lower the option's price even when the stock moves the right way. Vega "
            "shows the per-contract change for a 1-point IV move.",
        ],
        "estimates": [
            "The T1 / stop scenarios apply the stock plan's distances (entry -> T1, entry -> "
            "stop) to the current option price, as an immediate move with IV and time "
            "unchanged: delta follows gamma, capped at +/-1, and the value never falls below "
            "intrinsic. They are estimates, not prices, and are marked low-reliability for moves "
            "above 5% of the stock price. If the stock has not reached its entry yet, the "
            "option's price at entry will differ from today's.",
        ],
    }
    if focus:
        sens = focus["sensitivity"]
        if sens["theta_per_day_per_contract"] is not None:
            notes["time_decay"].append(
                f"{focus['symbol']}: theta {sens['theta_per_day_per_contract']:+.2f} USD per "
                f"contract per day ({sens['theta_pct_of_mid_per_day']}% of the mid) with "
                f"{focus['dte']} days left.")
        if focus["dte"] <= 7:
            notes["short_dated"].insert(0, f"{focus['symbol']} has {focus['dte']} day(s) to "
                                           f"expiration.")
        stop = sens["at_stock_stop"]
        if stop and stop["pnl_from_ask_per_contract"] is not None and focus["premium_per_contract"]:
            notes["stop_vs_option"].insert(
                0, f"{focus['symbol']}: for a stock move equal to the plan's risk, the Greek "
                   f"estimate is "
                   f"{stop['pnl_from_ask_per_contract']:+,.0f} USD per contract from the ask, but "
                   f"the maximum loss is the full premium of "
                   f"{focus['premium_per_contract']:,.0f} USD per contract.")
    return notes


def analyze(*, symbol: str, mode: str, setup: dict | None, plan: dict | None,
            underlying: dict, contracts: dict, chain: dict, feed: OptionsFeed,
            market_open: bool, now: datetime, cfg: OptionsConfig,
            expiration: str | None = None, truncated: bool = False) -> dict:
    """Evaluate the chain for one stock setup. See the module docstring for the rules."""
    today = now.astimezone(NY).date()
    base = {"available": True, "symbol": symbol, "mode": mode, "feed": feed.to_dict(),
            "as_of": now.astimezone(NY).isoformat(timespec="seconds"),
            "order_execution": False}
    if setup is None or plan is None:
        return {**base, "setup": None, "contracts": [], "candidates": [], "expirations": [],
                "recommendation": {"contract": None, "reasons": [
                    f"No {mode} setup with a trade plan for {symbol}: options are evaluated "
                    f"only for a detected stock setup."]}}
    kind = "call" if setup["direction"] == "LONG" else "put"
    price = underlying.get("price")
    p = lambda k: (plan.get(k) or {}).get("price")  # noqa: E731
    ctx = {"cfg": cfg, "mode": mode, "price": price, "today": today,
           "now_ts": int(now.timestamp()), "market_open": market_open,
           "stop": p("stop"), "target1": p("target1"),
           "entry": p("entry") or p("entry_trigger")}
    mine = [c for c in contracts.get("contracts", []) if c["type"] == kind]
    exps = expirations_list(mine, today, cfg, mode)
    lo, hi = cfg.dte_window[mode]
    if expiration:
        chosen = [c for c in mine if c["expiration"] == expiration]
    else:
        chosen = [c for c in mine if lo <= (date.fromisoformat(c["expiration"]) - today).days <= hi]
    snaps = chain.get("snapshots", {})
    rows = [evaluate_contract(c, snaps.get(c["symbol"]), ctx) for c in chosen]
    rows.sort(key=lambda r: (r["expiration"], r["strike"]))
    eligible = sorted((r for r in rows if r["eligible"]), key=lambda r: -r["score"]["total"])

    # ---- global gates: no recommendation unless every one passes
    reasons = []
    if setup["status"] not in QUALIFYING:
        reasons.append(f"The stock setup is {setup['status']}: "
                       f"{(setup.get('reasons') or ['it does not qualify'])[0]}")
    if None in (p("stop"), p("target1")):
        reasons.append("The stock plan has no stop or target, so option risk cannot be "
                       "related to it.")
    if price is None or underlying.get("stale"):
        reasons.append("Underlying price data is "
                       + ("unavailable" if price is None else "stale") + ": "
                       + (underlying.get("reason") or "no fresh trade") + ".")
    if not feed.actual_quotes:
        reasons.append(f"The {feed.label} feed's quotes are not actual OPRA quotes and its "
                       f"trades are delayed 15 minutes; a contract is never recommended from "
                       f"it. Real-time OPRA data {OPRA_REQ}")
    if not market_open:
        reasons.append("The market is closed: option quotes are from the last session and are "
                       "not executable prices now.")
    if not rows:
        reasons.append("No " + kind + " contracts in the "
                       + (f"selected expiration {expiration}" if expiration else
                          f"{lo}-{hi} day expiration window") + " near the setup's prices.")
    elif not eligible:
        reasons.append(f"None of the {len(rows)} {kind}s passed every quote-quality, liquidity "
                       f"and Greek check (see the most common blockers).")
    blocked: dict[str, int] = {}
    for r in rows:
        for x in r["checks"]:
            if x["severity"] == "block":
                blocked[x["id"]] = blocked.get(x["id"], 0) + 1
    top = eligible[0] if eligible and not reasons else None
    focus = top or (eligible[0] if eligible else None)

    limitations = [feed.coverage]
    if truncated or contracts.get("truncated") or chain.get("truncated"):
        limitations.append("Alpaca returned more contracts than one request window allows; "
                           "the list was truncated.")
    with_snap = sum(r["has_data"] for r in rows)
    if rows and with_snap < len(rows):
        limitations.append(f"{len(rows) - with_snap} of {len(rows)} contracts had no "
                           f"snapshot (no quote, trade or Greeks) from Alpaca.")
    no_greeks = blocked.get("missing_greeks", 0)
    if no_greeks:
        limitations.append(f"{no_greeks} contract(s) have no Greeks from Alpaca; their "
                           f"sensitivity is not estimated.")
    if not market_open:
        limitations.append("Market closed: quotes, volume and Greeks reflect the last session.")
    limitations.append("Implied volatility and Greeks are model values computed by the data "
                       "provider; open interest updates once per day.")

    listed = rows if len(rows) <= cfg.max_listed else _trim(rows, price, cfg.max_listed)
    return {
        **base,
        "setup": {"id": setup["id"], "strategy": setup["strategy"],
                  "strategy_name": setup["strategy_name"], "direction": setup["direction"],
                  "status": setup["status"], "timeframe": setup["timeframe"],
                  "qualifies": setup["status"] in QUALIFYING},
        "option_type": kind,
        "type_rationale": (f"{setup['direction']} stock setup -> {kind}s "
                           + ("(the right to buy: gains when the stock rises)" if kind == "call"
                              else "(the right to sell: gains when the stock falls)")
                           + ". Only long calls/puts are evaluated (maximum loss = premium)."),
        "underlying": underlying,
        "market_open": market_open,
        "stock_risk": stock_risk(setup, plan),
        "expirations": exps,
        "selected_expiration": expiration,
        "dte_window": [lo, hi],
        "contracts": listed,
        "contracts_total": len(rows),
        "candidates": eligible[: cfg.max_compared],
        "eligible_count": len(eligible),
        "blocked_by": dict(sorted(blocked.items(), key=lambda kv: -kv[1])),
        "check_labels": CHECK_LABELS,
        "recommendation": {
            "contract": top,
            "reasons": reasons if not top else [],
            "label": ("Highest-ranked contract that passed every check - a candidate for your "
                      "review, not advice; no order is placed.") if top else
            "No contract recommended.",
        },
        "option_risk": option_risk(focus) if focus else None,
        "risk_notes": risk_notes(rows, focus, mode),
        "data_limitations": limitations,
        "thresholds": {
            "max_spread_pct": cfg.max_spread_pct[mode], "min_volume": cfg.min_volume[mode],
            "min_open_interest": cfg.min_open_interest[mode],
            "max_quote_age_seconds": cfg.max_quote_age_seconds[mode],
            "delta_band": cfg.delta_band[mode], "target_delta": cfg.target_delta[mode],
            "dte_window": cfg.dte_window[mode], "weights": cfg.weights,
        },
    }


def option_risk(r: dict) -> dict:
    s = r["sensitivity"]
    return {
        "contract": r["symbol"],
        "premium_per_contract": r["premium_per_contract"],
        "max_loss_per_contract": r["premium_per_contract"],
        "max_loss_note": "For a long option the maximum loss is the full premium paid, "
                         "regardless of the stock stop.",
        "spread_cost_per_contract": r["spread_cost_per_contract"],
        "breakeven_at_expiration": r["breakeven"],
        "estimated_at_stock_stop": s["at_stock_stop"],
        "estimated_at_stock_target1": s["at_stock_target1"],
        "estimated_option_rr": s["estimated_option_rr"],
        "theta_per_day_per_contract": s["theta_per_day_per_contract"],
        "vega_per_vol_point_per_contract": s["vega_per_vol_point_per_contract"],
        "dte": r["dte"],
    }


def _trim(rows: list[dict], price: float | None, n: int) -> list[dict]:
    """Keep the n contracts closest to the money (eligible first), in display order."""
    key = (lambda r: (not r["eligible"], abs(r["strike"] - price))) if price else \
        (lambda r: (not r["eligible"], r["strike"]))
    keep = {r["symbol"] for r in sorted(rows, key=key)[:n]}
    return [r for r in rows if r["symbol"] in keep]
