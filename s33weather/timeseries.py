"""Time-series utilities: put steps from different models on one hourly grid so they can be
blended and compared. Linear interpolation for scalar fields, vector interpolation for wind
direction, nearest-earlier for categorical fields. Never extrapolates past the source's range."""
from __future__ import annotations

import math
from dataclasses import fields, replace
from datetime import datetime, timedelta

from .models import WeatherStep

SCALARS = ["wind_speed", "wind_gust", "wind_speed_p10", "wind_speed_p90", "temp_c", "dewpoint_c", "rh_pct", "pressure_hpa",
           "cloud_pct", "cloud_low_pct", "cloud_mid_pct", "cloud_high_pct", "fog_pct", "ghi_wm2"]


def floor_hour(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def hourly_grid(start: datetime, end: datetime) -> list[datetime]:
    t = floor_hour(start)
    if t < start:
        t += timedelta(hours=1)
    out = []
    while t <= end:
        out.append(t)
        t += timedelta(hours=1)
    return out


def _lerp(a, b, f):
    if a is None or b is None:
        return a if f < 0.5 else b
    return a + (b - a) * f


def _lerp_dir(a, b, f):
    if a is None or b is None:
        return a if f < 0.5 else b
    ar, br = math.radians(a), math.radians(b)
    x = (1 - f) * math.cos(ar) + f * math.cos(br)
    y = (1 - f) * math.sin(ar) + f * math.sin(br)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def to_hourly(steps: list[WeatherStep], start: datetime | None = None, end: datetime | None = None) -> list[WeatherStep]:
    """Resample one point's steps to every whole hour within the source's own coverage."""
    steps = sorted(steps, key=lambda s: s.valid_at)
    if not steps:
        return []
    start = max(start or steps[0].valid_at, steps[0].valid_at)
    end = min(end or steps[-1].valid_at, steps[-1].valid_at)
    out, j = [], 0
    for t in hourly_grid(start, end):
        while j + 1 < len(steps) and steps[j + 1].valid_at <= t:
            j += 1
        a = steps[j]
        if a.valid_at == t or j + 1 >= len(steps):
            out.append(replace(a, valid_at=t))
            continue
        b = steps[j + 1]
        f = (t - a.valid_at).total_seconds() / (b.valid_at - a.valid_at).total_seconds()
        s = replace(a, valid_at=t)
        for name in SCALARS:
            setattr(s, name, _lerp(getattr(a, name), getattr(b, name), f))
        s.wind_dir = _lerp_dir(a.wind_dir, b.wind_dir, f)
        s.model = a.model if f < 0.5 else b.model
        s.issued_at = a.issued_at if f < 0.5 else b.issued_at
        # precipitation is a period total - do not interpolate it onto an hour it was not reported for
        s.precip_mm = s.precip_min_mm = s.precip_max_mm = s.precip_prob_pct = s.precip_period_h = None
        out.append(s)
    return out


def field_names() -> list[str]:
    return [f.name for f in fields(WeatherStep)]
