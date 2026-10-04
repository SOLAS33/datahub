"""Hub settings, from HUB_* environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_BASE = os.environ.get("HUB_PUBLIC_BASE", "https://data.solas33.com")
BUCKET = os.environ.get("HUB_BUCKET", "solas33-datahub")


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("HUB_DATA_DIR", ROOT / "data")))
    database_url: str = ""
    contact: str = os.environ.get("HUB_CONTACT", "hello@solas33.com")
    http_timeout: float = float(os.environ.get("HUB_HTTP_TIMEOUT", "60"))
    backfill_days: int = int(os.environ.get("HUB_BACKFILL_DAYS", "400"))
    keep_weather_hours: int = int(os.environ.get("HUB_KEEP_WEATHER_HOURS", "72"))

    def __post_init__(self) -> None:
        if not self.database_url:
            self.database_url = os.environ.get("HUB_DATABASE_URL", f"sqlite:///{(self.data_dir / 'hub.db').as_posix()}")

    @property
    def user_agent(self) -> str:
        return f"Solas33DataHub/0.1 (+{PUBLIC_BASE}; public-data research; contact: {self.contact})"


settings = Settings()
