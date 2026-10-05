"""Great Britain grid: carbon intensity and generation mix (NESO / Carbon Intensity API), as daily records.

The API is open and keyless (https://carbonintensity.org.uk/). It serves half-hourly national figures; the hub folds them into daily
min/max/mean (rows tier) and publishes one file per year per dataset. A context series for the all-island grid (GridWatch) and for GB comparisons.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from s33weather.models import sha256

from ..db import utcnow
from ..models import DailyStat
from .base import Collector, Result, get_with_retry, log_fetch
from .tables import add_daily, daily_files, load_state, save_state

API = "https://api.carbonintensity.org.uk"
WINDOW = timedelta(days=13)   # the API serves at most 14 days per request


def parse_intensity(doc: dict) -> list[tuple[datetime, float]]:
    out = []
    for d in doc.get("data", []):
        v = (d.get("intensity") or {}).get("actual")
        if v is None:
            continue
        out.append((datetime.strptime(d["from"], "%Y-%m-%dT%H:%MZ"), float(v)))
    return out


def parse_generation(doc: dict) -> list[tuple[datetime, str, float]]:
    out = []
    for d in doc.get("data", []):
        ts = datetime.strptime(d["from"], "%Y-%m-%dT%H:%MZ")
        out += [(ts, g["fuel"], float(g["perc"])) for g in d.get("generationmix", []) if g.get("perc") is not None]
    return out


class NesoCarbon(Collector):
    key = "gb_carbon_intensity"
    name = "Great Britain carbon intensity and generation mix (NESO / Carbon Intensity API)"
    publisher = "National Energy System Operator (NESO) with the Carbon Intensity API partners"
    url = "https://carbonintensity.org.uk/"
    licence = "Creative Commons Attribution 4.0"
    provides = "GB grid carbon intensity (gCO2/kWh, actual) and generation mix (% by fuel): daily min/max/mean since collection began (30 days back-filled)."
    interval_hours = 6.0
    domain, tier = "energy_intl", "rows"
    rights = "check"        # confirm the Carbon Intensity API / NESO terms before any paid use

    def run(self, session: Session, client: httpx.Client) -> Result:
        now = utcnow().replace(second=0, microsecond=0)
        last = session.scalar(select(func.max(DailyStat.last_ts)).where(DailyStat.dataset == "gb_carbon_intensity"))
        start = (last - timedelta(hours=1)) if last else now - timedelta(days=30)
        added = n = 0
        while start < now:
            end = min(start + WINDOW, now)
            span = f"{start:%Y-%m-%dT%H:%MZ}/{end:%Y-%m-%dT%H:%MZ}"
            ci = get_with_retry(client, f"{API}/intensity/{span}", timeout=120).json()
            gen = get_with_retry(client, f"{API}/generation/{span}", timeout=120).json()
            log_fetch(session, self.key, f"{API}/intensity/{span}", sha256(repr(ci).encode()), len(ci.get("data", [])), self.licence)
            for ts, v in parse_intensity(ci):
                added += add_daily(session, "gb_carbon_intensity", "GB", "intensity_actual", ts, v, "gCO2/kWh")
                n += 1
            for ts, fuel, perc in parse_generation(gen):
                add_daily(session, "gb_generation_mix", "GB", fuel, ts, perc, "%")
            session.flush()
            start = end
        res = save_state(session, self.key, load_state(session, self.key) or {"items": {}}, self.url, self.name)
        res.fetched, res.created, res.message = n, added, f"{n} half-hours read, {added} new"
        return res

    def files(self, session: Session) -> dict[str, dict]:
        out = daily_files(session, "gb_carbon_intensity", "energy_intl/gb_carbon_daily", self.key, "GB carbon intensity (gCO2/kWh, actual): daily min/max/mean")
        out.update(daily_files(session, "gb_generation_mix", "energy_intl/gb_generation_mix_daily", self.key, "GB generation mix (% of generation by fuel): daily min/max/mean"))
        return out
