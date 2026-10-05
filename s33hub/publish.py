"""Publish the hub to R2 (served at https://data.solas33.com by worker/worker.js).

Layout (all under v1/, CSV in UTC, every row traceable):
  hub.sqlite.gz                         full snapshot - what the Solas33 sites read
  catalog.json  status.json  events.json
  eirgrid/grid_15min/YYYY-MM.csv        ts_utc, series, region, value, unit, fetch_id
  eirgrid/forecast_snapshots/YYYY-MM.csv EirGrid's own forecasts as fetched (taken_at, ts, value)
  eirgrid/fuel_mix/YYYY-MM.csv
  eirgrid/dispatch_down/{monthly,regional,farms,workbooks}.csv   with workbook, sheet, cell
  weather/{points,stations}.csv  weather/forecast_latest.csv  weather/observations/YYYY-MM.csv  weather/warnings.json
  mirror/<key>/latest.<ext>  mirror/<key>/<date>-<sha12>.<ext>
  ledger/YYYY-MM.csv                     every payload: source, url, time, sha256, model runs

Only files whose bytes changed are uploaded (tracked in the `published` table).
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from s33weather import points

from . import artifacts
from .collectors import REGISTRY, last_run
from .collectors.tenders import ted_files
from .config import BUCKET, PUBLIC_BASE, settings
from .db import utcnow
from .models import (EVENT_LABELS, Farm, FetchLog, FuelMix, GridForecastSnap, GridReading, HubEvent, Mirror, MonthlyStat, ObservationRow,
                     Published, RegionalStat, WarningRow, WeatherStepRow, Workbook)

LICENCE = ("Derived/compiled data CC BY 4.0 - cite 'Solas33 Data Hub (data.solas33.com)' and the original publisher named in the "
           "catalogue; the original publishers' terms also apply.")


def _iso(t):
    return t.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z") if t else None


def _csv(header, rows) -> tuple[bytes, int]:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    n = 0
    for r in rows:
        w.writerow(["" if v is None else (_iso(v) if isinstance(v, datetime) else v) for v in r])
        n += 1
    return buf.getvalue().encode("utf-8"), n


def _months(session: Session, col, since: datetime | None = None) -> list[str]:
    q = select(col)
    if since:
        q = q.where(col >= since)
    lo = session.scalar(select(col).order_by(col).limit(1))
    hi = session.scalar(select(col).order_by(col.desc()).limit(1))
    if not lo:
        return []
    lo = max(lo, since) if since else lo
    out, y, m = [], lo.year, lo.month
    while (y, m) <= (hi.year, hi.month):
        out.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _month_range(ym: str) -> tuple[datetime, datetime]:
    y, m = map(int, ym.split("-"))
    start = datetime(y, m, 1)
    end = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)
    return start, end


def build(session: Session, months_back: int | None = None) -> dict[str, dict]:
    """{path: {data, type, rows, desc, source}}. months_back limits monthly partitions to recent ones."""
    files: dict[str, dict] = {}

    def add(path, data, ctype, rows=None, desc="", source=""):
        files[path] = dict(data=data, type=ctype, rows=rows, desc=desc, source=source)

    since = (utcnow() - timedelta(days=31 * months_back)).replace(day=1, hour=0, minute=0, second=0, microsecond=0) if months_back else None
    for ym in _months(session, GridReading.ts, since):
        a, b = _month_range(ym)
        data, n = _csv(["ts_utc", "series", "region", "value", "unit", "fetch_id"], session.execute(
            select(GridReading.ts, GridReading.series, GridReading.region, GridReading.value, GridReading.unit, GridReading.fetch_id)
            .where(GridReading.ts >= a, GridReading.ts < b).order_by(GridReading.ts, GridReading.series, GridReading.region)))
        add(f"v1/eirgrid/grid_15min/{ym}.csv", data, "text/csv", n, "EirGrid Smart Grid Dashboard 15-minute readings: wind, solar, demand (ALL/ROI/NI), "
            "SNSP, CO2 intensity, interconnector flows", "eirgrid_live")
    for ym in _months(session, GridForecastSnap.taken_at, since):
        a, b = _month_range(ym)
        data, n = _csv(["taken_at_utc", "ts_utc", "series", "region", "value", "fetch_id"], session.execute(
            select(GridForecastSnap.taken_at, GridForecastSnap.ts, GridForecastSnap.series, GridForecastSnap.region, GridForecastSnap.value,
                   GridForecastSnap.fetch_id).where(GridForecastSnap.taken_at >= a, GridForecastSnap.taken_at < b).order_by(GridForecastSnap.taken_at, GridForecastSnap.ts)))
        add(f"v1/eirgrid/forecast_snapshots/{ym}.csv", data, "text/csv", n, "EirGrid's own wind and demand forecasts, as they stood at each fetch", "eirgrid_live")
    for ym in _months(session, FuelMix.ts, since):
        a, b = _month_range(ym)
        data, n = _csv(["ts_utc", "region", "fuel", "value_mwh_day_to_date", "fetch_id"], session.execute(
            select(FuelMix.ts, FuelMix.region, FuelMix.fuel, FuelMix.value, FuelMix.fetch_id).where(FuelMix.ts >= a, FuelMix.ts < b).order_by(FuelMix.ts)))
        add(f"v1/eirgrid/fuel_mix/{ym}.csv", data, "text/csv", n, "All-island fuel mix, day-to-date totals as published", "eirgrid_live")
    books = {b.id: b for b in session.scalars(select(Workbook))}
    data, n = _csv(["period", "jurisdiction", "tech", "metric", "value", "unit", "workbook", "sheet", "cell"],
                   ((m.period, m.jurisdiction, m.tech, m.metric, m.value, m.unit, books[m.workbook_id].filename, m.sheet, m.cell)
                    for m in session.scalars(select(MonthlyStat).order_by(MonthlyStat.period, MonthlyStat.jurisdiction, MonthlyStat.tech, MonthlyStat.metric))))
    add("v1/eirgrid/dispatch_down/monthly.csv", data, "text/csv", n, "Monthly wind/solar/RES availability, generation and dispatch-down (MWh, %) and wind causes, IE/NI/all-island", "eirgrid_dd")
    data, n = _csv(["period", "tech", "region", "metric", "value_pct", "workbook", "sheet", "cell"],
                   ((r.period, r.tech, r.region, r.metric, r.value, books[r.workbook_id].filename, r.sheet, r.cell)
                    for r in session.scalars(select(RegionalStat).order_by(RegionalStat.period, RegionalStat.tech, RegionalStat.region))))
    add("v1/eirgrid/dispatch_down/regional.csv", data, "text/csv", n, "Dispatch-down % by EirGrid region (quarterly, monthly from 2021, annual)", "eirgrid_dd")
    data, n = _csv(["tech", "jurisdiction", "region", "node", "name", "capacity_mw", "workbook", "first_seen_utc", "last_seen_utc"],
                   ((f.tech, f.jurisdiction, f.region, f.node, f.name, f.capacity_mw, books[f.workbook_id].filename, f.first_seen, f.last_seen)
                    for f in session.scalars(select(Farm).order_by(Farm.tech, Farm.region, Farm.name))))
    add("v1/eirgrid/dispatch_down/farms.csv", data, "text/csv", n, "Controllable wind and solar farms: region, node, capacity", "eirgrid_dd")
    data, n = _csv(["filename", "url", "sha256", "bytes", "retrieved_utc", "latest_period"],
                   ((b.filename, b.url, b.sha256, b.bytes, b.retrieved_at, b.latest_period) for b in sorted(books.values(), key=lambda b: b.retrieved_at)))
    add("v1/eirgrid/dispatch_down/workbooks.csv", data, "text/csv", n, "Every EirGrid dispatch-down workbook version read", "eirgrid_dd")

    data, n = _csv(["id", "name", "region", "kind", "lat", "lon", "weight_mw"], ((p.id, p.name, p.tags[0], p.tags[1], p.lat, p.lon, round(p.weight, 1))
                                                                                    for p in points.all_points()))
    add("v1/weather/points.csv", data, "text/csv", n, "Weather sampling points (wind/solar regions, capacity-weighted)", "weather_forecasts")
    data, n = _csv(["slug", "name", "lat_approx", "lon_approx"], ((k, v[0], v[1], v[2]) for k, v in points.STATIONS.items()))
    add("v1/weather/stations.csv", data, "text/csv", n, "Met Éireann observation stations used", "weather_obs")
    best = {}
    for r in session.scalars(select(WeatherStepRow).where(WeatherStepRow.valid_at >= utcnow() - timedelta(hours=3))):
        k = (r.source, r.location_id, r.valid_at)
        if k not in best or (r.issued_at or datetime.min) > (best[k].issued_at or datetime.min):
            best[k] = r
    cols = ["source", "location_id", "issued_at", "valid_at", "model", "wind_speed", "wind_dir", "wind_gust", "temp_c", "rh_pct", "pressure_hpa",
            "cloud_pct", "ghi_wm2", "precip_mm", "precip_period_h", "precip_prob_pct", "altitude_m", "fetch_id"]
    data, n = _csv([c if c not in ("issued_at", "valid_at") else c + "_utc" for c in cols],
                   ([getattr(r, c) for c in cols] for _, r in sorted(best.items(), key=lambda kv: kv[0])))
    add("v1/weather/forecast_latest.csv", data, "text/csv", n, "Freshest hourly/3-hourly point forecasts from Met Éireann (HARMONIE/ECMWF) and MET Norway, SI units", "weather_forecasts")
    for ym in _months(session, ObservationRow.observed_at, since):
        a, b = _month_range(ym)
        data, n = _csv(["station", "station_name", "observed_utc", "wind_ms", "wind_dir_deg", "gust_ms", "temp_c", "rh_pct", "pressure_hpa", "rain_mm", "weather", "fetch_id"],
                       ((o.station_id, o.station_name, o.observed_at, o.wind_speed, o.wind_dir, o.wind_gust, o.temp_c, o.rh_pct, o.pressure_hpa, o.rain_mm, o.weather, o.fetch_id)
                        for o in session.scalars(select(ObservationRow).where(ObservationRow.observed_at >= a, ObservationRow.observed_at < b)
                                                 .order_by(ObservationRow.observed_at, ObservationRow.station_id))))
        add(f"v1/weather/observations/{ym}.csv", data, "text/csv", n, "Met Éireann hourly station observations", "weather_obs")
    warns = [dict(id=w.warning_id, level=w.level, type=w.type, headline=w.headline, description=w.description, regions=w.regions, onset=_iso(w.onset),
                  expiry=_iso(w.expiry), first_seen=_iso(w.first_seen), last_seen=_iso(w.last_seen), active=w.active, fetch_id=w.fetch_id)
             for w in session.scalars(select(WarningRow).order_by(WarningRow.first_seen.desc()).limit(500))]
    add("v1/weather/warnings.json", json.dumps(warns, indent=1).encode(), "application/json", len(warns), "Met Éireann warnings seen by the hub", "weather_warnings")
    for m in session.scalars(select(Mirror)):
        ctype = "application/json" if m.ext == "json" else "text/csv"
        add(f"v1/mirror/{m.key}/latest.{m.ext}", m.body or b"", ctype, m.records, f"Raw mirror of {m.key} (latest)", m.key)
        if m.versioned_path:
            add(m.versioned_path, m.body or b"", ctype, m.records, f"Raw mirror of {m.key}, version {m.sha256[:12]}", m.key)
    files.update(ted_files(session))        # TED notices by year (kept in the database)
    files.update(artifacts.ARTIFACTS)       # large files a collector produced on this run (eTenders, climate stations)
    for ym in _months(session, FetchLog.retrieved_at, since):
        a, b = _month_range(ym)
        data, n = _csv(["id", "source", "retrieved_utc", "url", "sha256", "bytes", "issued_utc", "model_runs", "licence", "note"],
                       ((f.id, f.source, f.retrieved_at, f.url, f.sha256, f.bytes, f.issued_at, json.dumps(f.model_runs) if f.model_runs else None, f.licence, f.note)
                        for f in session.scalars(select(FetchLog).where(FetchLog.retrieved_at >= a, FetchLog.retrieved_at < b).order_by(FetchLog.id))))
        add(f"v1/ledger/{ym}.csv", data, "text/csv", n, "Provenance ledger: every payload used", "all")
    evs = [dict(id=e.id, at=_iso(e.at), kind=e.kind, label=EVENT_LABELS.get(e.kind, e.kind), source=e.source, summary=e.summary, detail=e.detail,
                source_url=e.source_url, event_date=_iso(e.event_date)) for e in session.scalars(select(HubEvent).order_by(HubEvent.at.desc()).limit(500))]
    add("v1/events.json", json.dumps(evs, indent=1).encode(), "application/json", len(evs), "Source-level events (new workbook months, warnings, records, outages)", "all")
    add("v1/status.json", json.dumps(status(session), indent=1).encode(), "application/json", None, "Health of every collector", "all")
    return files


def status(session: Session) -> dict:
    out = []
    for key, c in REGISTRY.items():
        run, ok = last_run(session, key), last_run(session, key, ok_only=True)
        stale = ok is None or (utcnow() - ok.started_at) > timedelta(hours=max(3, c.interval_hours * 3))
        out.append(dict(key=key, name=c.name, publisher=c.publisher, licence=c.licence, url=c.url, provides=c.provides, every_hours=c.interval_hours,
                        used_by=list(c.used_by), state="failing" if run and not run.ok else "stale" if stale else "healthy",
                        last_ok=_iso(ok.started_at) if ok else None, last_message=run.message if run else None))
    return dict(generated_at=_iso(utcnow()), sources=out)


def snapshot_bytes() -> bytes:
    """Consistent copy of the live DB (sqlite backup API), VACUUMed, gzipped."""
    src = settings.database_url.replace("sqlite:///", "")
    with tempfile.TemporaryDirectory() as td:
        dst = Path(td) / "hub.sqlite"
        a, b = sqlite3.connect(src), sqlite3.connect(dst)
        a.backup(b)
        a.close()
        b.execute("PRAGMA journal_mode=DELETE")
        b.execute("VACUUM")
        b.close()
        buf = io.BytesIO()
        with open(dst, "rb") as f, gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as g:
            shutil.copyfileobj(f, g)
        return buf.getvalue()


def catalog(published: list[Published], meta: dict[str, dict]) -> bytes:
    """Every file currently on data.solas33.com, from the `published` table (true size and hash),
    described from this run's build where available, else from the description of its family."""
    family = {}
    for path, f in meta.items():
        family[path.rsplit("/", 1)[0]] = (f["desc"], f["source"])
    items = []
    for p in sorted(published, key=lambda p: p.path):
        desc, source = (meta[p.path]["desc"], meta[p.path]["source"]) if p.path in meta else family.get(p.path.rsplit("/", 1)[0], ("", ""))
        c = REGISTRY.get(source)
        items.append(dict(path=p.path, url=f"{PUBLIC_BASE}/{p.path}", bytes=p.bytes, sha256=p.sha256, rows=p.rows, updated=_iso(p.uploaded_at),
                          description=desc, source=source or None, publisher=c.publisher if c else "Solas33 Data Hub",
                          source_licence=c.licence if c else None, source_url=c.url if c else None, used_by=list(c.used_by) if c else []))
    return json.dumps(dict(generated_at=_iso(utcnow()), licence=LICENCE, publisher="Solas33 Data Hub", base=PUBLIC_BASE, files=items), indent=1).encode()


