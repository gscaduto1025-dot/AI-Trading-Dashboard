# Opening the TradeScope AI dashboard

TradeScope AI is two programs that run together:

| Program | Port | Who can reach it |
| --- | --- | --- |
| Backend (FastAPI): market data, scanners, key levels, trade log | 8000 | Only the computer it runs on (`127.0.0.1`) |
| Dashboard (Next.js): the web page you use | 3000 | Your browser; with `--lan`, also your phone on the same Wi-Fi |

The browser only talks to the dashboard. The dashboard passes `/api/...` requests to the
backend on the same computer, so the backend and your API keys never face the network.

## Why "localhost" did not open

`http://localhost:3000` means "port 3000 on **this** computer". When the app runs inside
a Claude Code cloud session, it runs on a cloud container, and `localhost` there is not your
computer. Claude Code cloud sessions do not forward ports to your browser, so that address
never reaches the container. To use the dashboard yourself, run it on your own computer.
It is free, private, and uses your own Alpaca keys.

## One-time setup (about 10 minutes)

You need:
- **Python 3.11 or newer**: <https://www.python.org/downloads/> (on Windows, tick
  "Add python.exe to PATH" in the installer);
- **Node.js 20.9 or newer (LTS)**: <https://nodejs.org/>;
- **Git**: <https://git-scm.com/downloads>;
- your **Alpaca API key and secret**: <https://app.alpaca.markets> → API Keys. The free
  plan works. These are market-data keys; TradeScope never places orders.

Get the code (Terminal on macOS, PowerShell on Windows):

```bash
git clone https://github.com/gscaduto1025-dot/AI-Trading-Dashboard.git
cd AI-Trading-Dashboard
git checkout claude/exciting-gauss-kwqte7
```

If Git asks you to sign in, use your GitHub account.

Add your keys. Copy `backend/.env.example` to `backend/.env`, open `backend/.env` in a
text editor, and fill in these two lines:

```
ALPACA_API_KEY_ID=your-key-id
ALPACA_API_SECRET_KEY=your-secret-key
```

`backend/.env` is git-ignored: it stays on your computer and is never uploaded. Do not paste
your keys into chats, issues or screenshots. Without keys the app runs on clearly labelled
DEMO DATA. `ANTHROPIC_API_KEY` is optional: without it, explanations are rules-based and
labelled as such.

## Start it

### macOS or Linux

```bash
./scripts/start.sh          # this computer only
./scripts/start.sh --lan    # also reachable from your phone on the same Wi-Fi
```

The first run installs everything and builds the dashboard, which takes a few minutes. Then
open **<http://localhost:3000>**. Stop it with Ctrl+C in that terminal window.

### Windows (PowerShell, two windows)

The `.sh` scripts need bash, so on Windows run the two programs by hand. First time only,
from the `AI-Trading-Dashboard` folder:

```powershell
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
cd ..\frontend
npm install
npm run build
cd ..
```

Then, every time:

```powershell
# Window 1: backend
cd AI-Trading-Dashboard\backend
.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1

# Window 2: dashboard (add --hostname 0.0.0.0 for phone access on your Wi-Fi)
cd AI-Trading-Dashboard\frontend
npx next start --port 3000
```

Open **<http://localhost:3000>**. Windows may ask whether Node.js may use the network.
Allow private networks only.

## What to expect when it opens

- The top-right badge says **ALPACA IEX** (green) with real keys, or **DEMO FEED** without.
  It shows **Connecting…** for a moment while the backend starts.
- The first load after starting takes about 1–2 minutes while candles download (the free
  Alpaca plan allows about 200 requests a minute). Later starts are faster because downloaded
  candles are cached in `backend/data/cache`.
- If a price says **stale** or a panel says **MARKET DATA UNAVAILABLE**, the newest candle
  from Alpaca is too old during an open session. TradeScope then refuses to compute
  intraday levels and plans instead of guessing. Swing mode (daily/weekly candles) keeps
  working.

## Phone access

- **Same Wi-Fi:** start with `./scripts/start.sh --lan` (or `--hostname 0.0.0.0` on
  Windows), then open `http://<your computer's IP>:3000` on the phone. The script prints
  the address. The dashboard has no login, so only do this on a network you trust.
- **Away from home:** the dashboard must be reachable over the internet. A private option
  is a personal VPN such as Tailscale (free for personal use), which links your phone and
  computer without making anything public. Add the name or 100.x.y.z address you open it by to
  `ALLOWED_HOSTS` in `backend/.env`; the backend only answers to `localhost` and
  private-network addresses by default. A hosted server also works but costs money and
  needs a login added first. See [DEPLOYMENT.md](DEPLOYMENT.md). Neither is set up.

## Does it keep running?

It runs only while the terminal window running it stays open and the computer is awake.
The scanners, alerts and market scanner run inside the backend, so they pause when it
stops. Your trade log is saved in `backend/tradescope.db` and survives restarts.
