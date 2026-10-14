# Deploying TradeScope AI for reliable alerts

TradeScope AI is an analysis tool. It never places orders, and nothing in this guide
connects it to a brokerage account. This guide covers what is needed for the server-side
scanner and alert engine to keep producing correct alerts while nobody has the dashboard
open.

## How alerts are produced

```
scheduler loop (inside the FastAPI process, every SCAN_INTERVAL_SECONDS)
   └─ scanner.due()?  ── only when a new 5-minute / daily candle can exist, or the watchlist changed
        └─ scanner.run()        strategy + planning engines → snapshot
             └─ alerts.process()  diff vs. state in SQLite → alert_events (+ webhook)
browser (optional)  ── polls GET /api/alerts every 20 s → panel + browser notifications
```

- The **scheduler, scanner and alert engine all run in the backend process**. A browser is
  not needed to create alerts; it only displays them.
- Browser notifications only fire while a dashboard tab is open. They use the Notification
  API, which needs HTTPS or `localhost`. For delivery while the dashboard is closed, set
  `ALERT_WEBHOOK_URL` (see below).

## Minimum reliable setup (single server)

1. **One always-on host.** Use a small VM or container; 1 vCPU and 1 GB of RAM is enough
   for the default 9-ticker watchlist. Sleeping laptops and serverless platforms that scale
   to zero will stop the scheduler.
2. **Run the backend as a supervised service** with a single process:
   ```bash
   cd backend
   .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
   ```
   - **Use `--workers 1`.** Each worker would run its own scheduler.
   - Duplicate alerts are still impossible with more workers: the `dedupe_key` is `UNIQUE`,
     so two workers cannot store the same alert. But they would waste market-data API calls.
   - Do not use `--reload` in production.

   Example systemd unit:
   ```ini
   [Unit]
   Description=TradeScope AI backend
   After=network-online.target
   Wants=network-online.target

   [Service]
   WorkingDirectory=/opt/tradescope/backend
   ExecStart=/opt/tradescope/backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
   Restart=always
   RestartSec=5
   User=tradescope
   # backend/.env holds the secrets; keep it readable by this user only (chmod 600).

   [Install]
   WantedBy=multi-user.target
   ```
   With Docker, use `restart: unless-stopped` and mount the database directory as a volume.
3. **Persistent storage for SQLite.**
   - Set `DATABASE_PATH` to a file on a persistent disk or volume. It holds alerts, alert
     state, preferences, price levels, the watchlist, the signal log and the AI cache.
   - If this file is lost, the engine starts with an empty state. On the first scan it
     re-alerts setups that are triggered *now*, but never past events.
   - Back it up regularly. With `sqlite3`: `sqlite3 tradescope.db ".backup backup.db"`.
     Copying a live file is not a safe backup.
   - Keep it on a local disk, not a network share: SQLite locking over NFS/SMB is
     unreliable.
4. **Accurate clock.**
   - Run NTP (chrony or systemd-timesyncd).
   - Candle completion, the scan schedule, freshness checks and cooldowns all depend on
     the system clock.
   - Market-session logic uses `America/New_York` internally, so the host timezone does
     not matter. The clock must still be correct.
5. **HTTPS reverse proxy** for the dashboard (Caddy, nginx or a managed load balancer).
   Browser notifications need a secure context. Build the frontend with `npm run build`
   and serve it with `npm start`, proxying `/api` to the backend. Set `CORS_ORIGINS` to
   the public origin.
6. **Restrict access.**
   - The API has no authentication, so do not expose port 8000 to the internet.
   - Put the dashboard behind your proxy's authentication (basic auth, an OAuth proxy,
     Tailscale or a VPN). Alert preferences and price levels can be changed by anyone who
     can reach the API.

## Market data

- **Real-time alerts need a real-time feed.**
  - Alpaca's free `iex` feed is real-time but covers only the IEX exchange.
  - `delayed_sip` is 15 minutes delayed. Alerts carry the feed and its latency in
    `feed_status`, and the dashboard labels it.
  - `sip` (paid) is the full consolidated tape.
