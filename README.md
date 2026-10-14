# TradeScope AI

An AI-assisted stock trading **analysis** dashboard for day and swing traders. It scans a
watchlist (SPY, QQQ, NVDA, TSLA, AAPL, AMD, MSFT, META, AMZN), finds support and resistance,
reads market bias, detects breakout / breakdown, trend-pullback and reversal setups, computes
entry, stop, targets and reward/risk, and says plainly when there is **no trade**. Claude
explains each analysis in plain language.

> **Analysis only.** TradeScope AI never places trades and has no brokerage/trading
> integration. Nothing here is financial advice.

> **Demo data.** Without Alpaca API keys, the app runs on **simulated DEMO DATA**. That's
> labelled everywhere: a banner, the watchlist, the chart watermark, the header, setups and
> alerts. Demo prices are not real and do not change in real time.

![stack](https://img.shields.io/badge/Next.js-16-black) ![stack](https://img.shields.io/badge/FastAPI-0.143-009688) ![stack](https://img.shields.io/badge/SQLite-local-blue)

## Quick start

Requirements: Python 3.11+, Node.js 20+.

```bash
./scripts/dev.sh
```

The first run creates `backend/.venv`, installs the Python and npm dependencies and copies
`backend/.env.example` to `backend/.env`. Then open:

- Dashboard: <http://localhost:3000>
- API docs (Swagger): <http://localhost:8000/docs>

### Manual start (two terminals)

```bash
# Terminal 1: backend
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env            # optional: add API keys
.venv/bin/uvicorn app.main:app --reload --port 8000

# Terminal 2: frontend
cd frontend
npm install
npm run dev                     # http://localhost:3000
```

The frontend proxies `/api/*` to the backend (see `frontend/next.config.ts`). To point at a
backend elsewhere, set `BACKEND_URL`, e.g. `BACKEND_URL=http://10.0.0.5:8000 npm run dev`.

### Connecting real services

Edit `backend/.env` and restart the backend:

| Variable | Purpose |
| --- | --- |
| `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` | Real market data from the Alpaca Market Data API. Read-only; free accounts work with `ALPACA_FEED=iex`. When set, every DEMO label disappears and the header shows **Alpaca**. |
| `ANTHROPIC_API_KEY` | Enables the **Claude AI Analysis** panel. |
| `CLAUDE_MODEL` | Defaults to `claude-opus-5-5`. |
| `CLAUDE_EFFORT` | `low` / `medium` (default) / `high` / `xhigh` / `max`. |
| `WATCHLIST` | Comma-separated symbols. |
| `SCAN_INTERVAL_SECONDS` | Background scanner interval (default 300, `0` disables it). |

If Alpaca is configured but fails, the API returns an error. It never silently falls back to
demo data.

## Tests and builds

```bash
./scripts/test.sh
```

This runs the backend pytest suite (indicators, demo data, the setup engine on hand-built price
series, API routes, alert de-duplication, the Claude integration with a stubbed client), then
the frontend typecheck, ESLint and the production `next build`.

## How it works

```
backend/app/
  config.py            env/.env settings
  data/base.py         Bar/Quote types, timeframes, provider protocol
  data/demo.py         deterministic simulated data (DEMO DATA)
  data/alpaca.py       Alpaca Market Data v2 (bars + snapshots)
  analysis/indicators.py  EMA, SMA, RSI, ATR, session VWAP, MACD
  analysis/levels.py   swing-pivot clustering + reference levels (prior day H/L/C, opening range, 20-bar/52-week H/L)
  analysis/engine.py   market bias, setup detectors, entries/stops/targets, checklist, status
  ai/claude.py         Claude explanation (structured JSON output)
  db.py                SQLite: detected setups, alerts, AI response cache
  main.py              FastAPI routes + background scanner
frontend/src/
  components/Dashboard.tsx   page layout and data flow
  components/PriceChart.tsx  TradingView Lightweight Charts (candles, volume, EMAs, VWAP, SMA200, levels, trade plan)
  components/*.tsx           watchlist, setup, bias, levels, AI, recent setups, alerts panels
```

### Modes and timeframes

| Mode | Timeframes | Indicators | Min reward/risk |
| --- | --- | --- | --- |
| Day Trading | 1m, 5m, 15m (regular session) | EMA 9/21, session VWAP, prior-day & opening-range levels | 1.5 : 1 |
| Swing Trading | 1H, 1D | EMA 20/50, SMA 200, 20-bar and 52-week high/low | 2.0 : 1 |

### Setup engine

- **Market bias** scores up to six factors (price vs slow EMA, fast vs slow EMA, slow EMA
  slope, RSI, price vs VWAP or SMA200, swing structure), each with an ATR dead-band so flat
  markets read as neutral.
- **Detectors**: breakout/breakdown through a key level (with the bias), trend pullback into
  an EMA or level, and reversal at a level with stretched RSI plus a hammer or engulfing candle.
- **Trade plan**: the entry is the trigger price, the stop sits beyond the level or pullback
  extreme, and the targets are the next levels in the trade direction, or 2R/3R when no level
  exists.
- **Status**:
  - **ACTIONABLE**: the trigger fired on a closed bar and every required check passes.
  - **FORMING**: the pattern is present but it's waiting on the trigger or on one required
    condition.
  - **NO TRADE**: no pattern, reward/risk too low, the move is already extended past the
    entry ("don't chase"), or too many conditions are missing. The panel explains why and
    which levels to watch.
- **Claude** gets the engine's numbers and explains them. It may not invent prices. The
  backend also enforces that its verdict is never more aggressive than the engine's status.
  Responses are cached in SQLite per bar so repeat views don't cost API calls. The request
  uses server-side refusal fallbacks (`fallbacks: "default"`).

### Alerts

The scanner runs on startup and every `SCAN_INTERVAL_SECONDS`, or when you press **Scan
watchlist**. It covers each symbol in both modes on the default timeframe (5m / 1D). An
alert fires when a symbol's setup starts forming or becomes actionable. Re-scans of unchanged
data don't duplicate alerts. Turn on **Notify** in the Alerts panel to get browser
notifications for actionable setups.

## API

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/health` | data mode (`demo`/`alpaca`), AI status, timeframes |
| GET | `/api/watchlist` | quotes with % change |
| GET | `/api/bars/{symbol}?timeframe=5Min` | OHLCV bars |
| GET | `/api/analysis/{symbol}?mode=day&timeframe=5Min` | bias, levels, setup, checklist, overlays |
| POST | `/api/ai/{symbol}?mode=day&timeframe=5Min[&refresh=true]` | Claude explanation |
| POST | `/api/scan` | scan the watchlist now |
| GET | `/api/setups/recent` | recently detected setups |
| GET | `/api/alerts` / POST `/api/alerts/{id}/ack` | alerts |
