"""Hub database plumbing. The hub owns one SQLite file; sites read a published snapshot of it
(see s33hub.client). Hub tables hang off `HubBase` so a site can bind them to the snapshot
while keeping its own tables in its own database."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import settings


class HubBase(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """Naive UTC 'now' - every hub timestamp is naive UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def naive(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t.astimezone(timezone.utc).replace(tzinfo=None) if t.tzinfo else t


def make_engine(url: str):
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url or url in ("sqlite://", "sqlite:///"):
            kwargs["poolclass"] = StaticPool
    eng = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _pragmas(conn, _):  # noqa: ANN001
            cur = conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
    return eng


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db(eng=None) -> None:
    from . import models  # noqa: F401
    eng = eng or engine
    if str(eng.url).startswith("sqlite:///") and ":memory:" not in str(eng.url):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
    HubBase.metadata.create_all(eng)
    migrate(eng)


def migrate(eng) -> None:
    """Additive, nullable column changes only - by design."""
    insp = inspect(eng)
    with eng.begin() as conn:
        for table in HubBase.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(eng.dialect)}'))
