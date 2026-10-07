"""Uisce Éireann open data: the public water supply zones and the water and wastewater asset lists, plus the published business tariffs.

Source: https://water.ie/open-data (datasets are Excel files; most are published under Creative Commons Attribution 4.0).
Required attribution: Copyright Uisce Éireann; source water.ie; licence CC BY 4.0; the disclaimer; and a note where the data was modified.
The hub converts each workbook to CSV unchanged apart from header names (snake_case) and the removal of empty rows. Nothing personal is in these files.

* `ue_water_assets`: supply zones (with population), district metered areas, water treatment plants, water and wastewater pumping stations,
  water storage (raw-water reservoirs and network storage tanks: names and local authority only, never levels), wastewater treatment plants
  (with capacity in population equivalent) and wastewater agglomerations.
* `ue_tariffs`: the business charges tables read from https://www.water.ie/business/billing/charges/ . The parser checks that every combined
  figure equals water plus wastewater, and fails (keeping the last published file) if the page layout changes.
"""
from __future__ import annotations

import io
import json
import re
from datetime import date

import httpx
from openpyxl import load_workbook
from sqlalchemy.orm import Session

from s33weather.models import sha256

from ..db import utcnow
from .base import Collector, Result, get_with_retry, log_fetch
from .tables import Tally, csv_bytes, gz, load_state, publish_table, save_state

WIDEN = "https://water.widen.net/content/{}/original/{}"
# name -> (url, minimum rows it must have)
DATASETS = {
    "supply_zones": (WIDEN.format("use17tawmb", "Q1-2026-Public-Water-Supply-Zone-Information.xlsx"), 400),
    "district_metered_areas": (WIDEN.format("d7n8siyenc", "DMA-2023Q3-Copy"), 2000),
    "water_treatment_plants": (WIDEN.format("qu0xi1uwe6", "WTP-2023Q3-Copy"), 300),
    "water_pumping_stations": (WIDEN.format("6d9ygznuly", "WPS-2023Q3"), 800),
    "water_storage": (WIDEN.format("fkxmbpnexh", "WNS-2023Q3"), 600),
    "wastewater_treatment_plants": (WIDEN.format("zpvs4n6yad", "WWTP-2023Q3"), 500),
    "wastewater_pumping_stations": (WIDEN.format("vwj0xxnrzf", "WWPS-2023Q3"), 1000),
    "wastewater_agglomerations": (WIDEN.format("zblek1wksi", "Agglomeration-2023Q3"), 500),
}
RENAME = {"la": "local_authority", "localauthority": "local_authority", "l5_number": "asset_id", "l5_asset_type": "asset_code", "dbo_operator": "dbo_operator",
          "wastewater_treatment_plant_name": "name", "identifier": "agglomeration_id", "dma_name": "dma_name", "eden_wsz_id": "eden_code",
          "waterresourcezone_id": "water_resource_zone", "capacity_pe": "capacity_pe", "water_services_area": "water_services_area",
          "water_supply_zone_name": "name", "eden_water_supply_zone_code": "eden_code", "wsz_id": "wsz_id", "water_resource_zone_code": "water_resource_zone", "owned_by": "owned_by"}


def snake(h: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(h or "").strip().lower()).strip("_")
    return "population" if s.startswith("population") else RENAME.get(s, s)


def read_workbook(raw: bytes) -> tuple[list[str], list[list[str]], str]:
    """First worksheet as (header, rows, as-of text taken from a header such as 'Population ... as of 27.03.2026')."""
    ws = load_workbook(io.BytesIO(raw), read_only=True, data_only=True).worksheets[0]
    it = ws.iter_rows(values_only=True)
    head_raw = [h for h in next(it)]
    asof = ""
    for h in head_raw:
        m = re.search(r"as of (\d{1,2}\.\d{1,2}\.\d{4})", str(h or ""))
        if m:
            d, mo, y = m.group(1).split(".")
            asof = f"{y}-{int(mo):02d}-{int(d):02d}"
    head = [snake(h) for h in head_raw]
    rows = []
    for r in it:
        if not any(c not in (None, "") for c in r):
            continue
        rows.append([("" if c is None else (str(int(c)) if isinstance(c, float) and c == int(c) else str(c).strip())) for c in r][: len(head)])
    return head, rows, asof


