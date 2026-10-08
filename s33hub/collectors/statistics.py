"""Official statistics: CSO PxStat tables and Eurostat datasets, published as tidy gzipped CSV.

Both are collected once here so any Solas33 site can use a national statistic (prices, population, labour, housing, energy,
environment) without contacting the publisher. Each table is kept exactly as published (CSV, BOM removed) and re-published
only when its bytes change; `index.csv` in each family lists every table with its row count, size, hash and source link.
"""
from __future__ import annotations

import csv
import io
import re

import httpx
from sqlalchemy.orm import Session

from s33weather.models import sha256

from ..db import utcnow
from .base import Collector, Result, get_with_retry, log_fetch
from .tables import Tally, csv_bytes, decode, gz, load_state, publish_table, save_state

MAX_BYTES = 70_000_000

# CSO PxStat table code -> what it is. Every code was fetched and checked; tables over ~70 MB (e.g. HPM05, RIA02) are left out.
CSO_TABLES = {
    "CPM01": "Consumer Price Index by commodity group (monthly)", "CPM02": "Consumer Price Index by base reference period", "CPM03": "Consumer Price Index: selected sub-indices",
    "CPA01": "Consumer Price Index (annual)", "WPM28": "Wholesale price index by material", "HPM09": "Residential Property Price Index by property type (monthly)",
    "NDQ01": "New dwelling completions by type", "NDQ04": "New dwelling electricity connections", "BHQ01": "Dwellings: planning permissions by local authority",
    "BHQ02": "Dwellings: planning permissions by region and county", "BHQ03": "Planning permissions by type of construction", "BHQ05": "Planning permissions by type of dwelling",
    "HSQ06": "Households by area", "HSQ13": "Housing stock by local authority", "HAP11": "Share of RTB tenancies in HAP by area", "HAP14": "HAP rent change by local authority",
    "VAC21": "Vacant dwellings by BER group", "NDBER03": "Non-domestic Building Energy Ratings", "PEA01": "Population estimates by age and sex", "PEA03": "Migration estimates",
    "PEA11": "Population estimates by single year of age", "ROA21": "Population by age and sex (annual)", "MUM01": "Monthly unemployment by age and sex", "MUM02": "Monthly unemployment: lower and upper bounds",
    "QLF03": "Labour force by sector (quarterly)", "QNQ22": "GDP by region (quarterly)", "NAQ03": "National accounts (quarterly)", "NAQ05": "National accounts by sector (quarterly)",
    "NQQ03": "National accounts: expenditure (quarterly)", "FIQ02": "Finance: financial accounts (quarterly)", "SEI01": "Energy balance by fuel and flow (SEAI)", "SEI02": "Primary energy production (SEAI)", "SEI03": "Net energy imports (SEAI)",
    "SEI05": "Gross energy consumption (SEAI)", "FSS01": "Fossil fuel subsidies, direct and indirect (euro million)", "EIIEEA04": "Consumer Price Index for energy products",
    "WPM29": "Wholesale Price Index (excl. VAT) for energy products", "AEA01": "Agriculture: annual estimates", "AEA02": "Agriculture: annual estimates (2)",
    "AEA04": "Agriculture: annual estimates (4)", "TSM01": "Tourism and travel (monthly)", "TOA06": "Registered vehicles by type", "TOA11": "Transport: licensed operators",
    "TOA14": "Dublin Bus passenger numbers by month", "FPM01": "Fuel excise clearances", "MEC02": "Data centre metered electricity consumption", "MEC03": "Metered electricity by county and sector",
    "BEU01": "Business energy use (ktoe)", "BEU02": "Business energy use (million euro)", "EIIA08": "Greenhouse gas emissions by sector", "EIIA10": "Bathing water quality",
    "EIIA11": "Drinking water quality", "EIIA13": "River water quality", "EIIEEA31": "Municipal waste disposal routes", "SUS01": "SUSI grant outcomes",
}

