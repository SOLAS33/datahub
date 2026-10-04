"""MET Norway Locationforecast 2.0 "complete" (CC BY 4.0 / NLOD; commercial use allowed;
an identifying User-Agent is mandatory and requests must be cached/limited).

Global ECMWF-based forecast, hourly for ~2.5 days then 6-hourly to ~9-10 days. It has no
irradiance field, so solar work derives GHI from cloud cover (see physics.solar). It is used
here as the second, independent model: agreement between it and Met Éireann is a direct
measure of forecast confidence.
"""
from __future__ import annotations

import json
from datetime import datetime

import httpx

from ..http import get
from ..models import Fetch, Location, Provenance, WeatherStep, sha256, utcnow

KEY = "metno"
PUBLISHER = "MET Norway"
LICENCE = "CC BY 4.0 / NLOD - MET Norway (attribution required)"
URL = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
LANDING = "https://api.met.no/weatherapi/locationforecast/2.0/documentation"


def _t(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def parse(raw: bytes, location: Location) -> tuple[list[WeatherStep], datetime | None]:
    body = json.loads(raw)
    props = body.get("properties", {})
    issued = _t(props.get("meta", {}).get("updated_at"))
    out = []
    for ts in props.get("timeseries", []):
        d = ts.get("data", {})
        inst = d.get("instant", {}).get("details", {})
        s = WeatherStep(location_id=location.id, source=KEY, valid_at=_t(ts["time"]), issued_at=issued, model="metno-ecmwf")
        s.wind_speed = inst.get("wind_speed")
        s.wind_dir = inst.get("wind_from_direction")
        s.wind_gust = inst.get("wind_speed_of_gust")
        s.wind_speed_p10 = inst.get("wind_speed_percentile_10")
        s.wind_speed_p90 = inst.get("wind_speed_percentile_90")
        s.temp_c = inst.get("air_temperature")
        s.dewpoint_c = inst.get("dew_point_temperature")
        s.rh_pct = inst.get("relative_humidity")
        s.pressure_hpa = inst.get("air_pressure_at_sea_level")
        s.cloud_pct = inst.get("cloud_area_fraction")
        s.cloud_low_pct = inst.get("cloud_area_fraction_low")
        s.cloud_mid_pct = inst.get("cloud_area_fraction_medium")
        s.cloud_high_pct = inst.get("cloud_area_fraction_high")
        s.fog_pct = inst.get("fog_area_fraction")
        # met.no precipitation is for the period AFTER `time`; it is kept on this step with
        # its period length so consumers can shift it if they need period-ending totals.
        for key, hours in (("next_1_hours", 1), ("next_6_hours", 6)):
            det = d.get(key, {}).get("details", {})
            if "precipitation_amount" in det:
                s.precip_mm = det.get("precipitation_amount")
                s.precip_min_mm = det.get("precipitation_amount_min")
                s.precip_max_mm = det.get("precipitation_amount_max")
                s.precip_prob_pct = det.get("probability_of_precipitation")
                s.precip_period_h = -hours  # negative = period starts at valid_at
                s.symbol = d.get(key, {}).get("summary", {}).get("symbol_code")
                break
        out.append(s)
    return out, issued


def fetch(client: httpx.Client, location: Location) -> Fetch:
    params = {"lat": f"{location.lat:.4f}", "lon": f"{location.lon:.4f}"}  # met.no asks for <= 4 decimals
    r = get(client, URL, params=params)
    steps, issued = parse(r.content, location)
    if not steps:
        raise ValueError(f"MET Norway returned no timeseries for {location.id}")
    prov = Provenance(source=KEY, publisher=PUBLISHER, url=str(r.url), licence=LICENCE, retrieved_at=utcnow(),
                      sha256=sha256(r.content), bytes=len(r.content), issued_at=issued,
                      model_runs=[{"model": "metno-ecmwf", "run_time": issued.isoformat() if issued else None}])
    return Fetch(records=steps, provenance=prov)
