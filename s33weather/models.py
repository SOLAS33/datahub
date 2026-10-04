"""Common data shapes for every source, all in SI units and UTC.

Every fetch returns a `Fetch`: the parsed records plus a `Provenance` that records exactly
where they came from (URL, licence, retrieval time, model run, SHA-256 of the raw payload).
Anything derived later can cite that provenance, so a published number is always traceable
back to the byte-exact response it was computed from.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Location:
    """A named point. `weight` lets a set of points stand for a region (e.g. its share of
    installed wind capacity); `tags` group points (region codes, 'coastal', ...)."""
    id: str
    name: str
    lat: float
    lon: float
    weight: float = 1.0
    tags: tuple[str, ...] = ()


@dataclass
class Provenance:
    source: str            # stable key, e.g. "metie" / "metno"
    publisher: str
    url: str               # the exact request URL
    licence: str
    retrieved_at: datetime
    sha256: str            # of the raw response body
    bytes: int
    model_runs: list[dict] = field(default_factory=list)  # [{model, run_time, valid_from, valid_to}]
    issued_at: datetime | None = None  # when the forecast was issued (latest model run used)
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("retrieved_at", "issued_at"):
            if d.get(k):
                d[k] = d[k].isoformat()
        return d


@dataclass
class WeatherStep:
    """One forecast valid time at one point. Fields are None when the source does not give them.

    Wind: 10 m above ground unless stated. Precipitation is the total over the
    `precip_period_h` hours ending at `valid_at`.
    """
    location_id: str
    source: str
    valid_at: datetime
    issued_at: datetime | None = None
    model: str | None = None              # e.g. "harmonie", "ecmwf"
    wind_speed: float | None = None       # m/s at 10 m
    wind_dir: float | None = None         # degrees, direction wind blows FROM
    wind_gust: float | None = None        # m/s
    wind_speed_p10: float | None = None   # m/s, source-provided percentile if any
    wind_speed_p90: float | None = None
    temp_c: float | None = None
    dewpoint_c: float | None = None
    rh_pct: float | None = None
    pressure_hpa: float | None = None     # mean sea level
    cloud_pct: float | None = None
    cloud_low_pct: float | None = None
    cloud_mid_pct: float | None = None
    cloud_high_pct: float | None = None
    fog_pct: float | None = None
    ghi_wm2: float | None = None          # global horizontal irradiance
    precip_mm: float | None = None
    precip_min_mm: float | None = None
    precip_max_mm: float | None = None
    precip_prob_pct: float | None = None
    precip_period_h: float | None = None
    symbol: str | None = None
    altitude_m: float | None = None

    @property
    def lead_hours(self) -> float | None:
        if self.issued_at is None:
            return None
        return (self.valid_at - self.issued_at).total_seconds() / 3600


@dataclass
class Observation:
    station_id: str
    station_name: str
    observed_at: datetime
    source: str
    wind_speed: float | None = None   # m/s (converted from the source's units)
    wind_dir: float | None = None
    wind_gust: float | None = None
    temp_c: float | None = None
    rh_pct: float | None = None
    pressure_hpa: float | None = None
    rain_mm: float | None = None
    weather: str | None = None


@dataclass
class Warning:
    id: str
    source: str
    level: str               # yellow | orange | red (lower-case)
    type: str                # e.g. "Wind", "Rain"
    headline: str
    description: str
    regions: list[str]
    onset: datetime | None
    expiry: datetime | None
    issued: datetime | None = None
    updated: datetime | None = None
    status: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class Fetch:
    records: list
    provenance: Provenance
