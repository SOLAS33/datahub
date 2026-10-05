"""Environmental Protection Agency (EPA) open data: licensed facilities, emission and monitoring points, water and air monitoring
stations, drinking-water remedial actions, bathing water.

* `epa_wfs` - curated layers from the EPA's GeoServer (https://gis.epa.ie/geoserver/EPA/ows, WFS, GeoJSON). Each layer is published
  twice: the GeoJSON as served (gzipped) and a flat CSV (properties plus lon/lat; polygons use the centre of their bounding box).
  EPA writes missing values as the text "None"; the CSV turns those into empty cells.
* `epa_bathing_water` - the EPA bathing water API (https://data.epa.ie/bw/api/v1): every designated bathing water with its
  classification and facilities, and the pollution incidents/restrictions it announces (kept as a growing history).

All EPA datasets listed here carry a Creative Commons Attribution 4.0 licence on data.gov.ie.
"""
from __future__ import annotations

import json
import re

import httpx
from sqlalchemy.orm import Session

from s33weather.models import sha256

from ..db import utcnow
from .base import Collector, Result, get_with_retry, log_fetch
from .tables import Tally, csv_bytes, gz, load_state, publish_table, save_state

WFS = "https://gis.epa.ie/geoserver/EPA/ows"
MAX_BYTES = 90_000_000

# layer typeName -> (family, title). Every layer was fetched and counted; the very large map layers (river network 117k, radon grid 68k,
# illegal-waste risk 1.6M) and the road-noise contours (over 90 MB) are left out are left out.
LAYERS = {
    # licensed and regulated activities
    "LEMA_Facilities_P_IPPC_IPC_IEL": ("facilities", "Industrial emissions, IPC and waste licensed facilities (combined)"),
    "LEMA_Facilities_IEL": ("facilities", "Industrial Emissions licensed facilities"), "LEMA_Facilities_IPC": ("facilities", "Integrated Pollution Control licensed sites"),
    "LEMA_Facilities_Waste": ("facilities", "Licensed waste facilities"), "LEMA_Facilties_Extractive_Facilities": ("facilities", "Extractive industries registered sites"),
    "LEMA_Facilities_UWW": ("facilities", "Urban waste water treatment plants"), "IPPC_LicFacilities": ("facilities", "Licensed IPPC facilities"),
    "IPPC_Boundary": ("facilities", "IPPC facility boundaries"), "WASTE_Boundary": ("facilities", "Waste facility boundaries"),
    "IPPC_EmissionPts": ("emissions", "IPPC facility emission points"), "IPPC_MonitoringPts": ("emissions", "IPPC facility monitoring points"),
    "WST_EmissionPts": ("emissions", "Waste facility emission points"), "LEMA_EMISSIONPTSSNAPPED": ("emissions", "Urban waste water emission points"),
    "WFD_Section4s": ("emissions", "Water Framework Directive Section 4 discharge licences"), "UWW_AgglomerationBoundaries": ("emissions", "Urban waste water agglomeration boundaries"),
    # water
    "MON_WaterStations": ("water", "Water monitoring stations"), "MON_Hydrometricgauges": ("water", "Hydrometric gauges"),
    "MON_QRecords_Version2": ("water", "River biological quality (Q-value) records"), "BathingWaterQuality": ("water", "Designated bathing waters and current classification"),
    "WFD_RPA_BATHINGWATERAREAS": ("water", "Protected areas: bathing water areas"), "DW_RAL": ("water", "Drinking water Remedial Action List"),
    "WFD_LWB_ApprovedRisk_Cycle3": ("water", "Lake waterbody risk (cycle 3)"), "WFD_GWB_ApprovedRisk": ("water", "Groundwater waterbody risk (cycle 2)"),
    "WFD_TWB_ApprovedRisk_Cycle3": ("water", "Transitional waterbody risk (cycle 3)"), "WFD_CWB_ApprovedRisk_Cycle3": ("water", "Coastal waterbody risk (cycle 3)"),
    "WFD_TransitionalWaterQuality_20182020": ("water", "Transitional water quality 2018-2020"), "WFD_CoastalWaterQuality_20182020": ("water", "Coastal water quality 2018-2020"),
    "WFD_AFA_Plans": ("water", "Water Framework Directive Areas for Action"), "WFD_CATCHMENTPROJECTS": ("water", "Catchment projects"),
    "SIIF_AllPriorityUrban": ("water", "EPA priority urban areas (waste water)"), "DAS_DumpBoundaries": ("water", "Dumping at sea sites"),
    # air, radiation, noise, ecosystems
    "AIR_MonitoringSites": ("air", "Air quality monitoring sites"), "AIR_EMEP_MONITORINGSITES": ("air", "EMEP air monitoring sites"),
    "RAD_MONITORINGSTATION": ("radiation", "Radiation monitoring locations"), "RAD_RadonMap": ("radiation", "Radon map (10 km grid)"),
    "MON_NEMN_TerrestrialSites": ("ecosystems", "National Ecosystems Monitoring Network: terrestrial"), "MON_NEMN_LakeSites": ("ecosystems", "National Ecosystems Monitoring Network: lakes"),
    "MON_NEMN_AtmosphericSites": ("ecosystems", "National Ecosystems Monitoring Network: atmospheric"),
    "NOISE_Rd3_Rail_Day": ("noise", "Noise round 3: rail, Lden"), "NOISE_Rd3_Airport_Day": ("noise", "Noise round 3: airports, Lden"),
    "MINES_SiteLocation": ("mines", "Historic mine site districts"), "MINES_PointFeatures": ("mines", "Mine point features"),
}


