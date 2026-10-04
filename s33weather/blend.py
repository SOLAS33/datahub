"""Multi-model blending and spread.

Given hourly steps from several sources for the same point, produce one consensus value per
hour plus the inter-model spread. Weights can depend on lead time (e.g. trust the 2.5 km
HARMONIE more for the first two days, then lean on the global model). Spread between
independent models is the cheapest honest uncertainty signal there is: when they disagree,
the forecast is genuinely uncertain, whatever any single model says.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import WeatherStep
from .physics.wind import circular_mean_direction


@dataclass
class Blended:
    valid_at: datetime
    location_id: str
    members: dict[str, WeatherStep]
    wind_speed: float | None
    wind_speed_spread: float | None    # max - min across members (m/s)
    wind_dir: float | None
    wind_gust: float | None
    temp_c: float | None
    pressure_hpa: float | None
    rh_pct: float | None
    cloud_pct: float | None
    ghi_wm2: float | None             # only from members that provide it
    lead_hours: float | None


def default_weights(source: str, lead_h: float | None) -> float:
    """Met Éireann's HARMONIE (first ~54 h) is the high-resolution local model: weight it 2:1.
    Beyond that both feeds are ECMWF-based, so weight them equally."""
    if source == "metie" and lead_h is not None and lead_h <= 54:
        return 2.0
    return 1.0


def _wmean(vals: list[tuple[float, float]]) -> float | None:
    vals = [(v, w) for v, w in vals if v is not None and w > 0]
    if not vals:
        return None
    return sum(v * w for v, w in vals) / sum(w for _, w in vals)


def blend(per_source: dict[str, list[WeatherStep]], weight_fn=default_weights) -> list[Blended]:
    """per_source: {source: hourly steps for ONE location}. Hours covered by at least one member are returned."""
    by_time: dict[datetime, dict[str, WeatherStep]] = {}
    for src, steps in per_source.items():
        for s in steps:
            by_time.setdefault(s.valid_at, {})[src] = s
    out = []
    for t in sorted(by_time):
        mem = by_time[t]
        loc = next(iter(mem.values())).location_id
        leads = [s.lead_hours for s in mem.values() if s.lead_hours is not None]
        lead = min(leads) if leads else None
        w = {src: weight_fn(src, s.lead_hours) for src, s in mem.items()}
        speeds = [s.wind_speed for s in mem.values() if s.wind_speed is not None]
        dirs = [(s.wind_dir, w[src]) for src, s in mem.items() if s.wind_dir is not None]
        out.append(Blended(
            valid_at=t, location_id=loc, members=mem,
            wind_speed=_wmean([(s.wind_speed, w[k]) for k, s in mem.items()]),
            wind_speed_spread=(max(speeds) - min(speeds)) if len(speeds) > 1 else None,
            wind_dir=circular_mean_direction([d for d, _ in dirs], [x for _, x in dirs]) if dirs else None,
            wind_gust=_wmean([(s.wind_gust, w[k]) for k, s in mem.items()]),
            temp_c=_wmean([(s.temp_c, w[k]) for k, s in mem.items()]),
            pressure_hpa=_wmean([(s.pressure_hpa, w[k]) for k, s in mem.items()]),
            rh_pct=_wmean([(s.rh_pct, w[k]) for k, s in mem.items()]),
            cloud_pct=_wmean([(s.cloud_pct, w[k]) for k, s in mem.items()]),
            ghi_wm2=_wmean([(s.ghi_wm2, w[k]) for k, s in mem.items()]),
            lead_hours=lead))
    return out
