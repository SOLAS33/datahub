"""Where public data lives: an index of the national open-data catalogue, and mirrors of selected datasets from it.

* `datagovie_catalogue` - one row per dataset on data.gov.ie (about 22,000): title, publisher, licence, update frequency, tags,
  formats and the resource links. It lets any Solas33 site (or a person) find the next dataset worth collecting.
* `ckan_open_datasets` - selected datasets that publish plain CSV/GeoJSON files (Revenue statistics, TII traffic counters, Dublin cycle
  counts and similar), mirrored as published, with an index. Resources that turn out to be web pages or too large are skipped and said so.
"""
from __future__ import annotations

import re

import httpx
from sqlalchemy.orm import Session

from s33weather.models import sha256

from .base import Collector, Result, get_with_retry, log_fetch
from .tables import Tally, csv_bytes, gz, load_state, publish_table, save_state

CKAN = "https://data.gov.ie/api/3/action"
MAX_BYTES = 40_000_000

# package name on data.gov.ie -> why we mirror it. Every package was checked for a directly downloadable CSV/GeoJSON resource.
PACKAGES = {
    "electricity-tax": "Electricity tax receipts (Revenue)", "alcohol-products-tax": "Alcohol products tax (Revenue)", "sugar-sweetened-drinks-tax": "Sugar sweetened drinks tax (Revenue)",
    "excise-volumes-by-commodity": "Excise volumes by commodity (Revenue)", "income-earners-tax-paid": "Income tax paid by earners (Revenue)",
    "foreign-earnings-deduction-statistics": "Foreign earnings deduction statistics (Revenue)", "registrations-assessments-and-transactions": "Revenue registrations, assessments and transactions",
    "traffic-counter-locations": "TII traffic counter locations", "dublin-city-centre-cycle-counts": "Dublin City Council cycle counts", "bicycle-traffic-counts-dlr": "Dún Laoghaire-Rathdown bicycle counts",
    "pedestrian-footfall-dlr": "Dún Laoghaire-Rathdown pedestrian footfall", "dublin-economic-monitor": "Dublin Economic Monitor",
}
FORMATS = {"CSV": "csv", "GEOJSON": "geojson", "JSON": "json"}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:48] or "file"


class DataGovIeCatalogue(Collector):
    key = "datagovie_catalogue"
    name = "data.gov.ie catalogue index"
    publisher = "Open Data Unit, Dept. of Public Expenditure (data.gov.ie)"
    url = "https://data.gov.ie/"
    licence = "Metadata: Creative Commons Attribution 4.0 (each dataset carries its own licence, recorded in the index)"
    provides = "One row per dataset on data.gov.ie: title, publisher, licence, update frequency, tags, formats and resource links."
    interval_hours = 24.0
    domain, tier = "catalogue", "files"
    page = 1000

    def run(self, session: Session, client: httpx.Client) -> Result:
        rows, start, total = [], 0, None
        while total is None or start < total:
            r = get_with_retry(client, f"{CKAN}/package_search", params={"rows": self.page, "start": start, "sort": "name asc"}, timeout=120)
            res = r.json()["result"]
            total = res["count"]
            if not res["results"]:
                break
            for p in res["results"]:
                resources = p.get("resources", [])
                rows.append([p.get("name"), (p.get("title") or "").replace("\n", " ")[:300], (p.get("organization") or {}).get("title", ""), p.get("license_title") or "", p.get("frequency") or "",
                             (p.get("metadata_modified") or "")[:10], p.get("num_resources", len(resources)), "|".join(sorted({(x.get("format") or "").upper() for x in resources if x.get("format")})),
                             "|".join(t["name"] for t in p.get("tags", [])[:8]), " | ".join((x.get("url") or "") for x in resources[:3]), f"https://data.gov.ie/dataset/{p.get('name')}"])
            start += self.page
        log_fetch(session, self.key, f"{CKAN}/package_search", sha256(repr(rows).encode()), len(rows), self.licence, note=f"{len(rows)} datasets")
        if len(rows) < 1000:
            raise ValueError(f"catalogue looks wrong: {len(rows)} datasets")
        data = csv_bytes(["name", "title", "publisher", "licence", "update_frequency", "metadata_modified", "resources", "formats", "tags", "first_resource_urls", "page"], rows)
        state = load_state(session, self.key)
        items = state.setdefault("items", {})
        publish_table(items, "catalogue", "v1/catalogue/datagovie_packages.csv.gz", gz(data), "application/gzip", len(rows), "Index of every dataset on data.gov.ie", self.key)
        res = save_state(session, self.key, state, self.url, self.name)
        res.fetched, res.message = len(rows), f"{len(rows):,} datasets indexed"
        return res


