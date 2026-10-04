"""Met Éireann hourly synoptic observations for today (the same feed met.ie's own station
pages use). Not a formally versioned API, so it is treated defensively:

* An unknown station slug does NOT error - the service silently answers with Dublin
  Airport's data. Every response is therefore checked: the station name in the payload must
  match the station we asked for, or the whole response is rejected.
* windSpeed is in km/h (met.ie displays km/h for these stations); converted to m/s here.
* Times are local Irish time (reportTime HH:MM, date DD-MM-YYYY) and converted to UTC.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from ..http import get
from ..models import Fetch, Observation, Provenance, sha256, utcnow

KEY = "metie_obs"
PUBLISHER = "Met Éireann"
LICENCE = "CC BY 4.0 - Met Éireann (attribution required)"
URL = "https://prodapi.metweb.ie/observations/{slug}/today"
DUBLIN = ZoneInfo("Europe/Dublin")


def _num(v) -> float | None:
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "-", "NA", "n/a"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _norm(name: str) -> str:
    return "".join(c for c in name.lower() if c.isalnum())


def parse(raw: bytes, slug: str, expected_name: str) -> list[Observation]:
    rows = json.loads(raw)
    if not isinstance(rows, list):
        raise ValueError("unexpected observation payload")
    out = []
    for r in rows:
        if _norm(r.get("name", "")) != _norm(expected_name):
            raise ValueError(f"station mismatch: asked for {expected_name!r}, got {r.get('name')!r} (unknown slug falls back to another station)")
        try:
            local = datetime.strptime(f"{r['date']} {r['reportTime']}", "%d-%m-%Y %H:%M").replace(tzinfo=DUBLIN)
        except (KeyError, ValueError):
            continue
        kmh = _num(r.get("windSpeed"))
        gust = _num(r.get("windGust"))
        out.append(Observation(
            station_id=slug, station_name=r.get("name", expected_name), observed_at=local.astimezone(timezone.utc), source=KEY,
            wind_speed=round(kmh / 3.6, 2) if kmh is not None else None,
            wind_dir=_num(r.get("windDirection")) if kmh is not None else None,
            wind_gust=round(gust / 3.6, 2) if gust is not None else None,
            temp_c=_num(r.get("temperature")), rh_pct=_num(r.get("humidity")), pressure_hpa=_num(r.get("pressure")),
            rain_mm=_num(r.get("rainfall")), weather=r.get("weatherDescription")))
    return out


def fetch(client: httpx.Client, slug: str, expected_name: str) -> Fetch:
    url = URL.format(slug=slug)
    r = get(client, url)
    obs = parse(r.content, slug, expected_name)
    prov = Provenance(source=KEY, publisher=PUBLISHER, url=url, licence=LICENCE, retrieved_at=utcnow(),
                      sha256=sha256(r.content), bytes=len(r.content))
    return Fetch(records=obs, provenance=prov)
