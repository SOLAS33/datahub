"""Named locations for Ireland: weather-sampling points for each EirGrid renewable region,
and Met Éireann synoptic stations.

REGION POINTS. EirGrid reports dispatch-down and installed capacity for six regions in
Ireland (MID, NE, NW, SE, SW, W) plus Northern Ireland. Region boundaries are not published
as geometry, so each region is sampled at 1-3 representative points placed over its
known wind-farm clusters (approximate, documented here, and refined by calibration). Each
point's `weight` is its share of that region's capacity. Capacities (MW, controllable
farms) come from EirGrid's DD Summary Report "Regional Wind & Solar" farm list, read
2026-10-04 (V22); gridwatch re-reads them from every new workbook.

STATIONS. Slugs for Met Éireann's observation feed, each checked live on 2026-10-04
(an unknown slug silently returns Dublin Airport - see sources.metie_obs).
"""
from __future__ import annotations

from .models import Location

# (region, point id, name, lat, lon, share of region)
_WIND_POINTS = [
    ("SW", "sw-kerry-north", "North Kerry / Tralee uplands", 52.33, -9.55, 0.40),
    ("SW", "sw-cork-north", "North Cork / Limerick border", 52.25, -8.90, 0.35),
    ("SW", "sw-cork-west", "West Cork", 51.80, -9.15, 0.25),
    ("W", "w-galway-connemara", "Connemara / Galway west", 53.38, -9.30, 0.40),
    ("W", "w-mayo-north", "North Mayo", 54.15, -9.40, 0.30),
    ("W", "w-clare-west", "West Clare", 52.75, -9.30, 0.30),
    ("MID", "mid-tipperary", "Tipperary / Slieve Felim", 52.70, -8.10, 0.40),
    ("MID", "mid-offaly", "Offaly midland bogs", 53.20, -7.65, 0.35),
    ("MID", "mid-laois", "Laois / Slieve Bloom", 53.00, -7.40, 0.25),
    ("NW", "nw-donegal", "Donegal uplands", 54.85, -8.05, 0.60),
    ("NW", "nw-sligo-leitrim", "Sligo / Leitrim", 54.18, -8.30, 0.40),
    ("NE", "ne-cavan-monaghan", "Cavan / Monaghan", 54.00, -7.05, 1.00),
    ("SE", "se-wexford", "Wexford", 52.45, -6.65, 0.50),
    ("SE", "se-waterford", "Waterford / Comeragh", 52.20, -7.55, 0.50),
    ("NI", "ni-antrim", "Antrim plateau", 54.95, -6.20, 0.35),
    ("NI", "ni-tyrone", "Tyrone / Sperrins", 54.65, -7.15, 0.40),
    ("NI", "ni-fermanagh", "Fermanagh / Derry", 54.85, -7.35, 0.25),
]

# Controllable installed capacity (MW) by region, EirGrid DD Summary Report V22 farm list.
WIND_CAPACITY_MW = {"MID": 924.0, "NE": 215.2, "NW": 388.9, "SE": 319.9, "SW": 1530.3, "W": 1147.8, "NI": 1192.5}
SOLAR_CAPACITY_MW = {"MID": 323.0, "NE": 438.1, "SE": 391.8, "SW": 164.3, "W": 68.0, "NI": 118.2}
CAPACITY_SOURCE = "EirGrid DD-Summary-Report-V22.xlsx, sheet 'Regional Wind & Solar', farm list (controllable farms)"

# Solar sampling: one point per region at the main solar clusters (largely flat SE/NE/MID farmland).
_SOLAR_POINTS = [
    ("MID", "sol-mid", "Kildare / Offaly solar belt", 53.15, -7.10),
    ("NE", "sol-ne", "Meath / Louth solar belt", 53.70, -6.60),
    ("SE", "sol-se", "Wexford / Carlow solar belt", 52.55, -6.75),
    ("SW", "sol-sw", "Cork / Limerick solar", 52.15, -8.45),
    ("W", "sol-w", "Galway / Clare solar", 53.10, -8.80),
    ("NI", "sol-ni", "Down / Armagh solar", 54.35, -6.25),
]

REGION_NAMES = {"MID": "Midlands", "NE": "North-East", "NW": "North-West", "SE": "South-East", "SW": "South-West",
                "W": "West", "NI": "Northern Ireland"}

# Approximate visual centres of each region on a map of the island (label/bubble placement only).
REGION_CENTRES = {"MID": (53.05, -7.75), "NE": (53.85, -6.75), "NW": (54.55, -8.15), "SE": (52.45, -6.95),
                  "SW": (52.15, -9.05), "W": (53.55, -9.20), "NI": (54.65, -6.75)}


def wind_points() -> list[Location]:
    return [Location(id=pid, name=name, lat=lat, lon=lon, weight=WIND_CAPACITY_MW[reg] * share, tags=(reg, "wind"))
            for reg, pid, name, lat, lon, share in _WIND_POINTS]


def solar_points() -> list[Location]:
    return [Location(id=pid, name=name, lat=lat, lon=lon, weight=SOLAR_CAPACITY_MW[reg], tags=(reg, "solar"))
            for reg, pid, name, lat, lon in _SOLAR_POINTS]


def all_points() -> list[Location]:
    return wind_points() + solar_points()


# Met Éireann observing stations: slug -> (display name as the feed spells it, lat, lon).
# Coordinates are approximate (to ~0.05 deg) and used for map placement only; the feed does
# not return coordinates.
STATIONS = {
    "valentia": ("Valentia", 51.938, -10.241), "malin-head": ("Malin Head", 55.372, -7.339),
    "belmullet": ("Belmullet", 54.228, -10.007), "mace-head": ("Mace Head", 53.326, -9.904),
    "sherkin-island": ("Sherkin Island", 51.476, -9.428), "roches-point": ("Roche's Point", 51.793, -8.244),
    "finner": ("Finner", 54.494, -8.243), "claremorris": ("Claremorris", 53.711, -8.993),
    "athenry": ("Athenry", 53.289, -8.786), "gurteen": ("Gurteen", 53.051, -8.009),
    "mullingar": ("Mullingar", 53.537, -7.362), "ballyhaise": ("Ballyhaise", 54.052, -7.310),
    "dunsany": ("Dunsany", 53.516, -6.660), "casement": ("Casement", 53.306, -6.439),
    "phoenix-park": ("Phoenix Park", 53.364, -6.350), "oak-park": ("Oak Park", 52.861, -6.915),
    "johnstown-castle": ("Johnstown Castle", 52.298, -6.497), "moore-park": ("Moore Park", 52.164, -8.264),
}
