"""Thermodynamic helpers: humidity conversions, feels-like temperatures and degree days
(degree days drive heating demand, which matters for electricity and gas load models)."""
from __future__ import annotations

import math


def dewpoint_c(temp_c: float, rh_pct: float) -> float:
    """Magnus formula inverse."""
    a, b = 17.625, 243.04
    g = math.log(max(rh_pct, 0.1) / 100) + a * temp_c / (b + temp_c)
    return b * g / (a - g)


def rh_from_dewpoint(temp_c: float, dew_c: float) -> float:
    a, b = 17.625, 243.04
    return 100 * math.exp(a * dew_c / (b + dew_c) - a * temp_c / (b + temp_c))


def wind_chill_c(temp_c: float, wind_ms: float) -> float:
    """JAG/TI (Environment Canada / NWS 2001), valid for T <= 10 C and wind >= 4.8 km/h."""
    v = wind_ms * 3.6
    if temp_c > 10 or v < 4.8:
        return temp_c
    return 13.12 + 0.6215 * temp_c - 11.37 * v ** 0.16 + 0.3965 * temp_c * v ** 0.16


def heat_index_c(temp_c: float, rh_pct: float) -> float:
    """Rothfusz regression (NWS); returns temp unchanged below 27 C where it is not defined."""
    if temp_c < 27:
        return temp_c
    t = temp_c * 9 / 5 + 32
    hi = (-42.379 + 2.04901523 * t + 10.14333127 * rh_pct - 0.22475541 * t * rh_pct - 6.83783e-3 * t * t
          - 5.481717e-2 * rh_pct * rh_pct + 1.22874e-3 * t * t * rh_pct + 8.5282e-4 * t * rh_pct * rh_pct - 1.99e-6 * t * t * rh_pct * rh_pct)
    return (hi - 32) * 5 / 9


def heating_degree_hours(temp_c: float | None, base_c: float = 15.5) -> float:
    """Ireland (SEAI) uses a 15.5 C base for heating degree days."""
    return 0.0 if temp_c is None else max(0.0, base_c - temp_c)


def cooling_degree_hours(temp_c: float | None, base_c: float = 22.0) -> float:
    return 0.0 if temp_c is None else max(0.0, temp_c - base_c)
