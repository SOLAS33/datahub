"""Met Éireann open-data point forecast (CC BY 4.0, commercial use allowed with attribution).

One XML document per point: HARMONIE-AROME (2.5 km) hourly to ~54 h, then ECMWF out to
10 days at 1/3/6-hourly steps. The <meta> block names each model's run time and the valid
range it covers, so every step is tagged with the model and run that produced it.

Instant elements (from == to): temperature, wind, gust, global radiation, humidity,
pressure, cloud layers, dew point. Interval elements (from < to): precipitation (with
min/max/probability) and a weather symbol; they are attached to the step at `to`.

Note: the service answers over plain http; https returns 404 (checked 2026-10-04).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime

import httpx

from ..http import get
from ..models import Fetch, Location, Provenance, WeatherStep, sha256, utcnow

KEY = "metie"
PUBLISHER = "Met Éireann"
LICENCE = "CC BY 4.0 - Met Éireann open data (attribution required)"
URL = "http://openaccess.pf.api.met.ie/metno-wdb2ts/locationforecast"
LANDING = "https://data.gov.ie/dataset/met-eireann-forecast-api"


def _t(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def _f(el: ET.Element | None, attr: str) -> float | None:
    if el is None:
        return None
    v = el.get(attr)
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def parse(xml: bytes, location: Location) -> tuple[list[WeatherStep], list[dict]]:
    root = ET.fromstring(xml)
    runs = []
    for m in root.iter("model"):
        runs.append(dict(model=m.get("name"), run_time=_t(m.get("termin")), run_ended=_t(m.get("runended")),
                         next_run=_t(m.get("nextrun")), valid_from=_t(m.get("from")), valid_to=_t(m.get("to"))))

    def model_for(t: datetime) -> dict | None:
        for r in runs:
            if r["valid_from"] and r["valid_to"] and r["valid_from"] <= t <= r["valid_to"]:
                return r
        return None

    steps: dict[datetime, WeatherStep] = {}
    intervals: list[tuple[datetime, datetime, ET.Element]] = []
    for tm in root.iter("time"):
        frm, to = _t(tm.get("from")), _t(tm.get("to"))
        loc = tm.find("location")
        if loc is None or frm is None or to is None:
            continue
        if frm == to:
            run = model_for(frm)
            s = WeatherStep(location_id=location.id, source=KEY, valid_at=frm,
                            issued_at=run["run_time"] if run else None, model=run["model"] if run else None)
            s.altitude_m = _f(loc, "altitude")
            s.temp_c = _f(loc.find("temperature"), "value")
            s.wind_dir = _f(loc.find("windDirection"), "deg")
            s.wind_speed = _f(loc.find("windSpeed"), "mps")
            s.wind_gust = _f(loc.find("windGust"), "mps")
            s.ghi_wm2 = _f(loc.find("globalRadiation"), "value")
            s.rh_pct = _f(loc.find("humidity"), "value")
            s.pressure_hpa = _f(loc.find("pressure"), "value")
            s.cloud_pct = _f(loc.find("cloudiness"), "percent")
            s.cloud_low_pct = _f(loc.find("lowClouds"), "percent")
            s.cloud_mid_pct = _f(loc.find("mediumClouds"), "percent")
            s.cloud_high_pct = _f(loc.find("highClouds"), "percent")
            s.fog_pct = _f(loc.find("fog"), "percent")
            s.dewpoint_c = _f(loc.find("dewpointTemperature"), "value")
            steps[frm] = s
        else:
            intervals.append((frm, to, loc))
    # Attach each step's shortest precipitation interval ending at it (several overlap in the ECMWF range).
    best: dict[datetime, tuple[float, ET.Element]] = {}
    for frm, to, loc in intervals:
        hours = (to - frm).total_seconds() / 3600
        if to in steps and (to not in best or hours < best[to][0]):
            best[to] = (hours, loc)
    for to, (hours, loc) in best.items():
        s = steps[to]
        p = loc.find("precipitation")
        s.precip_mm, s.precip_min_mm = _f(p, "value"), _f(p, "minvalue")
        s.precip_max_mm, s.precip_prob_pct = _f(p, "maxvalue"), _f(p, "probability")
        s.precip_period_h = hours
        sym = loc.find("symbol")
        s.symbol = sym.get("id") if sym is not None else None
    return [steps[k] for k in sorted(steps)], runs


def fetch(client: httpx.Client, location: Location) -> Fetch:
    # The service expects ';' between parameters; httpx would encode it, so build the URL by hand.
    url = f"{URL}?lat={location.lat:.4f};long={location.lon:.4f}"
    r = get(client, url)
    steps, runs = parse(r.content, location)
    if not steps:
        raise ValueError(f"Met Éireann returned no forecast steps for {location.id}")
    issued = max((r_["run_time"] for r_ in runs if r_["run_time"]), default=None)
    prov = Provenance(source=KEY, publisher=PUBLISHER, url=url, licence=LICENCE, retrieved_at=utcnow(),
                      sha256=sha256(r.content), bytes=len(r.content), issued_at=issued,
                      model_runs=[{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in run.items()} for run in runs])
    return Fetch(records=steps, provenance=prov)
