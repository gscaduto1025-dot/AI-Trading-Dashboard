"""Run a backtest: replay -> hypothetical execution -> metrics and validation.

No order is ever sent anywhere: trades exist only in the result.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from ..market.calendar import NY, MarketCalendar
from . import metrics as M
from .config import EXEC_TF, BacktestConfig
from .data import HistoryProvider
from .execution import simulate
from .regime import regime_at
from .replay import load_series, replay_symbol

MAX_DAYS = {"day": 190, "swing": 366 * 15}  # calendar-day span limits per run

LIMITATIONS = [
    "Hypothetical results: no real orders were placed. Fills are modelled from OHLC candles; "
    "real fills depend on queue position, liquidity and latency.",
    "Intrabar order is unknown. When a stop and a target are both inside one candle the stop "
    "is assumed first (conservative); this can understate some winners.",
    "Spread, slippage and commission are fixed assumptions (shown with the results), not "
    "measured from historical quotes. Wide-spread or fast markets cost more.",
    "Day trades fill at the open of the candle after the signal and use 5-minute candles for "
    "exits; anything faster than 5 minutes is not resolved.",
    "Trades are simulated independently: overlapping trades are allowed across different "
    "setups and the equity curve applies fixed-fractional risk in exit order, without "
    "margin, buying-power or portfolio-level limits.",
    "The strategy and planning rules are today's rules. Results on the same period used to "
    "design them (in-sample) are optimistic; the out-of-sample and walk-forward sections are "
    "the fairer view.",
    "Survivorship bias: a universe made of today's watchlist (or of well-known survivors such "
    "as the bundled AAPL/GOOG history) excludes companies that were delisted or fell out of "
    "favour. Supply a point-in-time universe to reduce it.",
    "Historical data can contain errors, gaps and adjustments (splits/dividends); integrity "
    "issues found are listed per series. Corporate actions are only as good as the provider's "
    "adjustment.",
    "Market regimes change. A small number of trades, or results concentrated in one "
    "ticker or period, is weak evidence.",
]


def _ts(d: date, cal: MarketCalendar, end: bool = False) -> int:
    if end:
        days = cal.trading_days(d - timedelta(days=7), d)
        if days:
            return int(days[-1].close.timestamp())
    return int(datetime.combine(d, datetime.min.time(), NY).timestamp())


def _market_daily(provider: HistoryProvider, start: date, end: date, cal: MarketCalendar):
    """SPY daily candles for regime labels, if the source has them (else None)."""
    try:
        series, _ = load_series(provider, "SPY", "swing", start, end, cal, only=("1Day",))
        return series["1Day"] if series["1Day"].bars else None
    except Exception:
        return None


def run_backtest(provider: HistoryProvider, *, mode: str, symbols: list[str], start: date,
                 end: date, cal: MarketCalendar, engine_cfg: dict,
                 settings: dict | None = None, universe: list[dict] | None = None,
                 progress=None) -> dict:
    """`mode`: day, swing, or both (day and swing replayed separately, results combined and
    split by mode)."""
    if mode not in ("day", "swing", "both"):
        raise ValueError("mode must be day, swing or both")
    modes = ["day", "swing"] if mode == "both" else [mode]
    if start >= end:
        raise ValueError("start must be before end")
    limit = min(MAX_DAYS[m] for m in modes)
    if (end - start).days > limit:
        raise ValueError(f"{mode} backtests are limited to {limit} calendar days per run")
    cfg = BacktestConfig().with_overrides(settings)
    members = {}
    if universe:
        for m in universe:
            members[m["symbol"].upper()] = (date.fromisoformat(m["start"]) if m.get("start")
                                            else None,
                                            date.fromisoformat(m["end"]) if m.get("end") else None)
        symbols = sorted(members)
    symbols = [s.upper() for s in symbols]
    if not symbols:
        raise ValueError("no symbols to test")
    trades, skipped, rejected, data_reports, data_errors = [], [], [], [], []
    evaluations = 0
    market = _market_daily(provider, start, end, cal)
    regime_basis = "SPY (broad market)" if market else "each ticker's own daily candles"
    jobs = [(m, sym) for m in modes for sym in symbols]
    for i, (md, sym) in enumerate(jobs):
        def prog(done, of, i=i, md=md, sym=sym):
            if progress:
                progress((i + done / max(of, 1)) / len(jobs), f"{sym} ({md}): replaying candles")
        try:
            series, reps = load_series(provider, sym, md, start, end, cal)
        except Exception as exc:
            data_errors.append({"symbol": sym, "mode": md, "error": str(exc)})
            continue
        data_reports += [r.to_dict() | {"mode": md} for r in reps]
        if not series[EXEC_TF[md]].bars:
            data_errors.append({"symbol": sym, "mode": md, "error": "no candles in the period"})
            continue
        sigs, rej, ev = replay_symbol(sym, md, start, end, series, cal, engine_cfg,
                                      exhaustive=cfg.exhaustive, progress=prog)
        evaluations += ev
        rejected += [r | {"mode": md} for r in rej]
        regime_src = market or series["1Day"]
        open_until: dict[tuple, int] = {}
        for sig in sigs:
            d = datetime.fromtimestamp(sig["confirmed_at"], NY).date()
            base = {"signal_id": sig["id"], "ticker": sym, "mode": md,
                    "strategy": sig["strategy"], "timeframe": sig["timeframe"],
                    "direction": sig["direction"], "seen_at": sig["seen_at"]}
            if sym in members:
                lo, hi = members[sym]
                if (lo and d < lo) or (hi and d > hi):
                    skipped.append(base | {"reason": "not in the point-in-time universe on "
                                                     "that date"})
                    continue
            lineage = (sig["strategy"], sig["timeframe"], sig["direction"])
            if cfg.one_position_per_setup and open_until.get(lineage, 0) > sig["confirmed_at"]:
                skipped.append(base | {"reason": "a position from the same setup was still open"})
                continue
            bars = series[EXEC_TF[md]].after(sig["confirmed_at"])
            trade, skip = simulate(sig, bars, cfg, md)
            if trade:
                trade["regime"] = regime_at(regime_src, sig["confirmed_at"])
                trades.append(trade)
                open_until[lineage] = trade["exit_time"]
            else:
                skipped.append(base | skip)
    if progress:
        progress(1.0, "computing statistics")
    trades.sort(key=lambda t: (t["entry_time"], t["id"]))
    return {
        "mode": mode, "symbols": symbols, "start": start.isoformat(), "end": end.isoformat(),
        "start_ts": _ts(start, cal), "end_ts": _ts(end, cal, end=True),
        "data_source": provider.name, "data_description": provider.description,
        "is_demo": provider.is_demo,
        "survivorship_biased": not universe,
        "universe": universe, "settings": cfg.to_dict(), "regime_basis": regime_basis,
        "trades": trades, "skipped": skipped, "not_live": rejected,
        "data_integrity": data_reports, "data_errors": data_errors,
        "engine_evaluations": evaluations,
        "limitations": LIMITATIONS,
        "order_execution": False,
    }


def report(run: dict, *, strategy: str | None = None, ticker: str | None = None,
           timeframe: str | None = None, direction: str | None = None,
           start: str | None = None, end: str | None = None, mode: str | None = None,
           regime: str | None = None) -> dict:
    """Metrics for a stored run, optionally filtered. IS/OOS and walk-forward use the run's
    period so filters never move the split dates."""
    cfg = BacktestConfig().with_overrides(run["settings"])
    lo = datetime.combine(date.fromisoformat(start), datetime.min.time(), NY).timestamp() \
        if start else None
    hi = datetime.combine(date.fromisoformat(end), datetime.max.time(), NY).timestamp() \
        if end else None

    def keep(t):
        return ((not strategy or t["strategy"] == strategy)
                and (not ticker or t["ticker"] == ticker.upper())
                and (not timeframe or t["timeframe"] == timeframe)
                and (not direction or t["direction"] == direction)
                and (not mode or t.get("mode", run["mode"]) == mode)
                and (not regime or t.get("regime") == regime)
                and (lo is None or t["signal_time"] >= lo)
                and (hi is None or t["signal_time"] <= hi))

    trades = [t for t in run["trades"] if keep(t)]
    skipped = [s for s in run["skipped"]
               if (not strategy or s["strategy"] == strategy)
               and (not ticker or s["ticker"] == ticker.upper())
               and (not timeframe or s["timeframe"] == timeframe)]
    risk = cfg.risk_per_trade_pct
    port = M.portfolio(trades, starting_capital=cfg.starting_capital, risk_pct=risk,
                       max_position_pct=cfg.max_position_pct, max_leverage=cfg.max_leverage,
                       max_open=cfg.max_open_positions)
    trades = [t | port["per_trade"].get(t["id"], {"shares": 0}) for t in trades]
    overall = M.summarize(trades, risk)
    ins, outs, cut = M.split_in_out(trades, run["start_ts"], run["end_ts"],
                                    cfg.in_sample_fraction)
    is_sum, oos_sum = M.summarize(ins, risk), M.summarize(outs, risk)
    wf = M.walk_forward(trades, run["start_ts"], run["end_ts"], cfg.walk_forward_folds,
                        cfg.quality_thresholds, risk)
    skip_reasons: dict[str, int] = {}
    for s in skipped:
        skip_reasons[s["reason"]] = skip_reasons.get(s["reason"], 0) + 1
    return {
        "filters": {"strategy": strategy, "ticker": ticker, "timeframe": timeframe,
                    "direction": direction, "start": start, "end": end, "mode": mode,
                    "regime": regime},
        "overall": overall,
        "in_sample": is_sum | {"until": M._iso(cut)},
        "out_of_sample": oos_sum | {"from": M._iso(cut)},
        "walk_forward": wf,
        "by_ticker": M.group(trades, "ticker", risk),
        "by_strategy": M.group(trades, "strategy", risk),
        "by_timeframe": M.group(trades, "timeframe", risk),
        "by_direction": M.group(trades, "direction", risk),
        "by_mode": M.group(trades, "mode", risk),
        "by_regime": M.group(trades, "regime", risk),
        "portfolio": {k: v for k, v in port.items() if k != "per_trade"},
        "quality": M.quality_analysis(trades),
        "equity": M.equity_curve(trades, risk),
        "verdict": M.verdict(overall, oos_sum, wf, run["is_demo"], run["survivorship_biased"],
                             cfg.min_trades_for_evidence, cfg.min_oos_trades_for_evidence),
        "skipped_reasons": dict(sorted(skip_reasons.items(), key=lambda kv: -kv[1])),
        "trades": trades,
    }