class UisceEireannAssets(Collector):
    key = "ue_water_assets"
    name = "Uisce Éireann water supply zones and asset lists"
    publisher = "Uisce Éireann"
    url = "https://water.ie/open-data"
    licence = "Creative Commons Attribution 4.0 (Copyright Uisce Éireann; source water.ie; see the attribution statements on the open data page)"
    provides = ("Public water supply zones with population, district metered areas, water treatment plants, pumping stations, water storage (names only), "
                "wastewater treatment plants with capacity, and wastewater agglomerations. Converted from Excel to CSV.")
    used_by = ("waterwatch",)
    interval_hours = 168.0
    domain, tier = "environment", "files"

    def run(self, session: Session, client: httpx.Client) -> Result:
        state = load_state(session, self.key)
        items = state.setdefault("items", {})
        tally = Tally()
        for ds, (url, minimum) in DATASETS.items():
            try:
                r = get_with_retry(client, url, timeout=180)
                log_fetch(session, self.key, url, sha256(r.content), len(r.content), self.licence, note=ds)
                head, rows, asof = read_workbook(r.content)
                if len(rows) < minimum:
                    raise ValueError(f"{ds}: only {len(rows)} rows (expected at least {minimum})")
                if ds == "supply_zones":
                    head.append("population_as_of"); rows = [r + [asof] for r in rows]
                if publish_table(items, ds, f"v1/environment/ue/{ds}.csv.gz", gz(csv_bytes(head, rows)), "application/gzip", len(rows), f"Uisce Éireann {ds.replace('_', ' ')} (CC BY 4.0)", self.key):
                    tally.published += 1
                else:
                    tally.unchanged += 1
            except Exception as e:  # noqa: BLE001 - one workbook failing must not stop the others
                tally.failed.append(f"{ds} ({type(e).__name__}: {str(e)[:80]})")
        if tally.published + tally.unchanged == 0:
            raise RuntimeError("no Uisce Éireann dataset could be read: " + "; ".join(tally.failed))
        res = save_state(session, self.key, state, self.url, self.name)
        res.fetched, res.message = tally.published + tally.unchanged, tally.message(len(DATASETS))
        return res


# ---- business tariffs

CHARGES_URL = "https://www.water.ie/business/billing/charges/"
MONEY = r"€?\s*[\d,]+\.\d{2}"


def _num(s: str) -> float:
    return float(re.sub(r"[^\d.]", "", s))


def _iso(d: str) -> str:
    dd, mm, yy = d.split("/")
    return f"{yy}-{int(mm):02d}-{int(dd):02d}"


