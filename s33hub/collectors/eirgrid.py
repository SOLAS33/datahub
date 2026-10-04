"""EirGrid Smart Grid Dashboard - the live state of the all-island grid.

The dashboard's JSON endpoint (what its own charts call) is not a versioned API, so every
response is shape-checked and the collector fails soft. Timestamps are Irish local wall-clock
time and are converted to UTC here; on the October clock change EirGrid publishes the
repeated hour once, so one hour a year is ambiguous (resolved to the first occurrence).

Series kept (15-minute): wind, solar, demand (ALL/ROI/NI), SNSP (ALL), CO2 intensity (ALL),
interconnector flows (ALL), plus EirGrid's own wind and demand forecasts as snapshots so
their accuracy can be measured. The fuel mix is a day-to-date total, stored per fetch.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from s33weather.models import sha256

from ..config import settings
from ..db import utcnow
from ..models import FuelMix, GridForecastSnap, GridReading
from .base import Collector, Result, add_event, log_fetch

API = "https://www.smartgriddashboard.com/api/chart/"
DUBLIN = ZoneInfo("Europe/Dublin")
LICENCE = "EirGrid Group - published for public information; see smartgriddashboard.com terms"

# (area, region) -> (series, unit) ; FieldName from the payload decides series for multi-field areas
ACTUALS = [
    ("windactual", "ALL", "wind", "MW"), ("windactual", "ROI", "wind", "MW"), ("windactual", "NI", "wind", "MW"),
    ("solaractual", "ALL", "solar", "MW"), ("solaractual", "ROI", "solar", "MW"),
    ("demandactual", "ALL", "demand", "MW"), ("demandactual", "ROI", "demand", "MW"), ("demandactual", "NI", "demand", "MW"),
    ("snspall", "ALL", "snsp", "%"), ("co2intensity", "ALL", "co2", "gCO2/kWh"), ("interconnection", "ALL", "ic", "MW"),
]
FORECASTS = [("windforecast", "ALL", "wind_fc"), ("demandforecast", "ROI", "demand_fc")]
IC_FIELDS = {"INTER_NET": "ic_net", "INTER_EWIC": "ic_ewic", "INTER_GRNLK": "ic_greenlink", "INTER_MOYLE": "ic_moyle"}
# Areas whose history is backfilled on an empty database (the rest start from today).
BACKFILL = {("windactual", "ALL"), ("windactual", "ROI"), ("solaractual", "ALL"), ("demandactual", "ALL"), ("snspall", "ALL"),
            ("co2intensity", "ALL"), ("interconnection", "ALL")}


def to_utc(effective: str) -> datetime:
    local = datetime.strptime(effective, "%d-%b-%Y %H:%M:%S").replace(tzinfo=DUBLIN)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _get(client, area: str, region: str, d0: date, d1: date, date_range: str = "day"):
    fmt = "%d-%b-%Y"
    params = dict(region=region, chartType="default", dateRange=date_range, dateFrom=d0.strftime(fmt), dateTo=d1.strftime(fmt), areas=area)
    r = client.get(API, params=params)
    r.raise_for_status()
    body = r.json()
    rows = body.get("Rows")
    if rows is None:
        raise ValueError(f"EirGrid response for {area}/{region} has no Rows (shape changed?)")
    return r, rows


class EirGridLive(Collector):
    key = "eirgrid_live"
    name = "EirGrid Smart Grid Dashboard"
    publisher = "EirGrid Group"
    url = "https://www.smartgriddashboard.com/"
    licence = LICENCE
    provides = "15-minute wind, solar, demand, SNSP, CO2 intensity, interconnector flows and fuel mix; EirGrid's own wind/demand forecasts."
    interval_hours = 1.0
    used_by = ("gridwatch", "dcwatch")

    def _store(self, session: Session, rows, area: str, region: str, series: str, unit: str, fetch_id: int) -> tuple[int, int]:
        existing = {(r.series, r.ts): r for r in session.scalars(select(GridReading).where(
            GridReading.region == region, GridReading.series.in_(list(IC_FIELDS.values()) if series == "ic" else [series]),
            GridReading.ts >= min(to_utc(x["EffectiveTime"]) for x in rows), GridReading.ts <= max(to_utc(x["EffectiveTime"]) for x in rows)))}
        created = changed = 0
        for x in rows:
            if x.get("Value") is None:
                continue
            s = IC_FIELDS.get(x.get("FieldName"), None) if series == "ic" else series
            if s is None:
                continue
            ts = to_utc(x["EffectiveTime"])
            v = float(x["Value"])
            cur = existing.get((s, ts))
            if cur is None:
                row = GridReading(series=s, region=region, ts=ts, value=v, unit=unit, fetch_id=fetch_id)
                session.add(row)
                existing[(s, ts)] = row
                created += 1
            elif cur.value != v:
                cur.value, cur.fetch_id = v, fetch_id
                changed += 1
        return created, changed

    def run(self, session: Session, client, today: date | None = None) -> Result:
        today = today or datetime.now(DUBLIN).date()
        res = Result()
        empty = not session.scalar(select(func.count(GridReading.id)))
        for area, region, series, unit in ACTUALS:
            windows = [(today - timedelta(days=2), today, "day")]
            if empty and (area, region) in BACKFILL and settings.backfill_days > 3:
                start = today - timedelta(days=settings.backfill_days)
                d = start
                windows = []
                while d < today:  # 'month' windows return the ~30 days ending at dateFrom
                    end = min(d + timedelta(days=28), today)
                    windows.append((end, end, "month"))
                    d = end
                windows.append((today - timedelta(days=2), today, "day"))
            for d0, d1, rng in windows:
                r, rows = _get(client, area, region, d0, d1, rng)
                fl = log_fetch(session, self.key, str(r.url), sha256(r.content), len(r.content), self.licence)
                res.fetched += len(rows)
                if rows:
                    c, ch = self._store(session, rows, area, region, series, unit, fl.id)
                    res.created += c
                    res.changed += ch
                session.commit()
        taken = utcnow().replace(second=0, microsecond=0)
        for area, region, series in FORECASTS:
            r, rows = _get(client, area, region, today, today + timedelta(days=1))
            fl = log_fetch(session, self.key, str(r.url), sha256(r.content), len(r.content), self.licence, note="forecast snapshot")
            for x in rows:
                if x.get("Value") is None:
                    continue
                ts = to_utc(x["EffectiveTime"])
                if ts <= taken:  # keep only the part that is still a forecast
                    continue
                session.add(GridForecastSnap(series=series, region=region, taken_at=taken, ts=ts, value=float(x["Value"]), fetch_id=fl.id))
            session.commit()
        r, rows = _get(client, "fuelmix", "ALL", today, today)
        fl = log_fetch(session, self.key, str(r.url), sha256(r.content), len(r.content), self.licence, note="fuel mix, day to date")
        for x in rows:
            if x.get("Value") is None:
                continue
            ts = to_utc(x["EffectiveTime"])
            fuel = str(x.get("FieldName", "")).replace("FUEL_", "").lower()
            if not session.scalar(select(FuelMix.id).where(FuelMix.region == "ALL", FuelMix.ts == ts, FuelMix.fuel == fuel)):
                session.add(FuelMix(region="ALL", ts=ts, fuel=fuel, value=float(x["Value"]), fetch_id=fl.id))
        self._records(session)
        res.message = f"{res.created} new readings" + (" (history backfilled)" if empty else "")
        return res

    def _records(self, session: Session) -> None:
        """Flag a new all-time high for hourly island wind output (since our records began)."""
        rows = session.execute(select(GridReading.ts, GridReading.value).where(GridReading.series == "wind", GridReading.region == "ALL")).all()
        if len(rows) < 96 * 30:
            return
        top = max(rows, key=lambda r: r[1])
        add_event(session, "wind_record", f"New island wind output high in our records: {top[1]:,.0f} MW at {top[0]:%d %b %Y %H:%M} UTC",
                  key=f"wind_record:{top[0]:%Y%m%d%H%M}", source_url=self.url, event_date=top[0])
