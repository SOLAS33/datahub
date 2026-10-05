"""Shared helpers for the "files" tier: sources whose output is a published table or file rather than database rows.

A files-tier collector keeps a small JSON state record (per item: the SHA-256 of what it last published, when it changed)
in a Mirror row named after the collector. That record, not the files, is what persists between runs; the files live on R2.
`publish_table` hands a file to the publisher only when its bytes changed, so an unchanged source costs no upload.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from s33weather.models import sha256

from .. import artifacts
from ..models import DailyStat, Mirror
from ..db import utcnow


def gz(data: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as g:   # mtime=0: same input, same bytes, same hash
        g.write(data)
    return buf.getvalue()


def csv_bytes(header: list[str], rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    return buf.getvalue().encode("utf-8")


def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def load_state(session: Session, key: str) -> dict:
    m = session.scalar(select(Mirror).where(Mirror.key == key))
    try:
        return json.loads(m.body) if m and m.body else {}
    except ValueError:
        return {}


def save_state(session: Session, key: str, state: dict, url: str, name: str):
    """Persist the state record (published as v1/mirror/<key>/latest.json, so it doubles as a per-source manifest)."""
    from .mirrors import store_mirror
    return store_mirror(session, key, json.dumps(state, sort_keys=True, default=str).encode(), "json", len(state.get("items", state)), [], url, name)


@dataclass
class Tally:
    """What a multi-item run did, for the run message."""
    published: int = 0
    unchanged: int = 0
    failed: list[str] = field(default_factory=list)

    def message(self, total: int) -> str:
        bad = f"; FAILED: {', '.join(self.failed[:8])}{' …' if len(self.failed) > 8 else ''}" if self.failed else ""
        return f"{self.published} published, {self.unchanged} unchanged of {total}{bad}"


def publish_table(state_items: dict, item_key: str, path: str, data: bytes, ctype: str, rows: int | None, desc: str, source: str, extra: dict | None = None) -> bool:
    """Put `data` on the publish list if its hash differs from what this item last published. Returns True if it did."""
    digest = sha256(data)
    prev = state_items.get(item_key, {})
    if prev.get("published_sha256") == digest and prev.get("path") == path:
        state_items[item_key] = dict(prev, checked_at=utcnow().isoformat(timespec="seconds"))
        return False
    artifacts.put(path, data, ctype, rows, desc, source)
    state_items[item_key] = dict(prev, path=path, published_sha256=digest, bytes=len(data), rows=rows, changed_at=utcnow().isoformat(timespec="seconds"),
                                 checked_at=utcnow().isoformat(timespec="seconds"), **(extra or {}))
    return True


# ---- daily aggregates (rows tier): sources that only expose "latest" values, accumulated into daily min/max/mean

def add_daily(session: Session, dataset: str, station: str, var: str, ts, value: float, unit: str = "") -> bool:
    """Fold one reading into its day. A reading at or before the day's last counted timestamp is ignored (idempotent re-runs)."""
    day = ts.date()
    row = session.scalar(select(DailyStat).where(DailyStat.dataset == dataset, DailyStat.station == station, DailyStat.var == var, DailyStat.day == day))
    if row is None:
        session.add(DailyStat(dataset=dataset, station=station, var=var, day=day, n=1, vmin=value, vmax=value, vsum=value, last_ts=ts, unit=unit))
        return True
    if row.last_ts is not None and ts <= row.last_ts:
        return False
    row.n += 1
    row.vmin, row.vmax, row.vsum, row.last_ts = min(row.vmin, value), max(row.vmax, value), row.vsum + value, ts
    return True


def daily_files(session: Session, dataset: str, path_prefix: str, source: str, desc: str) -> dict[str, dict]:
    """Published yearly files for one dataset: v1/<path_prefix>/<YYYY>.csv (day, station, var, n, min, max, mean, unit)."""
    out: dict[str, dict] = {}
    rows = session.scalars(select(DailyStat).where(DailyStat.dataset == dataset).order_by(DailyStat.day, DailyStat.station, DailyStat.var)).all()
    by_year: dict[int, list] = {}
    for r in rows:
        by_year.setdefault(r.day.year, []).append(r)
    for y, rs in by_year.items():
        data = csv_bytes(["day", "station", "variable", "readings", "min", "max", "mean", "unit"],
                         ([r.day.isoformat(), r.station, r.var, r.n, round(r.vmin, 4), round(r.vmax, 4), round(r.vsum / r.n, 4), r.unit] for r in rs))
        out[f"v1/{path_prefix}/{y}.csv"] = dict(data=data, type="text/csv", rows=len(rs), desc=desc, source=source)
    return out
