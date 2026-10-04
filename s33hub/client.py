"""For sites: read the hub instead of the original sources.

    from s33hub.client import fetch_snapshot, hub_engine, fetch_mirror
    path = fetch_snapshot("data/hub.db")          # downloads only when the hub has a new snapshot
    eng = hub_engine(path)                         # read-only SQLAlchemy engine on the hub tables
    doc, prov = fetch_mirror("planning_npad_dc")   # raw mirror body + where it came from

Bind the hub models (s33hub.models, on HubBase) to `hub_engine` and your own models to your
own database: sessionmaker(binds={HubBase: hub_eng, YourBase: site_eng}).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path

import httpx

from .config import PUBLIC_BASE
from .db import make_engine

UA = {"User-Agent": "Solas33Site/0.1 (+https://solas33.com)"}


def _get(url: str, timeout: float = 120) -> httpx.Response:
    last = None
    for _ in range(3):
        try:
            r = httpx.get(url, timeout=timeout, headers=UA, follow_redirects=True)
            r.raise_for_status()
            return r
        except httpx.HTTPError as e:
            last = e
    raise last  # type: ignore[misc]


def snapshot_info(base: str = PUBLIC_BASE) -> dict:
    return _get(f"{base}/v1/hub.sqlite.json").json()


def fetch_snapshot(dest: str | Path, base: str = PUBLIC_BASE, force: bool = False) -> Path:
    """Download and unpack the hub snapshot to `dest` unless the local copy already matches.
    Writes `<dest>.json` with the snapshot's generated_at and sha256 for provenance."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    meta_path = dest.with_suffix(dest.suffix + ".json")
    info = snapshot_info(base)
    if not force and dest.exists() and meta_path.exists() and json.loads(meta_path.read_text()).get("sha256") == info.get("sha256"):
        return dest
    raw = _get(f"{base}/v1/hub.sqlite.gz", timeout=300).content
    digest = hashlib.sha256(raw).hexdigest()
    if info.get("sha256") and digest != info["sha256"]:
        # the snapshot was replaced between the two requests - take what we got, record its real hash
        info = dict(info, sha256=digest, note="snapshot changed during download")
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(gzip.decompress(raw))
    os.replace(tmp, dest)
    meta_path.write_text(json.dumps(dict(info, sha256=digest, source=f"{base}/v1/hub.sqlite.gz")))
    return dest


def snapshot_meta(dest: str | Path) -> dict:
    p = Path(dest)
    m = p.with_suffix(p.suffix + ".json")
    return json.loads(m.read_text()) if m.exists() else {}


def hub_engine(path: str | Path):
    return make_engine(f"sqlite:///{Path(path).as_posix()}")


def fetch_mirror(key: str, ext: str = "json", base: str = PUBLIC_BASE) -> tuple[object, dict]:
    """(parsed body, provenance). JSON mirrors return the combined document; CSV returns text."""
    url = f"{base}/v1/mirror/{key}/latest.{ext}"
    r = _get(url)
    prov = dict(url=url, sha256=hashlib.sha256(r.content).hexdigest(), bytes=len(r.content))
    if ext == "json":
        doc = r.json()
        prov.update(source_url=doc.get("source_url"), api=doc.get("api"), retrieved_at=doc.get("retrieved_at"), hub_fetch_ids=doc.get("fetch_ids"))
        return doc, prov
    return r.content.decode("utf-8-sig"), prov
