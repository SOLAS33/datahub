"""Water and sea: river and lake levels (OPW) and tide gauges (Marine Institute), as stations plus daily min/max/mean.

Both services only expose recent readings, so the hub folds each reading into a per-day record (rows tier: `daily_stats`) and publishes
one file per year. History therefore starts when the hub starts collecting; each station's coordinates are published too.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from s33weather.models import sha256

from .. import artifacts
from ..db import utcnow
from .base import Collector, Result, get_with_retry, log_fetch
from .tables import add_daily, csv_bytes, daily_files, gz, load_state, publish_table, save_state

OPW_URL = "https://waterlevel.ie/geojson/latest/"
OPW_UNITS = {"0001": "m", "0002": "degC", "0003": "m OD"}   # sensor 0001 = water level, 0002 = water temperature, 0003 = level above Ordnance Datum


def parse_opw(doc: dict) -> tuple[list[dict], list[list]]:
    """(readings, stations) from the OPW latest GeoJSON. A reading with no numeric value is dropped."""
    readings, stations = [], {}
    for f in doc.get("features", []):
        p, g = f.get("properties", {}), f.get("geometry") or {}
        try:
            v = float(p["value"])
            ts = datetime.strptime(p["datetime"], "%Y-%m-%dT%H:%M:%SZ")
        except (KeyError, ValueError, TypeError):
            continue
        if p.get("err_code") not in (99, None):
            continue                      # 99 = no error flag in the OPW feed
        ref = p["station_ref"].lstrip("0") or p["station_ref"]
        readings.append(dict(station=ref, sensor=p["sensor_ref"], ts=ts, value=v))
        c = g.get("coordinates") or [None, None]
        stations.setdefault(ref, [ref, p.get("station_name"), p.get("region_id"), c[1], c[0]])
    return readings, list(stations.values())


class OpwWaterLevels(Collector):
    key = "opw_water_levels"
    name = "OPW river and lake levels (waterlevel.ie)"
    publisher = "Office of Public Works (OPW)"
    url = "https://waterlevel.ie/"
    licence = "Creative Commons Attribution 4.0 (OPW data; provisional, unvalidated)"
    provides = "~2,000 river, lake and tide readings across ~450 stations: station list, latest readings, and daily min/max/mean since collection began."
    interval_hours = 1.0
    domain, tier = "environment", "rows"
    rights = "check"        # licence wording on waterlevel.ie not yet read by us; data is marked provisional

    def run(self, session: Session, client: httpx.Client) -> Result:
        raw = get_with_retry(client, OPW_URL, timeout=120).content
        log_fetch(session, self.key, OPW_URL, sha256(raw), len(raw), self.licence)
        import json
        readings, stations = parse_opw(json.loads(raw))
        if len(readings) < 200:
            raise ValueError(f"OPW feed looks wrong: {len(readings)} readings")
        added = sum(add_daily(session, "opw_water", r["station"], r["sensor"], r["ts"], r["value"], OPW_UNITS.get(r["sensor"], "")) for r in readings)
        state = load_state(session, self.key)
        items = state.setdefault("items", {})
        publish_table(items, "stations", "v1/water/opw_stations.csv", csv_bytes(["station", "name", "region_id", "lat", "lon"], sorted(stations, key=lambda s: str(s[0]))), "text/csv", len(stations),
                      "OPW water level stations: id, name, region, coordinates", self.key)
        artifacts.put("v1/water/opw_latest.json", gz(raw), "application/gzip", len(readings), "OPW latest readings exactly as published (gzip)", self.key)
        res = save_state(session, self.key, state, self.url, self.name)
        res.fetched, res.created, res.message = len(readings), added, f"{len(readings)} readings, {added} new, {len(stations)} stations"
        return res

    def files(self, session: Session) -> dict[str, dict]:
        return daily_files(session, "opw_water", "water/opw_daily", self.key, "OPW water levels and temperatures: daily min/max/mean per station and sensor (sensor 0001 level m, 0002 temperature degC, 0003 level m OD)")


ERDDAP = "https://erddap.marine.ie/erddap/tabledap"


def parse_erddap(text: str) -> list[dict]:
    """Rows of an ERDDAP .csv response (two header lines: names, units)."""
    rd = csv.reader(io.StringIO(text))
    names = next(rd, [])
    next(rd, None)
    return [dict(zip(names, r)) for r in rd if r]


class MarineTideGauges(Collector):
    key = "marine_tide_gauges"
    name = "Irish National Tide Gauge Network (Marine Institute)"
    publisher = "Marine Institute"
    url = "https://erddap.marine.ie/erddap/tabledap/IrishNationalTideGaugeNetwork.html"
    licence = "Creative Commons Attribution 4.0"
    provides = "Real-time sea level at Irish tide gauges (m above Chart Datum and Malin Head OD): daily min/max/mean per gauge since collection began."
    interval_hours = 3.0
    domain, tier = "environment", "rows"
    rights = "check"        # Marine Institute licence for this dataset not yet confirmed by us
    dataset = "IrishNationalTideGaugeNetwork"

    def run(self, session: Session, client: httpx.Client) -> Result:
        since = (utcnow() - timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = f"{ERDDAP}/{self.dataset}.csv?&time%3E={since.replace(':', '%3A')}"
        r = client.get(url, timeout=180)
        if r.status_code == 404:                       # ERDDAP answers 404 when no row matches the window
            return Result(message="no new readings in the window")
        r.raise_for_status()
        log_fetch(session, self.key, url, sha256(r.content), len(r.content), self.licence)
        rows = parse_erddap(r.text)
        added = 0
        for x in rows:
            try:
                ts = datetime.strptime(x["time"], "%Y-%m-%dT%H:%M:%SZ")
            except (KeyError, ValueError):
                continue
            if x.get("QC_Flag") not in ("0", "1", ""):
                continue
            for var in ("Water_Level_LAT", "Water_Level_OD_Malin"):
                try:
                    v = float(x[var])
                except (KeyError, ValueError):
                    continue
                if v != v:             # "NaN": the gauge reported nothing for that variable
                    continue
                added += add_daily(session, "tide_gauges", x["station_id"], var, ts, v, "m")
        state = load_state(session, self.key)
        items = state.setdefault("items", {})
        pos = {}
        for x in rows:
            pos.setdefault(x.get("station_id"), [x.get("station_id"), x.get("latitude"), x.get("longitude")])
        publish_table(items, "stations", "v1/water/tide_gauge_stations.csv", csv_bytes(["station", "lat", "lon"], sorted(v for v in pos.values() if v[0])), "text/csv", len(pos),
                      "Tide gauge stations and coordinates", self.key)
        res = save_state(session, self.key, state, self.url, self.name)
        res.fetched, res.created, res.message = len(rows), added, f"{len(rows)} readings, {added} folded into daily records, {len(pos)} gauges"
        return res

    def files(self, session: Session) -> dict[str, dict]:
        return daily_files(session, "tide_gauges", "water/tide_daily", self.key, "Tide gauge sea level: daily min/max/mean per gauge (m above Chart Datum, and above Malin Head OD)")
