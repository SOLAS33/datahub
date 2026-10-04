"""Collector framework: a collector fetches one public source, stores what it finds, logs
every payload (FetchLog) and reports counts. `run_collector` records each run, turns
failure/recovery transitions into events, and never lets one bad source stop the others."""
from __future__ import annotations

from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import naive, utcnow
from ..models import FetchLog, HubEvent, SourceRun


@dataclass
class Result:
    fetched: int = 0
    created: int = 0
    changed: int = 0
    message: str = ""


class Collector:
    key: str = ""
    name: str = ""
    url: str = ""          # human landing page
    licence: str = ""
    publisher: str = ""
    provides: str = ""
    interval_hours: float = 3.0
    used_by: tuple[str, ...] = ()   # which sites consume it (documentation for the catalogue)

    def run(self, session: Session, client: httpx.Client) -> Result:  # pragma: no cover - abstract
        raise NotImplementedError


def make_client() -> httpx.Client:
    return httpx.Client(timeout=settings.http_timeout, follow_redirects=True, headers={"User-Agent": settings.user_agent})


def get_with_retry(client: httpx.Client, url: str, params: dict | None = None, tries: int = 3) -> httpx.Response:
    import time
    last: Exception | None = None
    for attempt in range(tries):
        try:
            r = client.get(url, params=params)
            r.raise_for_status()
            return r
        except (httpx.HTTPError, httpx.TransportError) as e:
            last = e
            if attempt < tries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise last  # type: ignore[misc]


def log_fetch(session: Session, source: str, url: str, sha: str, size: int, licence: str | None = None, issued_at=None,
              model_runs=None, note: str | None = None, retrieved_at=None) -> FetchLog:
    row = FetchLog(source=source, url=url[:600], sha256=sha, bytes=size, licence=licence, issued_at=naive(issued_at),
                   model_runs=model_runs, note=note, retrieved_at=naive(retrieved_at) or utcnow())
    session.add(row)
    session.flush()
    return row


def log_provenance(session: Session, prov) -> FetchLog:
    """Record an s33weather Provenance."""
    return log_fetch(session, prov.source, prov.url, prov.sha256, prov.bytes, prov.licence, prov.issued_at, prov.model_runs,
                     prov.notes or None, prov.retrieved_at)


def add_event(session: Session, kind: str, summary: str, key: str | None = None, detail: str | None = None,
              source_url: str | None = None, event_date=None, source: str | None = None) -> bool:
    """Record an event once (by key). Returns True if it was new."""
    if key and session.scalar(select(HubEvent.id).where(HubEvent.key == key)):
        return False
    session.add(HubEvent(kind=kind, key=key, summary=summary, detail=detail, source_url=source_url, event_date=naive(event_date), source=source))
    return True


def run_collector(session: Session, collector: Collector, client: httpx.Client | None = None) -> SourceRun:
    prev = last_run(session, collector.key)
    run = SourceRun(source=collector.key)
    session.add(run)
    session.commit()
    own = client is None
    client = client or make_client()
    try:
        res = collector.run(session, client)
        session.commit()
        run.ok, run.fetched, run.created, run.changed, run.message = True, res.fetched, res.created, res.changed, res.message
    except Exception as e:  # noqa: BLE001 - recorded, never raised
        session.rollback()
        run.ok, run.message = False, f"{type(e).__name__}: {e}"[:1000]
    finally:
        if own:
            client.close()
        run.finished_at = utcnow()
        session.add(run)
        if prev is not None and prev.ok != run.ok:
            kind = "source_recovered" if run.ok else "source_failed"
            session.add(HubEvent(kind=kind, source=collector.key, source_url=collector.url or None,
                                 summary=f"Source {collector.name} {'recovered' if run.ok else 'failed'}"
                                 + ("" if run.ok else f": {(run.message or '')[:140]}")))
        session.commit()
    return run


def last_run(session: Session, key: str, ok_only: bool = False) -> SourceRun | None:
    q = select(SourceRun).where(SourceRun.source == key)
    if ok_only:
        q = q.where(SourceRun.ok.is_(True))
    return session.scalars(q.order_by(SourceRun.started_at.desc()).limit(1)).first()
