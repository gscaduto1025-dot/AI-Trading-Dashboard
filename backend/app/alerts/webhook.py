"""Optional webhook delivery so alerts reach you while the dashboard is closed.

Set ALERT_WEBHOOK_URL to any endpoint that accepts a JSON POST (a Slack / Discord /
Teams incoming webhook, ntfy.sh, a Zapier hook or your own service). The URL can contain a
secret token, so it is never logged or returned by the API; only its host is shown.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

log = logging.getLogger("tradescope.alerts")


def _text(a: dict) -> str:
    parts = [a["title"], a["message"]]
    if a.get("entry_conditions"):
        parts.append("Conditions: " + "; ".join(c["name"] for c in a["entry_conditions"]))
    f = a.get("feed_status") or {}
    parts.append(f"Data: {f.get('feed')} ({f.get('freshness')}), candle {a.get('candle_time')}"
                 f" - {a['timestamp']}")
    parts.append("Analysis only. TradeScope AI never places orders.")
    return "\n".join(p for p in parts if p)


class WebhookNotifier:
    def __init__(self, url: str, timeout: float = 5.0, client: httpx.Client | None = None) -> None:
        self._url = url
        self.host = urlparse(url).hostname or "?"
        self._client = client or httpx.Client(timeout=timeout)

    def deliver(self, alert: dict) -> str:
        """POST the alert; returns a short delivery status stored with the alert."""
        body = {"text": _text(alert), "content": _text(alert), "alert": alert}
        try:
            r = self._client.post(self._url, json=body)
        except httpx.HTTPError as exc:
            log.warning("Alert webhook to %s failed: %s", self.host, type(exc).__name__)
            return f"webhook failed: {type(exc).__name__}"
        if r.status_code >= 300:
            log.warning("Alert webhook to %s returned HTTP %s", self.host, r.status_code)
            return f"webhook HTTP {r.status_code}"
        return "webhook sent"