def _centre(geom: dict | None) -> tuple[float | None, float | None]:
    """lon, lat of a point, or the middle of a geometry's bounding box."""
    if not geom or not geom.get("coordinates"):
        return None, None
    xs, ys = [], []

    def walk(c):
        if c and isinstance(c[0], (int, float)):
            xs.append(c[0])
            ys.append(c[1])
        else:
            for i in c or []:
                walk(i)

    walk(geom["coordinates"])
    if not xs:
        return None, None
    return round((min(xs) + max(xs)) / 2, 6), round((min(ys) + max(ys)) / 2, 6)


def flatten_features(doc: dict) -> tuple[list[str], list[list]]:
    """(columns, rows) of a GeoJSON FeatureCollection: every property, geometry type, lon, lat."""
    feats = doc.get("features", [])
    cols: list[str] = []
    for f in feats:
        for k in (f.get("properties") or {}):
            if k not in cols:
                cols.append(k)
    rows = []
    for f in feats:
        p = f.get("properties") or {}
        lon, lat = _centre(f.get("geometry"))
        vals = []
        for c in cols:
            v = p.get(c)
            vals.append("" if v is None or v == "None" else re.sub(r"\s+", " ", str(v)).strip() if not isinstance(v, (dict, list)) else json.dumps(v))
        rows.append(vals + [(f.get("geometry") or {}).get("type", ""), lon, lat])
    return cols + ["geometry_type", "lon", "lat"], rows


class EpaWfsLayers(Collector):
    key = "epa_wfs"
    name = "EPA licensed facilities, emission points, water and air monitoring (GeoServer layers)"
    publisher = "Environmental Protection Agency (EPA)"
    url = "https://gis.epa.ie/"
    licence = "Creative Commons Attribution 4.0"
    provides = f"{len(LAYERS)} EPA layers as GeoJSON and flat CSV (facilities, emission points, water stations, hydrometric gauges, air sites, remedial actions...) with an index."
    interval_hours = 24.0
    domain, tier = "environment", "files"

    def fetch(self, client: httpx.Client, layer: str) -> tuple[bytes, dict, str]:
        params = {"service": "WFS", "version": "1.0.0", "request": "GetFeature", "typeName": f"EPA:{layer}", "outputFormat": "application/json", "srsName": "EPSG:4326",
                  "maxFeatures": 200000}
        r = get_with_retry(client, WFS, params=params, timeout=300)
        raw = r.content
        if len(raw) > MAX_BYTES:
            raise ValueError("response too large")
        if raw[:1] != b"{":
            raise ValueError("not GeoJSON (service exception)")
        doc = json.loads(raw)
        total = doc.get("totalFeatures")
        if isinstance(total, int) and total != len(doc.get("features", [])):
            raise ValueError(f"truncated: {len(doc.get('features', []))} of {total} features")
        return raw, doc, str(r.url)

    def run(self, session: Session, client: httpx.Client) -> Result:
        state = load_state(session, self.key)
        items = state.setdefault("items", {})
        tally = Tally()
        for layer, (family, title) in LAYERS.items():
            try:
                raw, doc, url = self.fetch(client, layer)
            except Exception as e:  # noqa: BLE001 - one layer failing must not lose the others
                tally.failed.append(f"{layer} ({type(e).__name__})")
                continue
            log_fetch(session, self.key, url, sha256(raw), len(raw), self.licence)
            slug = layer.lower()
            cols, rows = flatten_features(doc)
            changed = publish_table(items, f"{layer}:geojson", f"v1/environment/epa/{slug}.geojson.gz", gz(raw), "application/gzip", len(rows), f"EPA layer {layer}: {title} (GeoJSON, as served)", self.key,
                                    dict(layer=layer, family=family, title=title, source_url=url, source_sha256=sha256(raw), features=len(rows)))
            publish_table(items, f"{layer}:csv", f"v1/environment/epa/{slug}.csv.gz", gz(csv_bytes(cols, rows)), "application/gzip", len(rows), f"EPA layer {layer}: {title} (flat CSV)", self.key,
                          dict(layer=layer, family=family, title=title, source_url=url))
            tally.published += 1 if changed else 0
            tally.unchanged += 0 if changed else 1
        index = [[i.get("layer"), i.get("family"), i.get("title"), i.get("features"), i.get("path"), i.get("source_url"), i.get("changed_at")]
                 for k, i in sorted(items.items()) if k.endswith(":geojson")]
        publish_table(items, "_index", "v1/environment/epa/index.csv", csv_bytes(["layer", "family", "title", "features", "geojson_path", "source_url", "changed_at"], index), "text/csv", len(index),
                      "Index of the EPA layers the hub publishes", self.key)
        res = save_state(session, self.key, state, self.url, self.name)
        res.fetched, res.message = tally.published, tally.message(len(LAYERS))
        if len(tally.failed) == len(LAYERS):
            raise RuntimeError("no EPA layer could be fetched")
        return res