class CkanOpenDatasets(Collector):
    key = "ckan_open_datasets"
    name = "Selected open datasets mirrored from data.gov.ie (Revenue, TII, Dublin counters)"
    publisher = "Various public bodies via data.gov.ie"
    url = "https://data.gov.ie/"
    licence = "Creative Commons Attribution 4.0 (per dataset; recorded in the index)"
    provides = "CSV/GeoJSON resources of chosen datasets exactly as published (gzipped), with an index of source URL, licence and hash."
    interval_hours = 24.0
    domain, tier = "catalogue", "files"

    def run(self, session: Session, client: httpx.Client) -> Result:
        state = load_state(session, self.key)
        items = state.setdefault("items", {})
        tally, index = Tally(), []
        for pkg, why in PACKAGES.items():
            try:
                p = get_with_retry(client, f"{CKAN}/package_show", params={"id": pkg}).json()["result"]
            except Exception as e:  # noqa: BLE001
                tally.failed.append(f"{pkg} ({type(e).__name__})")
                continue
            for r in p.get("resources", []):
                fmt = FORMATS.get((r.get("format") or "").upper())
                url = r.get("url") or ""
                if not fmt or not url.startswith("http"):
                    continue
                item = f"{pkg}/{slug(r.get('name'))}-{(r.get('id') or '')[:6]}"
                try:
                    resp = get_with_retry(client, url, timeout=180)
                    raw = resp.content
                    if len(raw) > MAX_BYTES or "html" in resp.headers.get("content-type", "").lower() or raw[:200].lstrip().lower().startswith((b"<!doctype", b"<html")):
                        raise ValueError("not a data file or too large")
                except Exception as e:  # noqa: BLE001
                    tally.failed.append(f"{item} ({type(e).__name__})")
                    continue
                log_fetch(session, self.key, url, sha256(raw), len(raw), p.get("license_title"))
                path = f"v1/open/{pkg}/{slug(r.get('name'))}-{(r.get('id') or '')[:6]}.{fmt}.gz"
                if publish_table(items, item, path, gz(raw), "application/gzip", raw.count(b"\n"), f"{why}: {r.get('name') or fmt}", self.key,
                                 dict(package=pkg, title=p.get("title"), publisher=(p.get("organization") or {}).get("title"), licence=p.get("license_title"), source_url=url,
                                      source_sha256=sha256(raw), format=fmt)):
                    tally.published += 1
                else:
                    tally.unchanged += 1
        for k, i in sorted(items.items()):
            if k != "_index":
                index.append([i.get("package"), i.get("title"), i.get("publisher"), i.get("licence"), i.get("format"), i.get("path"), i.get("source_url"), i.get("source_sha256"), i.get("changed_at")])
        publish_table(items, "_index", "v1/open/index.csv", csv_bytes(["package", "title", "publisher", "licence", "format", "path", "source_url", "source_sha256", "changed_at"], index),
                      "text/csv", len(index), "Index of the mirrored open datasets", self.key)
        res = save_state(session, self.key, state, self.url, self.name)
        res.fetched, res.message = tally.published, tally.message(len(index) + len(tally.failed))
        if not index:
            raise RuntimeError("no dataset could be mirrored")
        return res