- **Rate limits.**
  - The client's token bucket (`ALPACA_RATE_LIMIT_PER_MIN`, default 190 for the free tier
    of 200/min) paces every request, and candle sets are cached.
  - The watchlist is capped at 30 tickers so a full scan fits inside the limit.
  - Rescans happen only when a new eligible candle exists.
- **Outages are expected.** When Alpaca is unreachable or data goes stale:
  - each affected ticker raises one `data_stale` warning;
  - every other alert for that ticker is suppressed;
  - no setup is "invalidated" by the outage;
  - alerts resume automatically when fresh data returns.

## Delivery while the dashboard is closed: webhook

Set `ALERT_WEBHOOK_URL` in `backend/.env` and restart the backend. Every alert whose type
is listed in *Settings → webhook types* (`webhook_types`) is POSTed as JSON:

```json
{"text": "...human-readable summary...", "content": "...same...", "alert": { ...full alert... }}
```

`text` suits Slack and Mattermost incoming webhooks. `content` suits Discord. `alert`
carries every field for custom receivers, such as ntfy.sh, Pushover via Zapier or your own
service.

- **Timeouts and failures:** the request times out after 5 s. Failures are logged by host
  only, and the result is stored with the alert (`delivery`: `webhook sent` /
  `webhook HTTP 500` / `webhook failed: ...`).
- **No retries:** delivery is attempted once. If you need guaranteed delivery, point the
  webhook at a queue (for example a small service that stores and retries), or poll
  `GET /api/alerts?after_id=<last id>` from your own job.
- **Secret URL:** the URL is treated as a secret. It is excluded from `repr`, never logged
  and never returned by the API; `/api/alerts/preferences` shows only the host.

## Monitoring

`GET /api/health` returns a heartbeat:

- `scanner.scan_count`, `scanner.last_finished_at`, `scanner.last_error`, `scanner.due`;
- `alerts.last_run` (time, scan id, alerts created, alerts suppressed), unread counts and
  whether the webhook is configured;
- `feed` and `is_demo`. Make sure production is **not** running on demo data
  (`data_mode` must be `alpaca`).

Alert from your uptime monitor (UptimeRobot, Healthchecks, Prometheus blackbox) when:

- `/api/health` fails;
- `scanner.last_error` is non-null;
- `scanner.last_finished_at` is older than about 10 minutes during regular trading hours.

Logs go to stdout/journald under the `tradescope.*` loggers.

## Scaling beyond one server

The single-process design is deliberate: one scheduler and one SQLite file. To run
several replicas or many users:

- Move persistence to **PostgreSQL**. The `alert_events.dedupe_key` uniqueness and
  `INSERT … ON CONFLICT DO NOTHING` semantics carry over directly.
- Run the scan + alert job as a **single dedicated worker**: one replica, a leader lock
  such as a Postgres advisory lock, or an external scheduler (cron, Cloud Scheduler)
  calling `POST /api/scanner/run`. Start the API replicas with `SCAN_INTERVAL_SECONDS=0`
  (disables their scheduler loop).
- Fan out delivery through a **queue** (SQS, Redis streams, Pub/Sub) with retries. Web
  Push (VAPID + a service worker) would deliver browser notifications with the tab closed.
- Add authentication and per-user preferences (today preferences are global to the
  installation).

## Checklist

- [ ] `backend/.env` has Alpaca keys and `chmod 600`; `data_mode` in `/api/health` is `alpaca`
- [ ] Backend runs under systemd/Docker with automatic restart and `--workers 1`
- [ ] `DATABASE_PATH` is on persistent storage, with scheduled `.backup` copies
- [ ] NTP is active
- [ ] Dashboard served over HTTPS behind authentication; port 8000 is not public
- [ ] `ALERT_WEBHOOK_URL` set and a test alert received; webhook types chosen in Settings
- [ ] Uptime check on `/api/health` (status, `scanner.last_error`, scan recency)
