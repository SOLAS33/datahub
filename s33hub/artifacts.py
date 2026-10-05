"""Files a collector produces that are too large or too structured to keep as database rows.

A collector calls `put()` during its run; `publish.build` adds whatever was put this run to the
files it uploads. Files that are not produced on a given run stay where they are on R2 (the
`published` table and the catalogue remember them), so a source that changes quarterly is only
re-uploaded when it changes. Nothing here is persisted between runs by itself: a collector
decides, from a small state record, whether it has anything new to put.
"""
from __future__ import annotations

ARTIFACTS: dict[str, dict] = {}


def put(path: str, data: bytes, ctype: str, rows: int | None = None, desc: str = "", source: str = "") -> None:
    ARTIFACTS[path] = dict(data=data, type=ctype, rows=rows, desc=desc, source=source)


def clear() -> None:
    ARTIFACTS.clear()
