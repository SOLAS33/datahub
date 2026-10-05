"""Companies Registration Office (CRO) open data: every company on the Irish register, and the accounts filed.

Source: https://opendata.cro.ie/ (listed on data.gov.ie as "Company Records", updated daily, and "Financial Statements"). Creative Commons
Attribution 4.0 per the data.gov.ie record.

What is published (all under v1/business/cro/):
* companies/<year of registration>.csv.gz - one row per company ever registered: number, name, status, type, registration, dissolution and
  status dates, last annual return and accounts dates, NACE code, Eircode routing key. About 825,000 companies in all.
* registrations_monthly.csv.gz - new companies per month by company type group and NACE division.
* recent_registrations.csv.gz - companies registered in the last 120 days.
* status_counts.csv - companies now by status and type.
* financial_statements/<year>.csv.gz - which companies filed accounts, and when (document file names dropped).

Privacy: the register carries each company's registered office address, which for a small company can be someone's home. The hub
publishes the company record, not the address lines: only the Eircode routing key (first three characters) is kept. Directors and
officers are not in the open data and are not collected.
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from collections import Counter, defaultdict
from datetime import timedelta

import httpx
from sqlalchemy.orm import Session

from s33weather.models import sha256

from ..db import utcnow
from .base import Collector, Result, get_with_retry, log_fetch
from .tables import Tally, csv_bytes, decode, gz, load_state, publish_table, save_state

CKAN = "https://data.gov.ie/api/3/action/package_show"
OUT = ["company_num", "name", "status", "status_code", "type", "type_code", "registered", "dissolved", "status_date", "last_annual_return", "last_accounts", "nace", "eircode_routing_key"]


def type_group(t: str) -> str:
    t = (t or "").lower()
    if "guarantee" in t or t.startswith("clg"):
        return "company limited by guarantee"
    if "designated activity" in t or t.startswith("dac"):
        return "designated activity company"
    if "unlimited" in t or t.startswith("ulc"):
        return "unlimited company"
    if "public" in t or "plc" in t:
        return "public limited company"
    if "private" in t or t.startswith("ltd") or "limited by shares" in t:
        return "private limited company"
    return "other"


def normalise_cro(text: str) -> list[list[str]]:
    """Company rows in one stable shape. Address lines are dropped; the Eircode routing key is kept."""
    rd = csv.DictReader(io.StringIO(text, newline=""))
    need = {"company_num", "company_name", "company_status", "company_type", "company_reg_date"}
    if not need <= set(rd.fieldnames or []):
        raise ValueError(f"unexpected CRO header: {rd.fieldnames}")
    out = []
    for r in rd:
        nace = (r.get("nace_v2_code") or "").strip()
        nace = nace[:-2] if nace.endswith(".0") else nace
        out.append([(r["company_num"] or "").strip(), (r["company_name"] or "").strip(), (r["company_status"] or "").strip(), (r.get("company_status_code") or "").strip(),
                    (r["company_type"] or "").strip(), (r.get("company_type_code") or "").strip(), (r["company_reg_date"] or "").strip(), (r.get("comp_dissolved_date") or "").strip(),
                    (r.get("company_status_date") or "").strip(), (r.get("last_ar_date") or "").strip(), (r.get("last_accounts_date") or "").strip(), nace,
                    (r.get("eircode") or "").replace(" ", "")[:3].upper()])
    return out


class CroCompanies(Collector):
    key = "cro_companies"
    name = "CRO company register and filed accounts"
    publisher = "Companies Registration Office (CRO)"
    url = "https://opendata.cro.ie/"
    licence = "Creative Commons Attribution 4.0"
    provides = ("Every company on the Irish register (status, type, registration and dissolution dates, NACE code, Eircode routing key; no street addresses), "
                "monthly new-company counts, recent registrations, and which companies filed accounts.")
    interval_hours = 24.0
    domain, tier = "business", "files"

    def locate(self, client: httpx.Client, package: str) -> list[dict]:
        r = get_with_retry(client, CKAN, params={"id": package}).json()["result"]
        return [x for x in r["resources"] if (x.get("format") or "").upper() == "CSV" and x.get("url")]

    def run(self, session: Session, client: httpx.Client) -> Result:
        state = load_state(session, self.key)
        items = state.setdefault("items", {})
        tally = Tally()
        res_c = self.locate(client, "companies")
        url = res_c[0]["url"]
        raw = get_with_retry(client, url, timeout=900).content
        digest = sha256(raw)
        log_fetch(session, self.key, url, digest, len(raw), self.licence)
        companies_changed = items.get("_source", {}).get("sha256") != digest
        if companies_changed:
            z = zipfile.ZipFile(io.BytesIO(raw)) if raw[:2] == b"PK" else None
            text = decode(z.read(z.namelist()[0]) if z else raw)
            rows = normalise_cro(text)
            if len(rows) < 500_000:
                raise ValueError(f"CRO register looks wrong: {len(rows)} companies")
            by_year: dict[str, list] = defaultdict(list)
            monthly: Counter = Counter()
            status: Counter = Counter()
            cutoff = (utcnow() - timedelta(days=120)).date().isoformat()
            recent = []
            for r in rows:
                y = r[6][:4] if len(r[6]) >= 4 else "unknown"
                by_year[y].append(r)
                if r[6]:
                    monthly[(r[6][:7], type_group(r[4]), r[11][:2] if r[11] else "")] += 1
                    if r[6] >= cutoff:
                        recent.append(r)
                status[(r[2], type_group(r[4]))] += 1
            for y, rs in sorted(by_year.items()):
                if publish_table(items, f"companies:{y}", f"v1/business/cro/companies/{y}.csv.gz", gz(csv_bytes(OUT, sorted(rs, key=lambda x: int(x[0]) if x[0].isdigit() else 0))),
                                 "application/gzip", len(rs), f"CRO companies registered in {y}", self.key):
                    tally.published += 1
                else:
                    tally.unchanged += 1
            publish_table(items, "_monthly", "v1/business/cro/registrations_monthly.csv.gz",
                          gz(csv_bytes(["month", "company_type_group", "nace_division", "registrations"], ([m, t, n, c] for (m, t, n), c in sorted(monthly.items())))), "application/gzip", len(monthly),
                          "New companies per month by type group and NACE division", self.key)
            publish_table(items, "_recent", "v1/business/cro/recent_registrations.csv.gz", gz(csv_bytes(OUT, sorted(recent, key=lambda x: x[6], reverse=True))), "application/gzip", len(recent),
                          "Companies registered in the last 120 days", self.key)
            publish_table(items, "_status", "v1/business/cro/status_counts.csv", csv_bytes(["status", "company_type_group", "companies"], ([s, t, c] for (s, t), c in sorted(status.items()))),
                          "text/csv", len(status), "Companies on the register now, by status and type group", self.key)
            items["_source"] = dict(sha256=digest, companies=len(rows), newest_registration=max((r[6] for r in rows), default=""), url=url)
        # accounts filings, one file per year
        filed = 0
        try:
            for res in self.locate(client, "financial-statements"):
                name = res.get("name") or ""
                m = next((t for t in name.replace(".", " ").replace("_", " ").split() if t.isdigit() and len(t) == 4), None)
                rr = get_with_retry(client, res["url"], timeout=600)
                if len(rr.content) > 80_000_000:
                    continue
                log_fetch(session, self.key, res["url"], sha256(rr.content), len(rr.content), self.licence)
                rd = csv.DictReader(io.StringIO(decode(rr.content), newline=""))
                rws = [[(r.get("company_num") or "").strip(), (r.get("company_name") or "").strip(), (r.get("submission_rec_date") or "").strip(), (r.get("submission_reg_date") or "").strip(),
                        (r.get("submissions_accounts_to_date") or "").strip()] for r in rd]
                year = m or (rws[0][2][:4] if rws and rws[0][2] else "unknown")
                if publish_table(items, f"fs:{year}:{res.get('id', '')[:6]}", f"v1/business/cro/financial_statements/{year}-{(res.get('id') or '')[:6]}.csv.gz",
                                 gz(csv_bytes(["company_num", "name", "received", "registered", "accounts_to_date"], rws)), "application/gzip", len(rws),
                                 f"CRO accounts filed (resource {name})", self.key):
                    tally.published += 1
                filed += len(rws)
        except Exception as e:  # noqa: BLE001 - the register is the main product; filings failing is reported, not fatal
            tally.failed.append(f"financial_statements ({type(e).__name__})")
        res = save_state(session, self.key, state, self.url, self.name)
        src = items.get("_source", {})
        res.fetched, res.message = src.get("companies", 0), f"{src.get('companies', 0):,} companies (newest registered {src.get('newest_registration')}); " + \
            ("register unchanged; " if not companies_changed else "") + tally.message(tally.published + tally.unchanged + len(tally.failed)) + f"; {filed:,} filings"
        return res