BW = "https://data.epa.ie/bw/api/v1"
BW_LOC_COLS = ["location_id", "beach_id", "beach_name", "county_name", "local_authority_name", "easting", "northing", "beach_type", "current_annual_water_quality_classification",
               "current_annual_classification_year", "year1_annual_water_quality_classification", "year2_annual_water_quality_classification", "year3_annual_water_quality_classification",
               "annual_water_quality_assessment", "next_monitoring_date", "has_all_season_bathing_restriction_in_place", "reason_for_all_season_bathing_restriction",
               "short_term_pollution_risk", "is_blue_flag", "is_green_coast", "has_lifeguard", "has_toilets", "beach_profile_url", "last_updated"]
BW_INC_COLS = ["incident_id", "bathing_water_incident_id", "beach_id", "beach_name", "county_name", "local_authority_name", "has_bathing_restriction_in_place", "incident_start_date",
               "incident_end_date", "incident_expected_duration", "bathing_restriction_type", "incident_description", "bathing_notice_pdf", "last_updated"]


class EpaBathingWater(Collector):
    key = "epa_bathing_water"
    name = "EPA bathing water: locations, classifications and incidents"
    publisher = "Environmental Protection Agency (EPA) with local authorities (beaches.ie)"
    url = "https://www.beaches.ie/"
    licence = "Creative Commons Attribution 4.0"
    provides = "Every designated bathing water (classification, facilities) and every pollution incident or bathing restriction announced since collection began."
    interval_hours = 3.0
    domain, tier = "environment", "files"

    def pages(self, client: httpx.Client, path: str) -> list[dict]:
        out, page = [], 1
        while True:
            r = get_with_retry(client, f"{BW}/{path}", params={"page": page}, timeout=120).json()
            lst = r.get("list", [])
            out += lst
            if not lst or len(out) >= int(r.get("count", 0)):
                return out
            page += 1

    def run(self, session: Session, client: httpx.Client) -> Result:
        locs = self.pages(client, "locations")
        alerts = self.pages(client, "alerts")
        if len(locs) < 100:
            raise ValueError(f"bathing water locations look wrong: {len(locs)}")
        log_fetch(session, self.key, f"{BW}/locations + {BW}/alerts", sha256(json.dumps([locs, alerts], sort_keys=True, default=str).encode()), len(locs) + len(alerts), self.licence)
        state = load_state(session, self.key)
        items = state.setdefault("items", {})
        history = state.setdefault("incidents", {})
        now = utcnow().isoformat(timespec="seconds")
        new = 0
        for a in alerts:
            k = str(a.get("incident_id"))
            if k not in history:
                new += 1
            history[k] = dict(a, first_seen=history.get(k, {}).get("first_seen", now), last_seen=now)
        publish_table(items, "locations", "v1/environment/epa_bathing_water_locations.csv.gz", gz(csv_bytes(BW_LOC_COLS, ([x.get(c) for c in BW_LOC_COLS] for x in sorted(locs, key=lambda x: x.get("location_id") or 0)))),
                      "application/gzip", len(locs), "Designated bathing waters: classification, restrictions, facilities", self.key)
        publish_table(items, "incidents", "v1/environment/epa_bathing_water_incidents.csv.gz",
                      gz(csv_bytes(BW_INC_COLS + ["first_seen", "last_seen"], ([x.get(c) for c in BW_INC_COLS] + [x["first_seen"], x["last_seen"]] for x in sorted(history.values(), key=lambda x: str(x.get("incident_start_date")), reverse=True)))),
                      "application/gzip", len(history), "Bathing water incidents and restrictions seen by the hub (history)", self.key)
        res = save_state(session, self.key, state, self.url, self.name)
        res.fetched, res.created, res.message = len(locs) + len(alerts), new, f"{len(locs)} bathing waters, {len(alerts)} current incidents ({new} new), {len(history)} in history"
        return res
