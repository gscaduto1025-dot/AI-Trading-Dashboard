"""Low-level Alpaca REST client: auth, rate limiting, retries, error classification.

Read-only by construction: it only issues GET requests, against the Market Data API
(data.alpaca.markets) and the Trading API's /v2/calendar endpoint. It never calls order or
account endpoints.

Credentials are sent as the APCA-API-KEY-ID / APCA-API-SECRET-KEY headers and are never
logged or included in error messages.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable

import httpx

from ..data.base import (AuthError, DataError, InvalidRequest, ProviderUnavailable,
                         RateLimitError, SubscriptionError)

log = logging.getLogger(__name__)

MAX_PAGE_SIZE = 10_000  # Alpaca's maximum `limit` per request
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_BACKOFF_SECONDS = 30.0


class RateLimiter:
    """Token bucket shared by all requests. Alpaca's free plan allows 200 calls/minute."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.capacity = max(1, per_minute)
        self.rate = self.capacity / 60.0
        self.tokens = float(self.capacity)
        self._clock, self._sleep = clock, sleep
        self._last = clock()
        self._blocked_until = 0.0
        self._lock = threading.Lock()

    def block_until(self, monotonic_deadline: float) -> None:
        """Called when the server says the quota is exhausted."""
        with self._lock:
            self._blocked_until = max(self._blocked_until, monotonic_deadline)
            self.tokens = 0.0

    def acquire(self, max_wait: float = 15.0) -> None:
        while True:
            with self._lock:
                now = self._clock()
                self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
                self._last = now
                wait = max(self._blocked_until - now, 0.0)
                if wait == 0 and self.tokens >= 1:
                    self.tokens -= 1
                    return
                if wait == 0:
                    wait = (1 - self.tokens) / self.rate
            if wait > max_wait:
                raise RateLimitError(
                    f"Alpaca request quota exhausted; next request possible in {wait:.0f}s",
                    retryable=True)
            self._sleep(wait)
            max_wait -= wait


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


class AlpacaClient:
    def __init__(self, key_id: str, secret_key: str, *,
                 data_url: str = "https://data.alpaca.markets",
                 trading_url: str = "https://paper-api.alpaca.markets",
                 rate_limiter: RateLimiter | None = None, max_retries: int = 3,
                 timeout: float = 15.0, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time) -> None:
        if not key_id or not secret_key:
            raise AuthError("Alpaca API key ID and secret key are both required")
        self._secrets = (key_id, secret_key)
        headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key,
                   "Accept": "application/json", "User-Agent": "TradeScopeAI/0.2"}
        self._data = httpx.Client(base_url=data_url, headers=headers, timeout=timeout,
                                  transport=transport)
        self._trading = httpx.Client(base_url=trading_url, headers=headers, timeout=timeout,
                                     transport=transport)
        self.limiter = rate_limiter or RateLimiter(190, clock=clock, sleep=sleep)
        self.max_retries = max_retries
        self._sleep, self._clock, self._wall = sleep, clock, wall_clock
        self.requests_made = 0

    def close(self) -> None:
        self._data.close()
        self._trading.close()

    # ------------------------------------------------------------------ requests

    def get_data(self, path: str, params: dict) -> dict:
        return self._request(self._data, path, params)

    def get_trading(self, path: str, params: dict) -> dict | list:
        return self._request(self._trading, path, params)

    def _retry_delay(self, attempt: int, resp: httpx.Response | None) -> float:
        backoff = min(MAX_BACKOFF_SECONDS, 0.5 * 2 ** attempt) + random.uniform(0, 0.25)
        if resp is None:
            return backoff
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return min(MAX_BACKOFF_SECONDS, max(float(retry_after), 0.0))
            except ValueError:
                pass
        reset = resp.headers.get("X-RateLimit-Reset")
        if reset:
            try:
                return min(MAX_BACKOFF_SECONDS, max(float(reset) - self._wall(), 0.0) + 0.1)
            except ValueError:
                pass
        return backoff

    def _request(self, client: httpx.Client, path: str, params: dict):
        last_error: DataError | None = None
        for attempt in range(self.max_retries + 1):
            self.limiter.acquire()
            resp = None
            try:
                self.requests_made += 1
                resp = client.get(path, params=params)
            except httpx.TimeoutException:
                last_error = ProviderUnavailable("Alpaca request timed out", retryable=True)
            except httpx.TransportError as exc:
                last_error = ProviderUnavailable(
                    f"Could not connect to Alpaca ({type(exc).__name__})", retryable=True)
            else:
                remaining = resp.headers.get("X-RateLimit-Remaining")
                if remaining == "0" and resp.headers.get("X-RateLimit-Reset"):
                    try:
                        wait = float(resp.headers["X-RateLimit-Reset"]) - self._wall()
                        self.limiter.block_until(self._clock() + max(wait, 0.0))
                    except ValueError:
                        pass
                if resp.status_code == 200:
                    return resp.json()
                last_error = self._classify(resp)
                if resp.status_code == 429:
                    self.limiter.block_until(self._clock() + self._retry_delay(attempt, resp))

            if not last_error.retryable or attempt == self.max_retries:
                break
            delay = self._retry_delay(attempt, resp)
            log.warning("Alpaca %s failed (%s); retry %d/%d in %.1fs", path, last_error.code,
                        attempt + 1, self.max_retries, delay)
            self._sleep(delay)
        assert last_error is not None
        raise last_error

    def _classify(self, resp: httpx.Response) -> DataError:
        try:
            body = resp.json()
            message = body.get("message") if isinstance(body, dict) else str(body)
        except ValueError:
            message = resp.text[:200]
        message = _redact(str(message or resp.reason_phrase), self._secrets)
        code = resp.status_code
        if code == 401:
            return AuthError("Alpaca rejected the API credentials (401). Check ALPACA_API_KEY_ID "
                             "and ALPACA_API_SECRET_KEY.")
        if code == 403:
            return SubscriptionError(f"Alpaca denied access (403): {message}. Your market data "
                                     "subscription may not cover this feed or time range.")
        if code == 429:
            return RateLimitError("Alpaca rate limit reached (429)", retryable=True)
        if code in (400, 404, 422):
            return InvalidRequest(f"Alpaca rejected the request ({code}): {message}")
        if code in RETRYABLE_STATUS:
            return ProviderUnavailable(f"Alpaca server error ({code})", retryable=True)
        return DataError(f"Unexpected Alpaca response ({code}): {message}")