def parse_tariffs(html_text: str) -> list[dict]:
    """Metered and unmetered business tariffs per charging period, from the page text. Raises if the tables do not add up."""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html_text, flags=re.S)
    import html as _h
    text = re.sub(r"\s+", " ", _h.unescape(re.sub(r"<[^>]+>", " ", text)))
    heads = list(re.finditer(r"effective\s+(\d{1,2}/\d{1,2}/\d{4})\s*\W+\s*(\d{1,2}/\d{1,2}/\d{4})", text))
    periods: dict[tuple[str, str], dict] = {}
    for i, h in enumerate(heads):
        seg = text[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)]
        p = periods.setdefault((_iso(h.group(1)), _iso(h.group(2))), {"from": _iso(h.group(1)), "to": _iso(h.group(2)), "metered": [], "unmetered": []})
        if not p["metered"]:
            for m in re.finditer(r"Band\s+(\d)\s+Class\s*\(([^)]*)\)\s*((?:(?:" + MONEY + r"|-)\s*){2,6})", seg):
                nums = re.findall(MONEY, m.group(3))
                vals = [_num(n) for n in nums]
                band = int(m.group(1))
                if len(vals) == 6:
                    ws_, wv, ww_, wwv, cs, cv = vals
                    p["metered"].append(dict(band=band, class_label=m.group(2).strip(), water_standing=ws_, water_volumetric=wv, wastewater_standing=ww_, wastewater_volumetric=wwv,
                                             combined_standing=cs, combined_volumetric=cv))
                elif len(vals) == 2 and band == 5:
                    p["metered"].append(dict(band=5, class_label=m.group(2).strip(), water_standing=vals[0], water_volumetric=vals[1], wastewater_standing=None, wastewater_volumetric=None,
                                             combined_standing=None, combined_volumetric=None))
        if not p["unmetered"]:
            um = re.search(r"Unmetered Tariffs.*", seg)
            if um:
                for m in re.finditer(r"Band\s+(\d)\*?\s+((?:" + MONEY + r"\s*){3})", um.group(0)):
                    w, ww, c = [_num(n) for n in re.findall(MONEY, m.group(2))]
                    p["unmetered"].append(dict(band=int(m.group(1)), water=w, wastewater=ww, combined=c))
    out = sorted((p for p in periods.values() if len(p["metered"]) >= 5), key=lambda p: p["from"])
    if len(out) < 2:
        raise ValueError(f"expected at least two charging periods with five metered bands, found {len(out)}")
    for p in out:
        if sorted(b["band"] for b in p["metered"]) != [1, 2, 3, 4, 5]:
            raise ValueError(f"period {p['from']}: metered bands are {[b['band'] for b in p['metered']]}")
        for b in p["metered"]:
            if b["band"] < 5 and (abs(b["water_standing"] + b["wastewater_standing"] - b["combined_standing"]) > 0.011
                                  or abs(b["water_volumetric"] + b["wastewater_volumetric"] - b["combined_volumetric"]) > 0.0051):
                raise ValueError(f"period {p['from']} band {b['band']}: combined does not equal water plus wastewater")
        for u in p["unmetered"]:
            if abs(u["water"] + u["wastewater"] - u["combined"]) > 0.011:
                raise ValueError(f"period {p['from']} unmetered band {u['band']}: combined does not equal water plus wastewater")
    return out


class UisceEireannTariffs(Collector):
    key = "ue_tariffs"
    name = "Uisce Éireann business water and wastewater charges"
    publisher = "Uisce Éireann"
    url = CHARGES_URL
    licence = "Published tariff figures from the Uisce Éireann business charges page; check the page for the authority. Attribution: Uisce Éireann, water.ie"
    provides = "Metered (bands 1 to 5) and unmetered (bands 1 to 2) non-domestic tariffs for each published charging period, checked so combined = water + wastewater."
    used_by = ("waterwatch",)
    interval_hours = 24.0
    rights = "check"
    domain, tier = "environment", "files"

    def run(self, session: Session, client: httpx.Client) -> Result:
        state = load_state(session, self.key)
        items = state.setdefault("items", {})
        r = get_with_retry(client, CHARGES_URL, timeout=120)
        digest = sha256(r.content)
        log_fetch(session, self.key, CHARGES_URL, digest, len(r.content), self.licence)
        periods = parse_tariffs(r.content.decode("utf-8", "replace"))
        doc = {"source_url": CHARGES_URL, "publisher": "Uisce Éireann", "page_sha256": digest, "retrieved_on": utcnow().date().isoformat(), "periods": periods,
               "note": "Transcribed by the hub from the charges page and checked for internal consistency. The page is the authority."}
        body = json.dumps(doc, indent=1, sort_keys=True).encode()
        publish_table(items, "tariffs", "v1/environment/ue/business_tariffs.json", body, "application/json", len(periods), "Uisce Éireann business tariffs by charging period", self.key)
        res = save_state(session, self.key, state, self.url, self.name)
        res.fetched, res.message = len(periods), f"{len(periods)} charging periods ({periods[0]['from']} to {periods[-1]['to']})"
        return res
