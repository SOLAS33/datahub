"""Wind-to-power physics, from a 10 m forecast wind to a fleet capacity factor.

Pipeline (each step is a pure function, so it can be tested and reused independently):

1. `shear_power_law` / `shear_log_law` - extrapolate 10 m wind to hub height.
2. `air_density` + `density_adjusted_speed` - a turbine's power depends on rho*v^3; the
   industry convention (IEC 61400-12-1) folds density into an equivalent wind speed.
3. `PowerCurve` - a normalised single-turbine curve (0..1 of rated power).
4. `fleet_curve` - a fleet of many turbines spread over a region never sees one wind speed:
   the single curve is smoothed by a Gaussian of the spatial/temporal spread (sigma), which
   also produces the gradual high-wind shutdown seen in national output.

Defaults describe a modern ~3-4 MW onshore turbine (cut-in 3 m/s, rated ~12.5 m/s,
cut-out 25 m/s with high-wind ride-through to ~28 m/s). They are priors: gridwatch fits
shear exponent, rated speed and loss factor against EirGrid's metered output.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

RHO_STD = 1.225  # kg/m3, ISO standard atmosphere at sea level, 15 C


def shear_power_law(v_ref: float, h_ref: float = 10.0, h: float = 90.0, alpha: float = 0.14) -> float:
    """Hellmann power law: v(h) = v_ref * (h / h_ref) ** alpha. alpha ~0.10-0.12 over open
    sea, ~0.14 open flat land (1/7 rule), 0.2-0.3 over rough/forested terrain or at night."""
    if v_ref is None:
        return None
    return v_ref * (h / h_ref) ** alpha


def shear_log_law(v_ref: float, h_ref: float = 10.0, h: float = 90.0, z0: float = 0.03) -> float:
    """Neutral-stability log law with roughness length z0 (m): 0.0002 sea, 0.03 open
    farmland, 0.1 farmland with hedges (much of Ireland), 0.5-1 forest/suburb."""
    if v_ref is None:
        return None
    return v_ref * math.log(h / z0) / math.log(h_ref / z0)


def alpha_from_z0(z0: float, h_ref: float = 10.0, h: float = 90.0) -> float:
    """Equivalent power-law exponent for a roughness length, over the given height span."""
    return math.log(math.log(h / z0) / math.log(h_ref / z0)) / math.log(h / h_ref)


def saturation_vapour_pressure_hpa(temp_c: float) -> float:
    """Magnus-Tetens (Alduchov & Eskridge 1996)."""
    return 6.1094 * math.exp(17.625 * temp_c / (temp_c + 243.04))


def air_density(temp_c: float | None, pressure_hpa: float | None, rh_pct: float | None = None, altitude_m: float = 0.0,
                hub_m: float = 90.0) -> float:
    """Moist-air density (kg/m3) at hub height from screen temperature, MSL pressure and RH.

    MSL pressure is reduced to hub altitude with the hypsometric relation, temperature with a
    standard lapse rate of 6.5 K/km. Missing inputs fall back to the standard atmosphere."""
    if temp_c is None or pressure_hpa is None:
        return RHO_STD
    z = altitude_m + hub_m                                  # hub height above sea level
    t_sea = temp_c + 273.15 + 0.0065 * altitude_m           # screen temperature carried down to sea level
    t_k = temp_c + 273.15 - 0.0065 * hub_m                  # temperature at hub
    p = pressure_hpa * 100 * (1 - 0.0065 * z / t_sea) ** 5.257  # barometric formula, standard lapse rate
    e = 0.0 if rh_pct is None else (rh_pct / 100) * saturation_vapour_pressure_hpa(temp_c) * 100
    rd, rv = 287.058, 461.495
    return (p - e) / (rd * t_k) + e / (rv * t_k)


def density_adjusted_speed(v: float, rho: float) -> float:
    """IEC 61400-12-1 normalisation: the wind speed that gives the same power at standard density."""
    return v * (rho / RHO_STD) ** (1 / 3)


@dataclass
class PowerCurve:
    """Normalised single-turbine power curve. Between cut-in and rated, output follows a
    cubic-like ramp (v^3 shape blended to a smooth knee, as real curves are). Above
    cut-out the turbine stops; with `ride_through` it ramps down linearly to zero at
    `ride_through_end` instead (modern storm-control behaviour)."""
    cut_in: float = 3.0
    rated: float = 12.5
    cut_out: float = 25.0
    ride_through: bool = True
    ride_through_end: float = 28.0
    knee: float = 0.85  # fraction of the ramp following v^3 before the knee rounds off

    def __call__(self, v: float | None) -> float:
        if v is None or v < self.cut_in:
            return 0.0
        if v < self.rated:
            x = (v ** 3 - self.cut_in ** 3) / (self.rated ** 3 - self.cut_in ** 3)
            # soften the corner at rated speed: blend v^3 shape with a smoothstep near the top
            s = (v - self.cut_in) / (self.rated - self.cut_in)
            smooth = s * s * (3 - 2 * s)
            w = max(0.0, (s - self.knee) / (1 - self.knee)) if self.knee < 1 else 0.0
            return max(0.0, min(1.0, (1 - w) * x + w * smooth))
        if v <= self.cut_out:
            return 1.0
        if self.ride_through and v < self.ride_through_end:
            return (self.ride_through_end - v) / (self.ride_through_end - self.cut_out)
        return 0.0


# A few representative curves. Low-wind (IEC III) turbines reach rated output earlier.
CURVES = {
    "iec1": PowerCurve(cut_in=3.5, rated=13.5, cut_out=25.0),
    "iec2": PowerCurve(cut_in=3.0, rated=12.5, cut_out=25.0),
    "iec3": PowerCurve(cut_in=2.5, rated=11.0, cut_out=22.0, ride_through_end=25.0),
    "offshore": PowerCurve(cut_in=3.5, rated=12.0, cut_out=30.0, ride_through_end=32.0),
}


@dataclass
class FleetCurve:
    """A single curve smoothed by a Gaussian of wind-speed spread `sigma` (m/s), tabulated
    once for speed. Represents many turbines over an area seeing a distribution of speeds."""
    curve: PowerCurve = field(default_factory=PowerCurve)
    sigma: float = 1.6
    step: float = 0.1
    vmax: float = 40.0
    _table: list[float] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        n = int(self.vmax / self.step) + 1
        offsets = [i * self.step for i in range(-int(4 * self.sigma / self.step), int(4 * self.sigma / self.step) + 1)] if self.sigma > 0 else [0.0]
        weights = [math.exp(-0.5 * (o / self.sigma) ** 2) if self.sigma > 0 else 1.0 for o in offsets]
        tw = sum(weights)
        self._table = []
        for i in range(n):
            v = i * self.step
            self._table.append(sum(w * self.curve(max(0.0, v + o)) for o, w in zip(offsets, weights)) / tw)

    def __call__(self, v: float | None) -> float:
        if v is None:
            return 0.0
        if v <= 0:
            return self._table[0]
        x = v / self.step
        i = int(x)
        if i >= len(self._table) - 1:
            return self._table[-1]
        f = x - i
        return self._table[i] * (1 - f) + self._table[i + 1] * f


def capacity_factor(v10: float | None, temp_c: float | None = None, pressure_hpa: float | None = None, rh_pct: float | None = None,
                    altitude_m: float = 0.0, hub_m: float = 90.0, alpha: float = 0.14, fleet: FleetCurve | None = None,
                    loss: float = 0.9) -> float | None:
    """Full chain for one point: 10 m wind -> hub wind -> density-adjusted -> fleet curve ->
    capacity factor after losses (availability, wakes, electrical). Returns None if no wind."""
    if v10 is None:
        return None
    fleet = fleet or FleetCurve()
    v = shear_power_law(v10, 10.0, hub_m, alpha)
    rho = air_density(temp_c, pressure_hpa, rh_pct, altitude_m, hub_m)
    return fleet(density_adjusted_speed(v, rho)) * loss


def gust_factor(speed: float | None, gust: float | None) -> float | None:
    """Gust / mean ratio - a turbulence proxy; > ~1.6 signals convective/turbulent flow."""
    if not speed or gust is None or speed < 1:
        return None
    return gust / speed


def wind_components(speed: float, direction_from: float) -> tuple[float, float]:
    """(u, v) eastward/northward components for a wind blowing FROM `direction_from` degrees."""
    rad = math.radians(direction_from)
    return -speed * math.sin(rad), -speed * math.cos(rad)


def direction_from_components(u: float, v: float) -> float:
    return (math.degrees(math.atan2(-u, -v)) + 360) % 360


def circular_mean_direction(dirs: list[float], weights: list[float] | None = None) -> float | None:
    if not dirs:
        return None
    weights = weights or [1.0] * len(dirs)
    s = sum(w * math.sin(math.radians(d)) for d, w in zip(dirs, weights))
    c = sum(w * math.cos(math.radians(d)) for d, w in zip(dirs, weights))
    if abs(s) < 1e-9 and abs(c) < 1e-9:
        return None
    return (math.degrees(math.atan2(s, c)) + 360) % 360


def beaufort(speed_ms: float | None) -> int | None:
    if speed_ms is None:
        return None
    limits = [0.5, 1.6, 3.4, 5.5, 8.0, 10.8, 13.9, 17.2, 20.8, 24.5, 28.5, 32.7]
    for i, lim in enumerate(limits):
        if speed_ms < lim:
            return i
    return 12


def compass(direction: float | None, points: int = 16) -> str | None:
    if direction is None:
        return None
    names16 = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    names = names16 if points == 16 else names16[::2]
    return names[int((direction % 360) / (360 / len(names)) + 0.5) % len(names)]
