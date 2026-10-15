"""Request guard for the unauthenticated, locally run API.

The API has no login, so it relies on being reachable only from this computer (and, with
`start.sh --lan`, the home network). Two browser attacks still reach a local server:

- Cross-site requests: any web page can make the browser send a request to
  http://localhost:3000/api/... or http://127.0.0.1:8000/api/... (CORS only hides the
  reply). Browsers label these with `Sec-Fetch-Site: cross-site` / `same-site`, and the
  dashboard's proxy passes the header on, so they are refused.
- DNS rebinding: a page on attacker.example re-points its name to 127.0.0.1 and becomes
  "same-origin". The Host header still names attacker.example, so only local names and
  IP literals (plus `ALLOWED_HOSTS`) are accepted. Behind the dashboard's proxy the
  browser's Host arrives as `X-Forwarded-Host`.

Requests without browser headers (curl, scripts, tests) are unaffected. This does not
replace authentication for anything reachable from an untrusted network.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping

BLOCKED_FETCH_SITES = ("cross-site", "same-site")


def extra_hosts() -> frozenset[str]:
    """Additional host names from ALLOWED_HOSTS (comma list; `*` disables the check)."""
    return frozenset(h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",")
                     if h.strip())


def _hostname(value: str) -> str:
    value = value.split(",")[0].strip().lower()
    if value.startswith("["):  # [::1]:8000
        return value[1:value.find("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def host_ok(host: str, extra: frozenset[str]) -> bool:
    name = _hostname(host)
    if "*" in extra or name in extra or name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def reject_reason(headers: Mapping[str, str], extra: frozenset[str]) -> str | None:
    """Why the request must be refused, or None."""
    site = headers.get("sec-fetch-site", "").lower()
    if site in BLOCKED_FETCH_SITES:
        return ("Cross-site request refused: TradeScope's API only answers its own "
                "dashboard.")
    host = headers.get("x-forwarded-host") or headers.get("host") or ""
    if host and not host_ok(host, extra):
        return (f"Host '{_hostname(host)}' is not allowed. Open the dashboard via localhost "
                f"or this computer's IP, or add the name to ALLOWED_HOSTS.")
    return None
