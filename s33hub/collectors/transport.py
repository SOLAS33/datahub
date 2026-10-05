"""National Transport Authority GTFS: every public transport stop and route in Ireland.

The NTA feed is a ~180 MB zip refreshed daily. The hub downloads it when its Last-Modified changes (at most weekly) and publishes only
the small reference tables most sites want: agencies, routes and stops (with coordinates), plus the feed's version record.
"""
from __future__ import annotations

import csv
import io
import zipfile

import httpx
from sqlalchemy.orm import Session

from s33weather.models import sha256

from .base import Collector, Result, get_with_retry, log_fetch
from .tables import csv_bytes, decode, gz, load_state, publish_table, save_state

URL = "https://www.transportforireland.ie/transitData/Data/GTFS_All.zip"
KEEP = {"agency.txt": ["agency_id", "agency_name", "agency_url", "agency_timezone"],
        "routes.txt": ["route_id", "agency_id", "route_short_name", "route_long_name", "route_type"],
        "stops.txt": ["stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon", "zone_id", "location_type", "parent_station"]}


def read_table(z: zipfile.ZipFile, name: str, cols: list[str]) -> list[list[str]]:
    rd = csv.DictReader(io.StringIO(decode(z.read(name)), newline=""))
    return [[(r.get(c) or "").strip() for c in cols] for r in rd]


class NtaGtfs(Collector):
    key = "nta_gtfs"
    name = "NTA GTFS: public transport agencies, routes and stops"
    publisher = "National Transport Authority"
    url = "https://www.transportforireland.ie/transitData/PT_Data.html"
    licence = "Creative Commons Attribution 4.0"
    provides = "Agencies, routes and stops (with coordinates) of every Irish public transport operator in the NTA GTFS feed, and the feed's version record."
    interval_hours = 168.0
    domain, tier = "transport", "files"

    def run(self, session: Session, client: httpx.Client) -> Result:
        state = load_state(session, self.key)
        items = state.setdefault("items", {})
        head = client.head(URL)
        lm, ln = head.headers.get("last-modified", ""), head.headers.get("content-length", "")
        if lm and items.get("_source", {}).get("last_modified") == lm and items.get("_source", {}).get("content_length") == ln:
            return Result(message=f"unchanged at source (last modified {lm})")
        raw = get_with_retry(client, URL, timeout=900).content
        log_fetch(session, self.key, URL, sha256(raw), len(raw), self.licence)
        z = zipfile.ZipFile(io.BytesIO(raw))
        n = 0
        for name, cols in KEEP.items():
            rows = read_table(z, name, cols)
            if not rows:
                raise ValueError(f"{name} is empty")
            publish_table(items, name, f"v1/transport/gtfs_{name[:-4]}.csv.gz", gz(csv_bytes(cols, rows)), "application/gzip", len(rows), f"NTA GTFS {name[:-4]}", self.key)
            n += len(rows)
        feed = {}
        if "feed_info.txt" in z.namelist():
            fi = list(csv.DictReader(io.StringIO(decode(z.read("feed_info.txt")))))
            feed = fi[0] if fi else {}
        items["_source"] = dict(last_modified=lm, content_length=ln, sha256=sha256(raw), feed=feed)
        res = save_state(session, self.key, state, self.url, self.name)
        res.fetched, res.message = n, f"{n:,} reference rows from a {len(raw) / 1e6:.0f} MB feed"
        return res
