"""Runtime configuration, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent

DEFAULT_WATCHLIST = ["SPY", "QQQ", "NVDA", "TSLA", "AAPL", "AMD", "MSFT", "META", "AMZN"]


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so the project has no extra dependency for it.

    Existing environment variables always win over values in the file.
    """
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    # Secrets are excluded from repr() so they can't leak into logs or tracebacks.
    alpaca_key_id: str = field(default="", repr=False)
    alpaca_secret_key: str = field(default="", repr=False)
    alpaca_data_url: str = "https://data.alpaca.markets"
    alpaca_trading_url: str = "https://paper-api.alpaca.markets"  # only /v2/calendar is used
    alpaca_feed: str = "iex"
    alpaca_adjustment: str = "split"
    alpaca_rate_limit_per_min: int = 190
    alpaca_max_retries: int = 3
    alpaca_timeout_seconds: float = 15.0
    anthropic_api_key_present: bool = False
    claude_model: str = "claude-opus-5-5"
    claude_effort: str = "medium"
    database_path: Path = BACKEND_DIR / "tradescope.db"
    watchlist: list[str] = field(default_factory=lambda: list(DEFAULT_WATCHLIST))
    scan_interval_seconds: int = 300
    cors_origins: list[str] = field(default_factory=lambda: ["http://localhost:3000"])

    @property
    def alpaca_enabled(self) -> bool:
        return bool(self.alpaca_key_id and self.alpaca_secret_key)

    @property
    def data_mode(self) -> str:
        return "alpaca" if self.alpaca_enabled else "demo"


def load_settings() -> Settings:
    _load_dotenv(BACKEND_DIR / ".env")
    watchlist = [
        s.strip().upper()
        for s in os.environ.get("WATCHLIST", ",".join(DEFAULT_WATCHLIST)).split(",")
        if s.strip()
    ]
    db_path = os.environ.get("DATABASE_PATH")
    env = os.environ.get
    return Settings(
        alpaca_key_id=(env("ALPACA_API_KEY_ID") or env("APCA_API_KEY_ID") or "").strip(),
        alpaca_secret_key=(env("ALPACA_API_SECRET_KEY") or env("APCA_API_SECRET_KEY") or "").strip(),
        alpaca_data_url=env("ALPACA_DATA_URL", "https://data.alpaca.markets"),
        alpaca_trading_url=env("ALPACA_TRADING_URL", "https://paper-api.alpaca.markets"),
        alpaca_feed=env("ALPACA_FEED", "iex").strip().lower(),
        alpaca_adjustment=env("ALPACA_ADJUSTMENT", "split").strip().lower(),
        alpaca_rate_limit_per_min=int(env("ALPACA_RATE_LIMIT_PER_MIN", "190")),
        alpaca_max_retries=int(env("ALPACA_MAX_RETRIES", "3")),
        alpaca_timeout_seconds=float(env("ALPACA_TIMEOUT_SECONDS", "15")),
        anthropic_api_key_present=bool(
            os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        ),
        claude_model=os.environ.get("CLAUDE_MODEL", "claude-opus-5-5"),
        claude_effort=os.environ.get("CLAUDE_EFFORT", "medium"),
        database_path=Path(db_path) if db_path else BACKEND_DIR / "tradescope.db",
        watchlist=watchlist,
        scan_interval_seconds=int(os.environ.get("SCAN_INTERVAL_SECONDS", "300")),
        cors_origins=[
            o.strip()
            for o in os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",")
            if o.strip()
        ],
    )
