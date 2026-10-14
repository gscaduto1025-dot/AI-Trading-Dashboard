# HTTP API reference

All routes are served by the FastAPI backend (interactive docs at `/docs`). The
frontend proxies `/api/*` to it. No route places orders.

## Routes

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/health` | data mode (`demo`/`alpaca`), feed, AI status, timeframes, scanner + alert-engine heartbeat |
| GET | `/api/market/status` | session phase, holiday/early close, next open/close, calendar source |
| GET | `/api/watchlist` | quotes: price, change, volume, last-trade time, latency |
| GET | `/api/bars/{symbol}?timeframe=5Min&session=regular\|extended&limit=300` | candles + completion + data status + quality report |
| GET | `/api/strategies/setups[?symbol=SPY&as_of=ISO]` | day-trading strategy setups (TRIGGERED / WATCHING / NO_TRADE) |
| GET | `/api/strategies/config` · `/api/strategies/signals` | strategy thresholds and rules · immutable trigger log |
| GET | `/api/swing/setups[?symbol=&as_of=&sort=quality]` | swing-trading scanner (daily/weekly; sortable) |
| GET | `/api/swing/config` | swing strategy thresholds and rules |
| GET | `/api/plans/{symbol}?mode=day\|swing[&as_of=]` | complete trade plans for a symbol (active first) |
| GET | `/api/planning/config` | trade-planning rules, thresholds and quality weights |
| GET | `/api/levels/{symbol}?mode=day&timeframe=5Min[&as_of=2026-03-05T12:00]` | automatic key levels with evidence, strength, validity (point-in-time replay with `as_of`) |
| GET | `/api/history/{symbol}?timeframe=1Day&start=2024-01-01&end=2024-12-31[&format=csv]` | full paginated history for backtesting (naive times = New York) |
| GET | `/api/analysis/{symbol}?mode=day&timeframe=5Min` | bias, levels, setup, checklist, overlays |
| GET | `/api/ai/{symbol}?mode=day\|swing[&setup_id=]` | cached Claude explanation + verified fact sheet (never calls Claude) |
| POST | `/api/ai/{symbol}?mode=day\|swing[&setup_id=&refresh=true]` | generate (or return cached) validated Claude explanation |
| GET | `/api/scanner[?mode=&side=&status=&min_rr=&min_quality=&strategy=&include_no_trade=]` | latest opportunity-scanner snapshot (server-side, filtered) |
| POST | `/api/scanner/run` · `/api/scan` | scan the watchlist now |
| POST / DELETE | `/api/watchlist` (`{"symbol": "IWM"}`) · `/api/watchlist/{symbol}` | add / remove a ticker (validated, persisted) |
| GET | `/api/setups/recent` | recently detected setups |
| GET | `/api/alerts[?limit=&unacknowledged=&ticker=&type=a,b&after_id=]` | alerts from the alert engine (newest first) + unread counts |
| POST | `/api/alerts/{id}/ack` · `/api/alerts/ack-all` | mark read |
| GET / PUT | `/api/alerts/preferences` (POST `/reset`) | alert types, preferences, webhook status |
| GET | `/api/options/{symbol}?mode=day\|swing[&setup_id=&expiration=YYYY-MM-DD]` | option contracts evaluated for a stock setup (analysis only) |
| GET / POST | `/api/backtests` · `/api/backtests/config` | list runs · start a run (background) · defaults and limitations |
| GET / DELETE | `/api/backtests/{id}[?strategy=&ticker=&timeframe=&direction=&mode=&regime=&start=&end=]` · `/api/backtests/{id}/trades.csv` | filtered metrics, portfolio and trades · CSV trade report |
| GET | `/api/backtests/{id}/trades/{trade_id}/chart` | candles around one SIMULATED trade (entry, stop, targets, exits) |
| GET | `/api/journal/{actual\|paper}` · `/api/journal/{account}/export.csv` | your trade log for one account: trades, fills, P&L, statistics |
| POST | `/api/journal/{account}/trades` (`{"ticker","direction","planned_stop?","planned_target?","strategy?","notes?","fills":[{"side","quantity","price","fees","executed_at"}]}`) | record a trade (manual; nothing is executed) |
| PATCH / DELETE | `/api/journal/{account}/trades/{id}` | edit notes / planned stop / target · delete a trade |
| POST / DELETE | `/api/journal/{account}/trades/{id}/fills` · `.../fills/{fill_id}` | add a fill (scale in, partial or full exit) · delete a fill (rejected if it would leave the trade inconsistent) |
| GET | `/api/options/capabilities` · `/api/options/config` | options feeds, fields, restrictions · filter thresholds and weights |
| GET / POST / DELETE | `/api/alerts/levels` (`{"ticker","price","condition?","note?"}`) · `/api/alerts/levels/{id}` | one-shot price-level alerts |

`account` must be `actual` or `paper`; anything else (for example `backtest`) is rejected
with 400. Simulated backtest trades are stored only inside backtest runs.
