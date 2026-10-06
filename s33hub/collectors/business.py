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

Derived (s33hub/risk.py, rules 1.0): per-company risk records in shards of 1,000 (v1/business/risk/company/<n>.json.gz), a name search index
(risk/search/), sector filing compliance, status events by month, the strike-off list and recent insolvencies. See docs/COMPANY_RISK.md.

Privacy: the register carries each company's registered office address, which for a small company can be someone's home. The hub
publishes the company record, not the address lines: only the Eircode routing key (first three characters) is kept. Directors and
officers are not in the open data and are not collected.
"""
from __future__ import annotations

import csv
import gzip
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
from .. import artifacts, risk
from ..config import PUBLIC_BASE
from ..names import NOT_A_COMPANY, norm_name
from .tables import Tally, csv_bytes, decode, gz, load_state, publish_table, save_state

CKAN = "https://data.gov.ie/api/3/action/package_show"
OUT = ["company_num", "name", "status", "status_code", "type", "type_code", "registered", "dissolved", "status_date", "last_annual_return", "last_accounts", "nace", "eircode_routing_key",
       "name_effective", "type_effective"]


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
                    (r.get("eircode") or "").replace(" ", "")[:3].upper(), (r.get("company_name_eff_date") or "").strip(), (r.get("company_type_eff_date") or "").strip()])
    return out


def load_public_contracts(client: httpx.Client) -> dict[str, dict]:
    """Public-contract wins per normalised winner name, from the hub's own published procurement files (eTenders and TED).
    Returns {} if those files are not on the hub yet: the risk records simply carry no contract information."""
    import re as _re
    out: dict[str, dict] = {}
    refs: set[str] = set()

    def add(names: list[str], year: str, value: str, tender: str, shared: bool):
        for raw in dict.fromkeys(names):
            k = norm_name(raw)
            if not k or NOT_A_COMPANY.search(raw):
                continue
            e = out.setdefault(k, {"w": 0, "v": 0.0, "fy": year, "ly": year, "_t": set()})
            if tender in e["_t"]:
                continue
            e["_t"].add(tender)
            e["w"] += 1
            if value and not shared:
                try:
                    e["v"] += float(value)
                except ValueError:
                    pass
            e["fy"], e["ly"] = min(e["fy"], year) if year else e["fy"], max(e["ly"], year) if year else e["ly"]

    try:
        r = client.get(f"{PUBLIC_BASE}/v1/tenders/etenders_notices.csv.gz", timeout=300)
        r.raise_for_status()
        rd = csv.DictReader(io.StringIO(gzip.decompress(r.content).decode("utf-8")))
        for row in rd:
            for c in ("ted_notice_link", "ted_can_link"):
                refs.update(_re.findall(r"(\d{5,8}-\d{4})", row.get(c) or ""))
            if row["suppliers"]:
                names = [x.strip() for x in row["suppliers"].split(";") if x.strip()]
                add(names, (row["award_published"] or row["published"])[:4], row["awarded_value_eur"], "E" + row["tender_id"], len(names) > 1)
        for year in range(2024, utcnow().year + 1):
            rt = client.get(f"{PUBLIC_BASE}/v1/tenders/ted_notices/{year}.csv", timeout=120)
            if rt.status_code != 200:
                continue
            for row in csv.DictReader(io.StringIO(rt.text)):
                if row["notice_type"].startswith("can") and row["winners"] and row["publication_number"] not in refs:
                    names = [x.strip() for x in row["winners"].split(";") if x.strip()]
                    add(names, row["publication_date"][:4], row["award_value_eur"], "T" + row["publication_number"], len(names) > 1)
    except Exception:  # noqa: BLE001 - contracts are an enrichment; the register is the product
        return {}
    for e in out.values():
        e.pop("_t", None)
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
        # the rows are parsed on every run: risk flags depend on today's date, and the publish ledger (not this state) decides what is uploaded
        companies_changed = True
        rows: list[list[str]] = []
        filings_by: dict[str, list[tuple[str, str]]] = {}
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
                for fr in rws:
                    if fr[0] and fr[2] and fr[4]:
                        filings_by.setdefault(fr[0], []).append((fr[2], fr[4]))
        except Exception as e:  # noqa: BLE001 - the register is the main product; filings failing is reported, not fatal
            tally.failed.append(f"financial_statements ({type(e).__name__})")
        risk_msg = ""
        if companies_changed and rows:
            # match winners to live companies (exactly one live company with the name, else no link) and build the risk datasets
            names: dict[str, list[str]] = {}
            for r in rows:
                if risk.status_group(r[2]) == "L":
                    names.setdefault(norm_name(r[1]), []).append(r[0])
            wins = load_public_contracts(client)
            contracts = {nums[0]: dict(w) for k, w in wins.items() if (nums := names.get(k)) and len(nums) == 1}
            records = risk.build_records(rows, filings_by, contracts, utcnow().date())
            for k in [k for k in items if "/risk/" in k]:       # a cut-off run can mark files published that never uploaded; the ledger dedups
                del items[k]
            files = {**risk.company_shards(records), **risk.search_shards(records)}
            for path, data in files.items():
                publish_table(items, path, path, data, "application/json" if path.endswith("index.json") else "application/gzip", None,
                              "Company risk records / search index (rules " + risk.RULES_VERSION + ")", self.key)
            for path, (data, ctype) in risk.aggregates(records, utcnow().date()).items():
                publish_table(items, path, path, data, ctype, None, "Company risk market tables", self.key)
            level = Counter(r["lv"] for r in records.values())
            risk_msg = f"; risk: {len(records):,} records ({level['red']:,} red, {level['amber']:,} amber), {len(contracts):,} with public contracts, {len(files)} files"
        res = save_state(session, self.key, state, self.url, self.name)
        src = items.get("_source", {})
        res.fetched, res.message = src.get("companies", 0), f"{src.get('companies', 0):,} companies (newest registered {src.get('newest_registration')}); " + \
            ("register unchanged; " if not companies_changed else "") + tally.message(tally.published + tally.unchanged + len(tally.failed)) + f"; {filed:,} filings" + risk_msg
        return res
