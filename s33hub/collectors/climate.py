"""Met Éireann historical daily climate data, by station (decades of record, ~520 stations with files).

Source: https://www.met.ie/climate/available-data/daily-data - the files behind that page, at
https://clidata.met.ie/cli/climate_data/webdata/ (StationDetails.csv and dly<station>.csv). Met
Éireann publishes data CC BY 4.0, with a lag of up to about a month.

Why the hub holds it: contractors who lose time to weather need a neutral, checkable record of
what the weather was at or near a site. Sites read `v1/climate/stations.csv` and
`v1/climate/daily/<id>.csv.gz` instead of contacting Met Éireann, so a request for evidence costs
the source nothing and every figure traces to a file whose SHA-256 is in the catalogue.

Each station file is trimmed to the columns that matter for delay claims, dates made ISO, units
left exactly as published (knots, mm, degrees C), and the data-quality indicator for rain kept.
Stations are refreshed a chunk at a time (CHUNK per run) so a first run or a monthly refresh never
has to upload all ~520 files in one go.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from s33weather.models import sha256

from .. import artifacts
from ..models import Mirror
from .base import Collector, Result, get_with_retry, log_fetch
from .mirrors import store_mirror

BASE = "https://clidata.met.ie/cli/climate_data/webdata"
KEEP = {"rain": "rain_mm", "irrd": "rain_ind", "maxtp": "max_temp_c", "mintp": "min_temp_c", "wdsp": "mean_wind_kn", "hm": "max_10min_wind_kn",
        "hg": "gust_kn", "sun": "sun_hours"}
OUT = ["date"] + list(KEEP.values())
CHUNK = 60
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def parse_daily(text: str) -> tuple[dict, list[list[str]]]:
    """(station header facts, rows) from a dly<id>.csv. The file has a prose preamble, a legend, then 'date,ind,maxtp,...' and data."""
    lines = text.splitlines()
    facts: dict = {}
    for ln in lines[:6]:
        m = re.match(r"Station Name:\s*(.*)", ln)
        if m:
            facts["name"] = m.group(1).strip()
        m = re.match(r"Station Height:\s*(\d+)", ln)
        if m:
            facts["height_m"] = int(m.group(1))
        m = re.match(r"Latitude:\s*([-\d.]+)\s*,\s*Longitude:\s*([-\d.]+)", ln)
        if m:
            facts["lat"], facts["lon"] = float(m.group(1)), float(m.group(2))
    start = next((i for i, ln in enumerate(lines) if ln.lower().startswith("date,")), None)
    if start is None:
        raise ValueError("no data header found in climate file")
    hdr = [h.strip() for h in lines[start].split(",")]
    col = {h: i for i, h in enumerate(hdr)}
    rows = []
    for ln in lines[start + 1:]:
        p = ln.split(",")
        if len(p) < 3:
            continue
        m = re.match(r"(\d{1,2})-([a-z]{3})-(\d{4})", p[0].strip().lower())
        if not m:
            continue
        d = f"{int(m.group(3)):04d}-{MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"
        row = [d]
        for src in KEEP:
            v = p[col[src]].strip() if src in col and col[src] < len(p) else ""
            row.append("" if v in ("", " ") else v)
        rows.append(row)
    return facts, rows


def _csv_gz(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(OUT)
    w.writerows(rows)
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", compresslevel=9, mtime=0) as g:
        g.write(buf.getvalue().encode("utf-8"))
    return out.getvalue()


def _state(session: Session, key: str) -> dict:
    m = session.scalar(select(Mirror).where(Mirror.key == key))
    try:
        return json.loads(m.body) if m and m.body else {}
    except ValueError:
        return {}


class ClimateDaily(Collector):
    key = "climate_daily"
    name = "Met Éireann daily climate records by station"
    publisher = "Met Éireann"
    url = "https://www.met.ie/climate/available-data/daily-data"
    licence = "Creative Commons Attribution 4.0 International"
    provides = ("Daily rainfall, temperature, wind and sunshine at ~520 stations with records back decades, trimmed and dated in ISO form; "
                "station list with coordinates and coverage.")
    used_by = ("tenderwatch",)
    interval_hours = 3.0

    def run(self, session: Session, client: httpx.Client) -> Result:
        st = _state(session, self.key)
        stations = st.get("stations", {})
        sd = get_with_retry(client, f"{BASE}/StationDetails.csv")
        log_fetch(session, self.key, f"{BASE}/StationDetails.csv", sha256(sd.content), len(sd.content), self.licence)
        detail = {r["station name"].strip(): r for r in csv.DictReader(io.StringIO(sd.content.decode("utf-8-sig")))
                  if r["close year"] in ("(null)", "") or r["station name"].strip() in stations}
        ref = "532"   # Dublin Airport: Met Éireann refreshes every file together, so one header tells us whether anything is new
        ref_lm = client.head(f"{BASE}/dly{ref}.csv").headers.get("last-modified", "")
        if ref_lm != st.get("ref_last_modified"):      # Met Éireann refreshed its files: every station is due again, a chunk per run
            for s in stations.values():
                s["stale"] = True
        todo = [sid for sid in detail if sid not in stations or stations[sid].get("stale") or not stations[sid].get("sha256")]
        batch = todo[:CHUNK]

        def fetch(sid: str):
            try:
                r = client.get(f"{BASE}/dly{sid}.csv", timeout=90)
                return sid, (r.content if r.status_code == 200 else None), str(r.url)
            except httpx.HTTPError:
                return sid, None, f"{BASE}/dly{sid}.csv"

        done = 0
        with ThreadPoolExecutor(6) as ex:
            for sid, raw, url in ex.map(fetch, batch):
                if raw is None:
                    stations[sid] = dict(stations.get(sid, {}), missing=True, stale=False)
                    continue
                try:
                    facts, rows = parse_daily(raw.decode("utf-8-sig", errors="replace"))
                except ValueError:
                    stations[sid] = dict(stations.get(sid, {}), missing=True, stale=False)
                    continue
                if not rows:
                    stations[sid] = dict(stations.get(sid, {}), missing=True, stale=False)
                    continue
                fl = log_fetch(session, self.key, url, sha256(raw), len(raw), self.licence)
                gz = _csv_gz(rows)
                artifacts.put(f"v1/climate/daily/{sid}.csv.gz", gz, "application/gzip", len(rows),
                              f"Met Éireann daily record, station {sid} ({detail[sid]['name']}): rain, temperature, wind, sunshine; units as published", self.key)
                d = detail[sid]
                has = lambda i: any(r[i] for r in rows[-3000:])  # noqa: E731
                stations[sid] = dict(sha256=sha256(gz), source_sha256=sha256(raw), fetch_id=fl.id, rows=len(rows), first=rows[0][0], last=rows[-1][0],
                                     has_wind=has(5) or has(6) or has(7), has_temp=has(3) or has(4), has_rain=has(1), name=d["name"], county=d["county"],
                                     lat=d["latitude"], lon=d["longitude"], height=d["height(m)"], open=d["open year"], close=d["close year"], stale=False)
                done += 1
        remaining = sum(1 for sid in detail if sid not in stations or stations[sid].get("stale"))
        st = dict(ref_last_modified=ref_lm, stations=stations, pending=remaining)
        # station list from everything we hold a file for
        have = {sid: s for sid, s in stations.items() if s.get("sha256")}
        rows_out = [[sid, s["name"], s["county"], s["lat"], s["lon"], s["height"], s["open"], s["close"], s["first"], s["last"], s["rows"],
                     int(s["has_rain"]), int(s["has_temp"]), int(s["has_wind"]), s["sha256"]] for sid, s in sorted(have.items(), key=lambda kv: int(kv[0]))]
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["station_id", "name", "county", "lat", "lon", "height_m", "open_year", "close_year", "first_date", "last_date", "days", "has_rain", "has_temp",
                    "has_wind", "file_sha256"])
        w.writerows(rows_out)
        artifacts.put("v1/climate/stations.csv", buf.getvalue().encode(), "text/csv", len(rows_out),
                      "Met Éireann stations with a daily record: coordinates, coverage dates, which measures exist, and the SHA-256 of each published file", self.key)
        res = store_mirror(session, self.key, json.dumps(st, sort_keys=True).encode(), "json", len(have), [], self.url, self.name)
        res.fetched = done
        res.message = f"{done} station files refreshed this run, {remaining} pending, {len(have)} held"
        return res
