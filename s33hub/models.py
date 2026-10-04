"""Hub tables. Traceability rule: every stored number traces to a FetchLog row (exact URL,
retrieval time, SHA-256 of the payload) or, for workbook figures, to file, sheet and cell."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import HubBase, utcnow


class FetchLog(HubBase):
    """One row per HTTP payload used - the provenance ledger (published monthly as /v1/ledger)."""
    __tablename__ = "fetch_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(40), index=True)
    url: Mapped[str] = mapped_column(String(600))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime)
    model_runs: Mapped[list | None] = mapped_column(JSON)
    licence: Mapped[str | None] = mapped_column(String(200))
    note: Mapped[str | None] = mapped_column(String(300))


class GridReading(HubBase):
    __tablename__ = "grid_readings"
    __table_args__ = (UniqueConstraint("series", "region", "ts"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    series: Mapped[str] = mapped_column(String(40), index=True)
    region: Mapped[str] = mapped_column(String(8))
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(12))
    fetch_id: Mapped[int | None] = mapped_column(ForeignKey("fetch_log.id"))


class GridForecastSnap(HubBase):
    __tablename__ = "grid_forecast_snaps"
    __table_args__ = (UniqueConstraint("series", "region", "taken_at", "ts"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    series: Mapped[str] = mapped_column(String(40), index=True)
    region: Mapped[str] = mapped_column(String(8))
    taken_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    value: Mapped[float] = mapped_column(Float)
    fetch_id: Mapped[int | None] = mapped_column(ForeignKey("fetch_log.id"))


class FuelMix(HubBase):
    __tablename__ = "fuel_mix"
    __table_args__ = (UniqueConstraint("region", "ts", "fuel"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    region: Mapped[str] = mapped_column(String(8))
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    fuel: Mapped[str] = mapped_column(String(20))
    value: Mapped[float] = mapped_column(Float)
    fetch_id: Mapped[int | None] = mapped_column(ForeignKey("fetch_log.id"))


class Workbook(HubBase):
    __tablename__ = "workbooks"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))
    filename: Mapped[str] = mapped_column(String(200), unique=True)
    url: Mapped[str] = mapped_column(String(600))
    sha256: Mapped[str] = mapped_column(String(64))
    bytes: Mapped[int] = mapped_column(Integer)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    latest_period: Mapped[str | None] = mapped_column(String(10))


class MonthlyStat(HubBase):
    __tablename__ = "monthly_stats"
    __table_args__ = (UniqueConstraint("jurisdiction", "tech", "metric", "period"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    jurisdiction: Mapped[str] = mapped_column(String(4))
    tech: Mapped[str] = mapped_column(String(20))
    metric: Mapped[str] = mapped_column(String(40))
    period: Mapped[str] = mapped_column(String(7), index=True)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(8))
    workbook_id: Mapped[int] = mapped_column(ForeignKey("workbooks.id"))
    sheet: Mapped[str] = mapped_column(String(60))
    cell: Mapped[str] = mapped_column(String(12))


class RegionalStat(HubBase):
    __tablename__ = "regional_stats"
    __table_args__ = (UniqueConstraint("tech", "region", "metric", "period"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tech: Mapped[str] = mapped_column(String(10))
    region: Mapped[str] = mapped_column(String(8))
    metric: Mapped[str] = mapped_column(String(20))
    period: Mapped[str] = mapped_column(String(8))
    value: Mapped[float] = mapped_column(Float)
    workbook_id: Mapped[int] = mapped_column(ForeignKey("workbooks.id"))
    sheet: Mapped[str] = mapped_column(String(60))
    cell: Mapped[str] = mapped_column(String(12))


class Farm(HubBase):
    __tablename__ = "farms"
    __table_args__ = (UniqueConstraint("tech", "name", "node"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    tech: Mapped[str] = mapped_column(String(10))
    jurisdiction: Mapped[str] = mapped_column(String(4))
    region: Mapped[str] = mapped_column(String(8), index=True)
    node: Mapped[str | None] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(200))
    capacity_mw: Mapped[float] = mapped_column(Float)
    workbook_id: Mapped[int] = mapped_column(ForeignKey("workbooks.id"))
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WeatherStepRow(HubBase):
    __tablename__ = "weather_steps"
    __table_args__ = (UniqueConstraint("source", "location_id", "issued_at", "valid_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(20), index=True)
    location_id: Mapped[str] = mapped_column(String(40), index=True)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    valid_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    model: Mapped[str | None] = mapped_column(String(20))
    wind_speed: Mapped[float | None] = mapped_column(Float)
    wind_dir: Mapped[float | None] = mapped_column(Float)
    wind_gust: Mapped[float | None] = mapped_column(Float)
    temp_c: Mapped[float | None] = mapped_column(Float)
    rh_pct: Mapped[float | None] = mapped_column(Float)
    pressure_hpa: Mapped[float | None] = mapped_column(Float)
    cloud_pct: Mapped[float | None] = mapped_column(Float)
    ghi_wm2: Mapped[float | None] = mapped_column(Float)
    precip_mm: Mapped[float | None] = mapped_column(Float)
    precip_period_h: Mapped[float | None] = mapped_column(Float)
    precip_prob_pct: Mapped[float | None] = mapped_column(Float)
    altitude_m: Mapped[float | None] = mapped_column(Float)
    fetch_id: Mapped[int | None] = mapped_column(ForeignKey("fetch_log.id"))


class ObservationRow(HubBase):
    __tablename__ = "observations"
    __table_args__ = (UniqueConstraint("station_id", "observed_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    station_id: Mapped[str] = mapped_column(String(40), index=True)
    station_name: Mapped[str] = mapped_column(String(80))
    observed_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    wind_speed: Mapped[float | None] = mapped_column(Float)
    wind_dir: Mapped[float | None] = mapped_column(Float)
    wind_gust: Mapped[float | None] = mapped_column(Float)
    temp_c: Mapped[float | None] = mapped_column(Float)
    rh_pct: Mapped[float | None] = mapped_column(Float)
    pressure_hpa: Mapped[float | None] = mapped_column(Float)
    rain_mm: Mapped[float | None] = mapped_column(Float)
    weather: Mapped[str | None] = mapped_column(String(80))
    fetch_id: Mapped[int | None] = mapped_column(ForeignKey("fetch_log.id"))


class WarningRow(HubBase):
    __tablename__ = "warnings"
    id: Mapped[int] = mapped_column(primary_key=True)
    warning_id: Mapped[str] = mapped_column(String(120), unique=True)
    level: Mapped[str] = mapped_column(String(10))
    type: Mapped[str] = mapped_column(String(40))
    headline: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    regions: Mapped[list] = mapped_column(JSON)
    onset: Mapped[datetime | None] = mapped_column(DateTime)
    expiry: Mapped[datetime | None] = mapped_column(DateTime)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    raw: Mapped[dict | None] = mapped_column(JSON)
    fetch_id: Mapped[int | None] = mapped_column(ForeignKey("fetch_log.id"))


class Mirror(HubBase):
    """A raw, unparsed copy of a public endpoint that a site parses itself (e.g. DCWatch's
    planning queries). The latest body is published at /v1/mirror/<key>/latest.<ext>; each
    distinct version is kept at /v1/mirror/<key>/<YYYY-MM-DD>-<sha12>.<ext>."""
    __tablename__ = "mirrors"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(60), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    bytes: Mapped[int] = mapped_column(Integer)
    records: Mapped[int] = mapped_column(Integer, default=0)
    ext: Mapped[str] = mapped_column(String(8))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    versioned_path: Mapped[str | None] = mapped_column(String(200))
    fetch_ids: Mapped[list | None] = mapped_column(JSON)
    body: Mapped[bytes | None] = mapped_column(LargeBinary)  # latest body, kept so it can be (re)published


class Published(HubBase):
    """What is on data.solas33.com: path -> SHA-256 of the bytes uploaded. Unchanged files are not re-uploaded."""
    __tablename__ = "published"
    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(String(300), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    bytes: Mapped[int] = mapped_column(Integer)
    rows: Mapped[int | None] = mapped_column(Integer)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SourceRun(HubBase):
    __tablename__ = "source_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(60), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[int] = mapped_column(Integer, default=0)
    changed: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)


EVENT_LABELS = {
    "dd_month": "Curtailment figures", "warning_issued": "Weather warning", "warning_updated": "Warning updated",
    "wind_record": "Wind record", "capacity_change": "Capacity change", "source_failed": "Source failed",
    "source_recovered": "Source recovered", "mirror_changed": "Source updated",
}


class HubEvent(HubBase):
    """Source-level news any site can show (new workbook month, warnings, records, outages)."""
    __tablename__ = "hub_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(30))
    key: Mapped[str | None] = mapped_column(String(160), unique=True)
    source: Mapped[str | None] = mapped_column(String(60))
    summary: Mapped[str] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(String(600))
    event_date: Mapped[datetime | None] = mapped_column(DateTime)
