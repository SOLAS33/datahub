"""Command line: quick look at any point, or a national renewable forecast.

    python -m s33weather point 53.35 -6.26           # blended hourly forecast for a point
    python -m s33weather renewables                   # national wind + solar forecast (Ireland)
    python -m s33weather warnings                     # Met Éireann warnings in force
"""
from __future__ import annotations

import sys

from . import points
from .blend import blend
from .energy import RenewableModel
from .http import make_client
from .models import Location
from .physics.wind import compass
from .sources import FORECAST_SOURCES, metie_warnings
from .timeseries import to_hourly


def _point(lat: float, lon: float) -> int:
    loc = Location(id="cli", name=f"{lat},{lon}", lat=lat, lon=lon)
    per = {}
    with make_client() as c:
        for key, mod in FORECAST_SOURCES.items():
            try:
                f = mod.fetch(c, loc)
                per[key] = to_hourly(f.records)
                print(f"{key}: {len(f.records)} steps, issued {f.provenance.issued_at}, sha256 {f.provenance.sha256[:12]}")
            except Exception as e:  # noqa: BLE001
                print(f"{key}: FAILED {type(e).__name__}: {e}")
    print(f"{'UTC':16} {'wind':>5} {'spr':>4} {'dir':>4} {'gust':>5} {'temp':>5} {'ghi':>5}")
    for b in blend(per)[:48]:
        f = lambda v, n=1: "-" if v is None else f"{v:.{n}f}"  # noqa: E731
        print(f"{b.valid_at:%Y-%m-%d %H:%M} {f(b.wind_speed):>5} {f(b.wind_speed_spread):>4} {compass(b.wind_dir) or '-':>4} {f(b.wind_gust):>5} {f(b.temp_c):>5} {f(b.ghi_wm2, 0):>5}")
    return 0


def _renewables() -> int:
    wp, sp = points.wind_points(), points.solar_points()
    data: dict[str, dict] = {k: {} for k in FORECAST_SOURCES}
    with make_client() as c:
        for loc in wp + sp:
            for key, mod in FORECAST_SOURCES.items():
                try:
                    data[key][loc.id] = mod.fetch(c, loc).records
                except Exception as e:  # noqa: BLE001
                    print(f"{key} {loc.id}: FAILED {e}")
    m = RenewableModel(wp, sp, points.WIND_CAPACITY_MW, points.SOLAR_CAPACITY_MW)
    for h in m.forecast(data)[:72]:
        print(f"{h.valid_at:%a %d %H:%M} wind {h.wind_mw or 0:6.0f} MW [{h.wind_p10 or 0:5.0f}-{h.wind_p90 or 0:5.0f}] solar {h.solar_mw or 0:5.0f} MW  members {', '.join(f'{k}={v:.0f}' for k, v in h.wind_members.items())}")
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "point" and len(argv) == 3:
        return _point(float(argv[1]), float(argv[2]))
    if argv[0] == "renewables":
        return _renewables()
    if argv[0] == "warnings":
        with make_client() as c:
            f = metie_warnings.fetch(c)
        for w in f.records:
            print(f"{w.level.upper():7} {w.type:10} {w.onset} -> {w.expiry}  {', '.join(w.regions)}  {w.headline}")
        print(f"{len(f.records)} warning(s) in force (sha256 {f.provenance.sha256[:12]})")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