EUROSTAT_API = "https://ec.europa.eu/eurostat/api/dissemination"
# Eurostat dataset -> (title, filter). No filter = the whole table via SDMX-CSV (small ones). A filter uses the statistics API (JSON-stat),
# which can restrict by dimension, for tables too large to publish whole.
EUROSTAT = {
    "nrg_ind_ren": ("Share of energy from renewable sources", None),
    "sdg_07_40": ("Energy dependency / renewable share indicators", None),
    "nrg_pc_204": ("Electricity prices for household consumers", None),
    "nrg_cb_pem": ("Electricity production by fuel", None),
    "prc_hicp_manr": ("HICP all-items annual inflation, Ireland and EU", {"geo": ["IE", "EU27_2020"], "coicop": ["CP00"], "unit": ["RCH_A"]}),
    "une_rt_m": ("Unemployment rate, seasonally adjusted, Ireland and EU", {"geo": ["IE", "EU27_2020"], "s_adj": ["SA"], "sex": ["T"], "age": ["TOTAL"], "unit": ["PC_ACT"]}),
    "namq_10_gdp": ("GDP growth, chain-linked volumes, Ireland and EU", {"geo": ["IE", "EU27_2020"], "s_adj": ["SCA"], "unit": ["CLV_PCH_PRE"], "na_item": ["B1GQ"]}),
    "env_air_gge": ("Greenhouse gas emissions by source", {"geo": ["IE", "EU27_2020"], "airpol": ["GHG"], "unit": ["MIO_T"]}),
}


def _rows(raw: bytes) -> int:
    return max(0, raw.count(b"\n") - 1)


class CsoPxStat(Collector):
    key = "cso_pxstat"
    name = "CSO PxStat tables: prices, housing, population, labour, energy, environment"
    publisher = "Central Statistics Office (CSO)"
    url = "https://data.cso.ie/"
    licence = "Creative Commons Attribution 4.0"
    provides = f"{len(CSO_TABLES)} CSO tables exactly as published (CSV), plus an index with row counts, sizes, hashes and source links."
    interval_hours = 24.0
    domain, tier = "statistics", "files"
    api = "https://ws.cso.ie/public/api.restful/PxStat.Data.Cube_API.ReadDataset/{code}/CSV/1.0/en"

    def run(self, session: Session, client: httpx.Client) -> Result:
        state = load_state(session, self.key)
        items = state.setdefault("items", {})
        tally = Tally()
        for code, title in CSO_TABLES.items():
            url = self.api.format(code=code)
            try:
                raw = get_with_retry(client, url, timeout=300).content
                if len(raw) > MAX_BYTES or b'"VALUE"' not in raw[:2000]:
                    raise ValueError("unexpected size or shape")
            except Exception as e:  # noqa: BLE001 - one table failing must not lose the rest
                tally.failed.append(f"{code} ({type(e).__name__})")
                continue
            log_fetch(session, self.key, url, sha256(raw), len(raw), self.licence)
            text = decode(raw)
            data = text.encode("utf-8")
            if publish_table(items, code, f"v1/statistics/cso/{code}.csv.gz", gz(data), "application/gzip", _rows(data), f"CSO table {code}: {title}", self.key,
                             dict(title=title, source_url=url, source_sha256=sha256(raw))):
                tally.published += 1
            else:
                tally.unchanged += 1
        index = csv_bytes(["table", "title", "rows", "file_bytes", "path", "source_url", "source_sha256", "changed_at"],
                          ([c, i.get("title"), i.get("rows"), i.get("bytes"), i.get("path"), i.get("source_url"), i.get("source_sha256"), i.get("changed_at")]
                           for c, i in sorted(items.items())))
        publish_table(items, "_index", "v1/statistics/cso/index.csv", index, "text/csv", len(items), "Index of the CSO tables the hub publishes", self.key)
        res = save_state(session, self.key, state, self.url, self.name)
        res.fetched, res.message = tally.published, tally.message(len(CSO_TABLES))
        if not items or len(tally.failed) == len(CSO_TABLES):
            raise RuntimeError("no CSO table could be fetched")
        return res


