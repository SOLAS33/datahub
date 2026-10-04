"""Weather -> renewable output for a fleet spread over regions.

`RenewableModel.forecast(...)` takes hourly per-point weather from one or more sources and
returns, per hour: the central estimate (from the blended weather), each source's own
estimate (run through the same model), and a P10-P90 band. The band is the wider of
(a) the spread between source runs and (b) the empirical error quantiles for that lead
time, when the caller supplies them from verification history. Until enough history exists
the band falls back to a stated prior (see PRIOR_BAND), and says so.

All parameters live in `RenewableParams` so a calibration (s33weather.calibrate) can fit them
and the fitted values can be published with the forecast.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

from .blend import Blended, blend
from .models import Location, WeatherStep
from .physics import solar as sol
from .physics import wind as wnd
from .timeseries import to_hourly

# Prior uncertainty (fraction of installed capacity, +/-) by lead hours, used until the
# model has its own verification history. Deliberately generous.
PRIOR_BAND = [(6, 0.06), (24, 0.10), (48, 0.14), (72, 0.18), (120, 0.22), (10_000, 0.26)]


@dataclass
class RenewableParams:
    alpha: float = 0.16          # wind shear exponent, 10 m -> hub
    hub_m: float = 90.0
    rated: float = 12.5          # fleet rated speed (m/s)
    sigma: float = 1.6           # fleet spatial spread (m/s)
    wind_loss: float = 0.90      # availability, wake, electrical losses
    solar_pr: float = 0.85       # PV performance ratio
    solar_tilt: float = 30.0
    version: str = "prior-2026-10"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HourOutput:
    valid_at: datetime
    lead_hours: float | None
    wind_mw: float | None
    solar_mw: float | None
    wind_by_region: dict[str, float] = field(default_factory=dict)
    solar_by_region: dict[str, float] = field(default_factory=dict)
    wind_members: dict[str, float] = field(default_factory=dict)   # source -> national wind MW
    wind_p10: float | None = None
    wind_p90: float | None = None
    band_basis: str = "prior"                                      # prior | verified | spread
    mean_wind_ms: float | None = None                              # capacity-weighted 10 m wind
    max_gust_ms: float | None = None
    spread_ms: float | None = None                                 # capacity-weighted inter-model spread


def prior_band(lead_h: float | None) -> float:
    lead = lead_h or 0
    for lim, frac in PRIOR_BAND:
        if lead < lim:
            return frac
    return PRIOR_BAND[-1][1]


class RenewableModel:
    def __init__(self, wind_points: list[Location], solar_points: list[Location], wind_capacity: dict[str, float],
                 solar_capacity: dict[str, float], params: RenewableParams | None = None):
        self.wind_points, self.solar_points = wind_points, solar_points
        self.wind_capacity, self.solar_capacity = wind_capacity, solar_capacity
        self.set_params(params or RenewableParams())

    def set_params(self, params: RenewableParams) -> None:
        self.params = params
        self.fleet = wnd.FleetCurve(curve=wnd.PowerCurve(rated=params.rated), sigma=params.sigma)

    @property
    def total_wind_mw(self) -> float:
        return sum(self.wind_capacity.values())

    def point_wind_cf(self, v10, temp_c=None, pressure_hpa=None, rh_pct=None, altitude_m=0.0) -> float | None:
        p = self.params
        return wnd.capacity_factor(v10, temp_c, pressure_hpa, rh_pct, altitude_m or 0.0, p.hub_m, p.alpha, self.fleet, p.wind_loss)

    def _region_mw(self, cf_by_point: dict[str, float], points: list[Location], capacity: dict[str, float]) -> dict[str, float]:
        out: dict[str, float] = {}
        for region, cap in capacity.items():
            pts = [pt for pt in points if region in pt.tags and cf_by_point.get(pt.id) is not None]
            if not pts:
                continue
            tw = sum(pt.weight for pt in pts)
            out[region] = cap * sum(cf_by_point[pt.id] * pt.weight for pt in pts) / tw
        return out

    def forecast(self, steps_by_source: dict[str, dict[str, list[WeatherStep]]], error_quantiles: dict[str, dict] | None = None,
                 lead_bucket=None) -> list[HourOutput]:
        """steps_by_source: {source: {location_id: [steps]}} (any resolution - resampled hourly here).
        error_quantiles: {lead bucket name: {0.1: err, 0.9: err}} in MW (forecast - actual)."""
        hourly: dict[str, dict[str, list[WeatherStep]]] = {src: {lid: to_hourly(st) for lid, st in pts.items()} for src, pts in steps_by_source.items()}
        loc_ids = {pt.id for pt in self.wind_points + self.solar_points}
        blended: dict[str, dict[datetime, Blended]] = {}
        for lid in loc_ids:
            per_src = {src: pts.get(lid, []) for src, pts in hourly.items() if pts.get(lid)}
            blended[lid] = {b.valid_at: b for b in blend(per_src)}
        times = sorted(set().union(*[set(b) for b in blended.values()])) if blended else []
        alt = {lid: next((s.altitude_m for src in hourly.values() for s in src.get(lid, []) if s.altitude_m is not None), 0.0) for lid in loc_ids}
        cap_w = self.total_wind_mw
        out = []
        for t in times:
            wcf, scf, members_cf = {}, {}, {}
            lead = None
            gusts, wsum, wwt, spr, sprw = [], 0.0, 0.0, 0.0, 0.0
            for pt in self.wind_points:
                b = blended.get(pt.id, {}).get(t)
                if not b:
                    continue
                lead = b.lead_hours if lead is None or (b.lead_hours is not None and b.lead_hours < lead) else lead
                wcf[pt.id] = self.point_wind_cf(b.wind_speed, b.temp_c, b.pressure_hpa, b.rh_pct, alt.get(pt.id))
                for src, m in b.members.items():
                    members_cf.setdefault(src, {})[pt.id] = self.point_wind_cf(m.wind_speed, m.temp_c, m.pressure_hpa, m.rh_pct, alt.get(pt.id))
                if b.wind_speed is not None:
                    wsum += b.wind_speed * pt.weight
                    wwt += pt.weight
                if b.wind_gust is not None:
                    gusts.append(b.wind_gust)
                if b.wind_speed_spread is not None:
                    spr += b.wind_speed_spread * pt.weight
                    sprw += pt.weight
            for pt in self.solar_points:
                b = blended.get(pt.id, {}).get(t)
                if not b:
                    continue
                scf[pt.id] = sol.pv_capacity_factor(t, pt.lat, pt.lon, ghi=b.ghi_wm2, cloud_pct=b.cloud_pct, air_c=b.temp_c,
                                                    tilt=self.params.solar_tilt, performance_ratio=self.params.solar_pr)
            w_reg = self._region_mw(wcf, self.wind_points, self.wind_capacity)
            s_reg = self._region_mw(scf, self.solar_points, self.solar_capacity)
            # only report a national figure when every region is covered (no silent partial totals)
            wind_mw = sum(w_reg.values()) if len(w_reg) == len(self.wind_capacity) else None
            solar_mw = sum(s_reg.values()) if len(s_reg) == len(self.solar_capacity) else None
            members = {}
            for src, cfs in members_cf.items():
                reg = self._region_mw(cfs, self.wind_points, self.wind_capacity)
                if len(reg) == len(self.wind_capacity):
                    members[src] = sum(reg.values())
            h = HourOutput(valid_at=t, lead_hours=lead, wind_mw=wind_mw, solar_mw=solar_mw, wind_by_region=w_reg, solar_by_region=s_reg,
                           wind_members=members, mean_wind_ms=(wsum / wwt) if wwt else None, max_gust_ms=max(gusts) if gusts else None,
                           spread_ms=(spr / sprw) if sprw else None)
            if wind_mw is not None:
                half = prior_band(lead) * cap_w
                lo, hi, basis = wind_mw - half, wind_mw + half, "prior"
                bname = lead_bucket(lead) if (lead_bucket and lead is not None) else None
                if error_quantiles and bname and bname in error_quantiles:
                    q = error_quantiles[bname]
                    # error = forecast - actual, so actual = forecast - error
                    lo, hi, basis = wind_mw - q[0.9], wind_mw - q[0.1], "verified"
                if members:
                    lo, hi = min(lo, min(members.values())), max(hi, max(members.values()))
                h.wind_p10, h.wind_p90, h.band_basis = max(0.0, lo), min(cap_w, hi), basis
            out.append(h)
        return out
