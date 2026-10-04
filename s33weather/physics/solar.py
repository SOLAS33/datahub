"""Solar geometry, irradiance and PV output.

* `sun_position` - NOAA solar-position algorithm (accuracy ~0.01 deg 1950-2050); enough for
  irradiance work and an order of magnitude simpler than full SPA.
* `clear_sky_ghi` - Haurwitz clear-sky model (needs only the zenith angle).
* `ghi_from_cloud` - Kasten & Czeplak (1980) cloud attenuation, for sources with no
  irradiance field (e.g. MET Norway). Where a source gives GHI (Met Éireann HARMONIE does),
  use it directly - it is far better than any cloud-cover proxy.
* `erbs_split` - Erbs et al. (1982) diffuse fraction from the clearness index.
* `poa_irradiance` - isotropic-sky transposition to a tilted panel.
* `pv_power` - DC/AC output per kWp with NOCT cell-temperature derate.

All angles in degrees, irradiance in W/m2, times timezone-aware (converted to UTC).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

SOLAR_CONSTANT = 1361.0  # W/m2 (Kopp & Lean 2011)


def _jd(t: datetime) -> float:
    t = t.astimezone(timezone.utc)
    y, m = t.year, t.month
    d = t.day + (t.hour + (t.minute + t.second / 60) / 60) / 24
    if m <= 2:
        y, m = y - 1, m + 12
    a = y // 100
    b = 2 - a + a // 4
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + b - 1524.5


def sun_position(t: datetime, lat: float, lon: float) -> tuple[float, float]:
    """(zenith, azimuth) in degrees for a UTC instant; azimuth clockwise from north."""
    jc = (_jd(t) - 2451545.0) / 36525
    l0 = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360
    m = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    e = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    mr = math.radians(m)
    c = math.sin(mr) * (1.914602 - jc * (0.004817 + 0.000014 * jc)) + math.sin(2 * mr) * (0.019993 - 0.000101 * jc) + math.sin(3 * mr) * 0.000289
    true_long = l0 + c
    omega = 125.04 - 1934.136 * jc
    app_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    eps0 = 23 + (26 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60) / 60
    eps = eps0 + 0.00256 * math.cos(math.radians(omega))
    decl = math.degrees(math.asin(math.sin(math.radians(eps)) * math.sin(math.radians(app_long))))
    y = math.tan(math.radians(eps / 2)) ** 2
    l0r = math.radians(l0)
    eq_time = 4 * math.degrees(y * math.sin(2 * l0r) - 2 * e * math.sin(mr) + 4 * e * y * math.sin(mr) * math.cos(2 * l0r)
                               - 0.5 * y * y * math.sin(4 * l0r) - 1.25 * e * e * math.sin(2 * mr))
    tu = t.astimezone(timezone.utc)
    minutes = tu.hour * 60 + tu.minute + tu.second / 60
    tst = (minutes + eq_time + 4 * lon) % 1440
    ha = tst / 4 - 180 if tst / 4 >= 0 else tst / 4 + 180
    latr, decr, har = math.radians(lat), math.radians(decl), math.radians(ha)
    cos_z = math.sin(latr) * math.sin(decr) + math.cos(latr) * math.cos(decr) * math.cos(har)
    zen = math.degrees(math.acos(max(-1.0, min(1.0, cos_z))))
    denom = math.cos(latr) * math.sin(math.radians(zen))
    if abs(denom) < 1e-9:
        az = 180.0 if lat > 0 else 0.0
    else:
        x = (math.sin(latr) * math.cos(math.radians(zen)) - math.sin(decr)) / denom
        az_raw = math.degrees(math.acos(max(-1.0, min(1.0, x))))
        az = (az_raw + 180) % 360 if ha > 0 else (540 - az_raw) % 360
    return zen, az


def extraterrestrial(t: datetime) -> float:
    """Top-of-atmosphere normal irradiance, corrected for Earth-Sun distance."""
    doy = t.timetuple().tm_yday
    return SOLAR_CONSTANT * (1 + 0.033 * math.cos(2 * math.pi * doy / 365))


def clear_sky_ghi(zenith: float) -> float:
    """Haurwitz (1945): GHI_cs = 1098 cos(z) exp(-0.057 / cos(z))."""
    cz = math.cos(math.radians(zenith))
    if cz <= 0.01:
        return 0.0
    return 1098 * cz * math.exp(-0.057 / cz)


def ghi_from_cloud(clear_ghi: float, cloud_pct: float | None) -> float:
    """Kasten & Czeplak (1980): GHI = GHI_cs * (1 - 0.75 * N^3.4), N cloud fraction 0..1."""
    if cloud_pct is None:
        return clear_ghi
    n = max(0.0, min(1.0, cloud_pct / 100))
    return clear_ghi * (1 - 0.75 * n ** 3.4)


def clearness_index(ghi: float, zenith: float, t: datetime) -> float:
    cz = math.cos(math.radians(zenith))
    if cz <= 0.01:
        return 0.0
    return max(0.0, min(1.2, ghi / (extraterrestrial(t) * cz)))


def erbs_split(ghi: float, zenith: float, t: datetime) -> tuple[float, float]:
    """(DNI, DHI) from GHI via the Erbs diffuse-fraction correlation."""
    kt = clearness_index(ghi, zenith, t)
    if kt <= 0.22:
        kd = 1 - 0.09 * kt
    elif kt <= 0.80:
        kd = 0.9511 - 0.1604 * kt + 4.388 * kt ** 2 - 16.638 * kt ** 3 + 12.336 * kt ** 4
    else:
        kd = 0.165
    dhi = ghi * kd
    cz = math.cos(math.radians(zenith))
    dni = (ghi - dhi) / cz if cz > 0.05 else 0.0
    return max(0.0, dni), max(0.0, dhi)


def poa_irradiance(ghi: float, zenith: float, azimuth: float, t: datetime, tilt: float = 30.0, panel_azimuth: float = 180.0,
                   albedo: float = 0.2) -> float:
    """Plane-of-array irradiance, isotropic sky (Liu & Jordan)."""
    if ghi <= 0 or zenith >= 90:
        return 0.0
    dni, dhi = erbs_split(ghi, zenith, t)
    zr, tr = math.radians(zenith), math.radians(tilt)
    cos_aoi = math.cos(zr) * math.cos(tr) + math.sin(zr) * math.sin(tr) * math.cos(math.radians(azimuth - panel_azimuth))
    beam = dni * max(0.0, cos_aoi)
    sky = dhi * (1 + math.cos(tr)) / 2
    ground = ghi * albedo * (1 - math.cos(tr)) / 2
    return beam + sky + ground


def cell_temperature(poa: float, air_c: float | None, noct: float = 45.0) -> float:
    """NOCT model: T_cell = T_air + (NOCT - 20) / 800 * POA."""
    return (air_c if air_c is not None else 10.0) + (noct - 20) / 800 * poa


def pv_power(poa: float, air_c: float | None = None, gamma: float = -0.0037, performance_ratio: float = 0.85, noct: float = 45.0) -> float:
    """AC output per kWp installed (0..~1.05): POA/1000 x temperature derate x PR (inverter,
    wiring, soiling, mismatch, availability)."""
    if poa <= 0:
        return 0.0
    tc = cell_temperature(poa, air_c, noct)
    return max(0.0, poa / 1000 * (1 + gamma * (tc - 25)) * performance_ratio)


def pv_capacity_factor(t: datetime, lat: float, lon: float, ghi: float | None = None, cloud_pct: float | None = None,
                       air_c: float | None = None, tilt: float = 30.0, performance_ratio: float = 0.85) -> float:
    """Whole chain for one point and instant. Uses source GHI when given, else cloud proxy."""
    zen, az = sun_position(t, lat, lon)
    if zen >= 90:
        return 0.0
    g = ghi if ghi is not None else ghi_from_cloud(clear_sky_ghi(zen), cloud_pct)
    return pv_power(poa_irradiance(g, zen, az, t, tilt), air_c, performance_ratio=performance_ratio)


def daylight(t: datetime, lat: float, lon: float) -> bool:
    return sun_position(t, lat, lon)[0] < 90