class Uploader:
    """Uploads to R2 with `wrangler r2 object put` (same Cloudflare credentials as deploys)."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.npx = "npx.cmd" if sys.platform == "win32" else "npx"

    def put(self, path: str, data: bytes, ctype: str, cache: str) -> None:
        if self.dry_run:
            return
        with tempfile.NamedTemporaryFile(delete=False) as t:
            t.write(data)
            tmp = t.name
        try:
            cmd = [self.npx, "--yes", "wrangler@4", "r2", "object", "put", f"{BUCKET}/{path}", "--file", tmp, "--content-type", ctype,
                   "--cache-control", cache, "--remote"]
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            if r.returncode != 0:
                raise RuntimeError(f"upload of {path} failed: {(r.stderr or r.stdout)[-400:]}")
        finally:
            os.unlink(tmp)


def publish(session: Session, uploader: Uploader, months_back: int | None = 2, include_snapshot: bool = True) -> dict:
    files = build(session, months_back)
    prev = {p.path: p for p in session.scalars(select(Published))}
    uploaded = skipped = 0
    for path, f in sorted(files.items()):
        digest = hashlib.sha256(f["data"]).hexdigest()
        if path in prev and prev[path].sha256 == digest:
            skipped += 1
            continue
        live = path.endswith(("latest.json", "latest.csv", "status.json", "events.json", "forecast_latest.csv", "warnings.json")) or \
            path.split("/")[-1][:7] == utcnow().strftime("%Y-%m")
        uploader.put(path, f["data"], f["type"] + ("; charset=utf-8" if f["type"].startswith("text") else ""), "public, max-age=300" if live else "public, max-age=86400")
        row = prev.get(path) or Published(path=path)
        row.sha256, row.bytes, row.rows, row.uploaded_at = digest, len(f["data"]), f["rows"], utcnow()
        session.add(row)
        session.commit()
        uploaded += 1
    cat = catalog(list(session.scalars(select(Published))), files)
    uploader.put("v1/catalog.json", cat, "application/json", "public, max-age=300")
    snap = None
    if include_snapshot:
        snap = snapshot_bytes()
        uploader.put("v1/hub.sqlite.gz", snap, "application/gzip", "public, max-age=300")
        uploader.put("v1/hub.sqlite.json", json.dumps(dict(generated_at=_iso(utcnow()), bytes=len(snap), sha256=hashlib.sha256(snap).hexdigest())).encode(),
                     "application/json", "public, max-age=60")
    return dict(files=len(files), uploaded=uploaded, skipped=skipped, snapshot_bytes=len(snap) if snap else 0)


