"""Weather collectors, built on the reusable s33weather package.

* WeatherForecasts - Met Éireann (HARMONIE/ECMWF) and MET Norway point forecasts for every
  wind/solar sampling point. Each response is logged (URL, SHA-256, model runs) and its steps
  stored; older runs are pruned after GW_KEEP_WEATHER_HOURS.
* WeatherObservations - Met Éireann hourly station readings (today).
* WeatherWarnings - Met Éireann warnings; new / changed warnings become change events.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from s33weather import points
from s33weather.sources import FORECAST_SOURCES, metie_obs, metie_warnings

from ..config import settings
from ..db import naive, utcnow
from ..models import ObservationRow, WarningRow, WeatherStepRow
from .base import Collector, Result, add_event, log_provenance

FIELDS = ["model", "wind_speed", "wind_dir", "wind_gust", "temp_c", "rh_pct", "pressure_hpa", "cloud_pct", "ghi_wm2",
          "precip_mm", "precip_period_h", "precip_prob_pct", "altitude_m"]


class WeatherForecasts(Collector):
    key = "weather_forecasts"
    name = "Weather forecasts (Met Éireann + MET Norway)"
    publisher = "Met Éireann; MET Norway"
    url = "https://data.gov.ie/dataset/met-eireann-forecast-api"
    licence = "CC BY 4.0 (Met Éireann); CC BY 4.0 / NLOD (MET Norway)"
    provides = "Hourly point forecasts to 10 days at 23 wind/solar sampling points from two independent models."
    interval_hours = 3.0
    used_by = ("gridwatch",)

    def run(self, session: Session, client) -> Result:
        res = Result()
        failures = []
        for loc in points.all_points():
            for key, mod in FORECAST_SOURCES.items():
                try:
                    f = mod.fetch(client, loc)
                except Exception as e:  # noqa: BLE001 - one point failing must not lose the rest
                    failures.append(f"{key}/{loc.id}: {type(e).__name__}")
                    continue
                fl = log_provenance(session, f.provenance)
                issued = naive(f.provenance.issued_at)
                have = set(session.execute(select(WeatherStepRow.issued_at, WeatherStepRow.valid_at).where(
                    WeatherStepRow.source == key, WeatherStepRow.location_id == loc.id)).all())
                for s in f.records:
                    va, iss = naive(s.valid_at), naive(s.issued_at) or issued
                    if (iss, va) in have:  # same model run already stored (Met Éireann mixes HARMONIE and ECMWF runs)
                        continue
                    have.add((iss, va))
                    session.add(WeatherStepRow(source=key, location_id=loc.id, issued_at=iss, valid_at=va,
                                               fetch_id=fl.id, **{k: getattr(s, k) for k in FIELDS}))
                    res.created += 1
                res.fetched += len(f.records)
            session.commit()
        cutoff = utcnow() - timedelta(hours=settings.keep_weather_hours)
        session.execute(delete(WeatherStepRow).where(WeatherStepRow.issued_at < cutoff))
        total = len(points.all_points()) * len(FORECAST_SOURCES)
        if len(failures) > total // 2:
            raise RuntimeError(f"{len(failures)} of {total} forecast fetches failed: {', '.join(failures[:6])}")
        res.message = f"{total - len(failures)}/{total} point forecasts" + (f"; failed: {', '.join(failures[:4])}" if failures else "")
        return res


class WeatherObservations(Collector):
    key = "weather_obs"
    name = "Met Éireann station observations"
    publisher = "Met Éireann"
    url = "https://www.met.ie/latest-reports/observations"
    licence = metie_obs.LICENCE
    provides = "Hourly wind, gust, temperature, pressure and rain at 18 synoptic stations (today)."
    interval_hours = 1.0
    used_by = ("gridwatch",)

    def run(self, session: Session, client) -> Result:
        res = Result()
        bad = []
        for slug, (name, _lat, _lon) in points.STATIONS.items():
            try:
                f = metie_obs.fetch(client, slug, name)
            except Exception as e:  # noqa: BLE001
                bad.append(f"{slug}: {type(e).__name__}")
                continue
            fl = log_provenance(session, f.provenance)
            have = set(session.scalars(select(ObservationRow.observed_at).where(ObservationRow.station_id == slug)))
            for o in f.records:
                t = naive(o.observed_at)
                if t in have:
                    continue
                session.add(ObservationRow(station_id=slug, station_name=o.station_name, observed_at=t, wind_speed=o.wind_speed,
                                           wind_dir=o.wind_dir, wind_gust=o.wind_gust, temp_c=o.temp_c, rh_pct=o.rh_pct,
                                           pressure_hpa=o.pressure_hpa, rain_mm=o.rain_mm, weather=o.weather, fetch_id=fl.id))
                res.created += 1
            res.fetched += len(f.records)
        session.execute(delete(ObservationRow).where(ObservationRow.observed_at < utcnow() - timedelta(days=60)))
        if len(bad) == len(points.STATIONS):
            raise RuntimeError(f"every station failed: {bad[:3]}")
        res.message = f"{len(points.STATIONS) - len(bad)}/{len(points.STATIONS)} stations" + (f"; failed: {', '.join(bad[:4])}" if bad else "")
        return res


class WeatherWarnings(Collector):
    key = "weather_warnings"
    name = "Met Éireann weather warnings"
    publisher = "Met Éireann"
    url = metie_warnings.LANDING
    licence = metie_warnings.LICENCE
    provides = "National weather warnings in force (level, type, counties, onset/expiry)."
    interval_hours = 1.0
    used_by = ("gridwatch",)

    def run(self, session: Session, client) -> Result:
        f = metie_warnings.fetch(client)
        fl = log_provenance(session, f.provenance)
        now = utcnow()
        seen = set()
        res = Result(fetched=len(f.records))
        for w in f.records:
            seen.add(w.id)
            row = session.scalar(select(WarningRow).where(WarningRow.warning_id == w.id))
            label = f"{w.level.capitalize()} {w.type.lower()} warning".strip()
            where = ", ".join(w.regions[:8]) + ("..." if len(w.regions) > 8 else "")
            if row is None:
                session.add(WarningRow(warning_id=w.id, level=w.level, type=w.type, headline=w.headline, description=w.description,
                                       regions=w.regions, onset=naive(w.onset), expiry=naive(w.expiry), raw=w.raw, fetch_id=fl.id))
                add_event(session, "warning_issued", f"{label}: {where or 'Ireland'}" + (f" - {w.headline}" if w.headline else ""),
                          key=f"warn:{w.id}", detail=w.description[:1500], source_url=self.url, event_date=w.onset)
                res.created += 1
            else:
                if (row.level, row.expiry, sorted(row.regions or [])) != (w.level, naive(w.expiry), sorted(w.regions)):
                    add_event(session, "warning_updated", f"{label} updated: {where}", key=f"warn:{w.id}:{w.updated or now:%Y%m%d%H%M}",
                              source_url=self.url, event_date=w.updated)
                    res.changed += 1
                row.level, row.type, row.headline, row.description = w.level, w.type, w.headline, w.description
                row.regions, row.onset, row.expiry, row.raw, row.fetch_id = w.regions, naive(w.onset), naive(w.expiry), w.raw, fl.id
                row.last_seen, row.active = now, True
        for row in session.scalars(select(WarningRow).where(WarningRow.active.is_(True))):
            if row.warning_id not in seen:
                row.active = False
        res.message = f"{len(f.records)} warning(s) in force"
        return res