def jsonstat_rows(doc: dict) -> tuple[list[str], list[list]]:
    """Flatten a JSON-stat 2.0 cube into (columns, rows): one column per dimension (code), then value, status."""
    ids, sizes = doc["id"], doc["size"]
    dims = []
    for d in ids:
        cat = doc["dimension"][d]["category"]
        idx = cat["index"]
        order = sorted(idx, key=idx.get) if isinstance(idx, dict) else list(idx)
        dims.append(order)
    values = doc.get("value", {})
    status = doc.get("status", {})
    items = values.items() if isinstance(values, dict) else enumerate(values)
    out = []
    for flat, v in items:
        if v is None:
            continue
        flat = int(flat)
        pos, rem = [], flat
        for s in reversed(sizes):
            pos.append(rem % s)
            rem //= s
        pos.reverse()
        st = status.get(str(flat), "") if isinstance(status, dict) else (status[flat] if flat < len(status) else "")
        out.append([dims[i][p] for i, p in enumerate(pos)] + [v, st or ""])
    return list(ids) + ["value", "status"], out


class Eurostat(Collector):
    key = "eurostat"
    name = "Eurostat: energy, prices, inflation, unemployment, GDP, emissions"
    publisher = "Eurostat (European Commission)"
    url = "https://ec.europa.eu/eurostat/data/database"
    licence = "Eurostat reuse policy (CC BY 4.0 compatible; attribute Eurostat)"
    provides = f"{len(EUROSTAT)} Eurostat datasets: small ones whole, large ones restricted to Ireland and the EU aggregate; with an index."
    interval_hours = 24.0
    domain, tier = "statistics", "files"

    def fetch_one(self, client: httpx.Client, code: str, flt: dict | None) -> tuple[bytes, str, str]:
        """(csv bytes, url, how) for one dataset."""
        if flt is None:
            url = f"{EUROSTAT_API}/sdmx/2.1/data/{code}?format=SDMX-CSV"
            raw = get_with_retry(client, url, timeout=300).content
            if len(raw) > MAX_BYTES:
                raise ValueError("too large without a filter")
            return raw, url, "sdmx-csv"
        params = [("format", "JSON"), ("lang", "EN")] + [(k, v) for k, vs in flt.items() for v in vs]
        url = f"{EUROSTAT_API}/statistics/1.0/data/{code}"
        r = get_with_retry(client, url, params=params, timeout=300)
        cols, rows = jsonstat_rows(r.json())
        return csv_bytes(cols, rows), str(r.url), "json-stat"

    def run(self, session: Session, client: httpx.Client) -> Result:
        state = load_state(session, self.key)
        items = state.setdefault("items", {})
        tally = Tally()
        for code, (title, flt) in EUROSTAT.items():
            try:
                raw, url, how = self.fetch_one(client, code, flt)
            except Exception as e:  # noqa: BLE001
                tally.failed.append(f"{code} ({type(e).__name__})")
                continue
            log_fetch(session, self.key, url, sha256(raw), len(raw), self.licence, note=how)
            if publish_table(items, code, f"v1/statistics/eurostat/{code}.csv.gz", gz(raw), "application/gzip", _rows(raw), f"Eurostat {code}: {title}", self.key,
                             dict(title=title, source_url=url, how=how, source_sha256=sha256(raw))):
                tally.published += 1
            else:
                tally.unchanged += 1
        index = csv_bytes(["dataset", "title", "rows", "file_bytes", "path", "source_url", "method", "changed_at"],
                          ([c, i.get("title"), i.get("rows"), i.get("bytes"), i.get("path"), i.get("source_url"), i.get("how"), i.get("changed_at")] for c, i in sorted(items.items())))
        publish_table(items, "_index", "v1/statistics/eurostat/index.csv", index, "text/csv", len(items), "Index of the Eurostat datasets the hub publishes", self.key)
        res = save_state(session, self.key, state, self.url, self.name)
        res.fetched, res.message = tally.published, tally.message(len(EUROSTAT))
        if len(tally.failed) == len(EUROSTAT):
            raise RuntimeError("no Eurostat dataset could be fetched")
        return res
