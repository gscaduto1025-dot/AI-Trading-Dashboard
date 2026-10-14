from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.data.demo import DemoDataProvider
from app.db import Database
from app.main import Service, create_app

FIXED_DAY = date(2026, 3, 10)  # a Tuesday


@pytest.fixture
def settings(tmp_path):
    return Settings(database_path=tmp_path / "test.db", scan_interval_seconds=0)


@pytest.fixture
def service(settings):
    return Service(settings, provider=DemoDataProvider(today=FIXED_DAY),
                   db=Database(settings.database_path))


@pytest.fixture
def client(service):
    with TestClient(create_app(service, run_scanner=False)) as c:
        yield c
