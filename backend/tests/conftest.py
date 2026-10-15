from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.data.demo import DemoSource
from app.db import Database
from app.main import Service, create_app
from app.market.calendar import NY
from app.market.service import MarketDataService

@pytest.fixture(autouse=True)
def _isolated_raw_cache(tmp_path, monkeypatch):
    """Tests never read or write the real raw-bar cache (fake 'alpaca' sources would
    otherwise be persisted and restored into later runs)."""
    monkeypatch.setenv("RAW_CACHE_DIR", str(tmp_path / "rawcache"))
    monkeypatch.setenv("RAW_CACHE_PERSIST", "false")
    monkeypatch.setenv("UNIVERSE_SCAN_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("UNIVERSE_RETRY_PAUSE_SECONDS", "0")
    monkeypatch.setenv("ALLOWED_HOSTS", "testserver")  # TestClient's Host header


# Demo data ends at the last completed session before this instant (Tue 2026-03-10 close).
FIXED_NOW = datetime(2026, 3, 10, 18, 0, tzinfo=NY)
WATCHLIST = ["SPY", "QQQ", "NVDA", "TSLA", "AAPL", "AMD", "MSFT", "META", "AMZN"]


def demo_market() -> MarketDataService:
    return MarketDataService(DemoSource(now=FIXED_NOW), symbols=WATCHLIST,
                             now_fn=lambda: FIXED_NOW)


@pytest.fixture
def settings(tmp_path):
    return Settings(database_path=tmp_path / "test.db", scan_interval_seconds=0)


@pytest.fixture
def service(settings):
    return Service(settings, market=demo_market(), db=Database(settings.database_path))


@pytest.fixture
def client(service):
    with TestClient(create_app(service, run_scanner=False)) as c:
        yield c
