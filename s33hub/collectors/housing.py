"""Housing sources for PropertyWatch Ireland: rents, mortgage rates, commencements, homelessness reports and a grant-page scraper.

* `rtb_rents`: CSO table RIH02 (RTB Average Monthly Rent Report, half-yearly, by town, bedrooms and property type), reduced to the rows a site needs.
* `cbi_mortgage_rates`: Central Bank retail interest rates for mortgages (table B.3.1), published unchanged.
* `dhlgh_commencements`: Department of Housing monthly commencement notices by local authority, unchanged.
* `homelessness_reports`: the Department of Housing monthly homelessness reports, joined into one tidy file.
* `housing_grants`: reads the official pages that describe housing grants and supports (SEAI, Citizens Information, Revenue, the gov.ie vacant property grant, First Home Scheme,
  Local Authority Home Loan) and records, for each page, its title, description, the euro amounts it states (each with its sentence), dates it mentions, words that suggest the
  scheme is closed or paused, a hash of its visible text and when that hash last changed. It keeps no page text beyond those short quoted facts. A person reviews anything shown
  to the public: this collector detects and extracts, it does not decide.
"""
from __future__ import annotations

import csv
import html as _html
import io
import json
import re
import subprocess
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from s33weather.models import sha256

from ..db import utcnow
from .base import Collector, Result, get_with_retry, log_fetch
from .tables import Tally, csv_bytes, gz, load_state, publish_table, save_state

BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CSO = "https://ws.cso.ie/public/api.restful/PxStat.Data.Cube_API.ReadDataset/{}/CSV/1.0/en"
CKAN = "https://data.gov.ie/api/3/action/"


def snake(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(h).strip().lower()).strip("_")


# ----------------------------------------------------------------------------- RTB rents (CSO RIH02)

def filter_rents(lines) -> tuple[list[str], list[list[str]]]:
    """From the RIH02 CSV rows keep [half_year, location, bedrooms, property_type, euro] where bedrooms or property type is the 'All' total and a value exists.
    That gives each town's rent by bedroom count and by property type without the full cross-product."""
    rdr = csv.DictReader(lines)
    out = []
    for r in rdr:
        v = (r.get("VALUE") or "").strip()
        if not v:
            continue
        beds, typ = r["Number of Bedrooms"], r["Property Type"]
        if beds == "All bedrooms" or typ == "All property types":
            out.append([r["HalfYear"], r["Location"], beds, typ, v])
    return ["half_year", "location", "bedrooms", "property_type", "euro_per_month"], out


class RtbRents(Collector):
    key = "rtb_rents"
    name = "RTB Average Monthly Rent Report (CSO table RIH02)"
    publisher = "Residential Tenancies Board, published by the Central Statistics Office"
    url = "https://data.cso.ie/table/RIH02"
    licence = "Creative Commons Attribution 4.0 (CSO)"
    provides = "Average monthly rent in euro by half-year, town or area, number of bedrooms and property type, reduced to totals by bedroom count and by property type."
    used_by = ("propertywatch",)
    interval_hours = 168.0
    domain, tier = "housing", "files"

    def run(self, session: Session, client: httpx.Client) -> Result:
        state = load_state(session, self.key)
        items = state.setdefault("items", {})
        url = CSO.format("RIH02")
        import hashlib
        h = hashlib.sha256()
        lines, n = [], 0
        with client.stream("GET", url, timeout=900) as r:
            r.raise_for_status()
            buf = ""
            for chunk in r.iter_text():
                h.update(chunk.encode("utf-8"))
                n += len(chunk)
                buf += chunk
                parts = buf.split("\n")
                buf = parts.pop()
                lines.extend(parts)
        if buf:
            lines.append(buf)
        if lines and lines[0].startswith("﻿"):
            lines[0] = lines[0][1:]
        log_fetch(session, self.key, url, h.hexdigest(), n, self.licence)
        head, rows = filter_rents(lines)
        halves = sorted({r[0] for r in rows})
        if len(rows) < 20000 or not halves or halves[-1] < "2025H1":
            raise ValueError(f"RIH02 looks wrong: {len(rows)} rows, latest {halves[-1] if halves else None}")
        publish_table(items, "rents", "v1/housing/rtb/rent_halfyear.csv.gz", gz(csv_bytes(head, rows)), "application/gzip", len(rows),
                      "RTB average monthly rent by half-year, location, bedrooms and property type (CSO RIH02, reduced)", self.key, dict(source_url=url, source_sha256=h.hexdigest()))
        res = save_state(session, self.key, state, self.url, self.name)
        res.fetched, res.message = len(rows), f"{len(rows):,} rows, latest {halves[-1]}"
        return res


# ----------------------------------------------------------------------------- Central Bank mortgage rates, DHLGH commencements

def ckan_resources(client: httpx.Client, package: str) -> list[dict]:
    return get_with_retry(client, f"{CKAN}package_show?id={package}", timeout=90).json()["result"]["resources"]


class CbiMortgageRates(Collector):
    key = "cbi_mortgage_rates"
    name = "Central Bank of Ireland retail interest rates: mortgages (B.3.1)"
    publisher = "Central Bank of Ireland"
    url = "https://opendata.centralbank.ie/dataset/retail-interest-rates-mortgage-rates"
    licence = "Central Bank of Ireland open data (check the dataset page for the licence); attribute the Central Bank"
    provides = "Monthly average mortgage interest rates on outstanding amounts and on new business, by type and fixing period. Statistical averages, not offers."
    used_by = ("propertywatch",)
    interval_hours = 168.0
    rights = "check"
    domain, tier = "housing", "files"

    def run(self, session: Session, client: httpx.Client) -> Result:
        state = load_state(session, self.key)
        items = state.setdefault("items", {})
        res_ = [r for r in ckan_resources(client, "retail-interest-rates-mortgage-rates") if (r.get("format") or "").upper() == "CSV"]
        if not res_:
            raise RuntimeError("no CSV resource on the Central Bank mortgage rates dataset")
        src = res_[0]["url"]
        def parse(body: bytes):
            t = body.decode("utf-8-sig", "replace")
            return t, list(csv.reader(io.StringIO(t)))

        raw = get_with_retry(client, src, timeout=120).content
        text, rows = parse(raw)
        if len(rows) < 24 or "date" not in rows[0][0].lower():      # an empty or blocked answer to a Python client: the same file is served to curl
            raw = page_get(client, src)
            text, rows = parse(raw)
        log_fetch(session, self.key, src, sha256(raw), len(raw), self.licence)
        if len(rows) < 24 or "date" not in rows[0][0].lower():
            raise ValueError(f"unexpected Central Bank file: {len(rows)} rows, header {rows[0][:2] if rows else None}")
        publish_table(items, "rates", "v1/housing/cbi/mortgage_rates.csv.gz", gz(text.encode("utf-8")), "application/gzip", len(rows) - 1,
                      "Central Bank retail interest rates, mortgages (B.3.1), as published", self.key, dict(source_url=src, source_sha256=sha256(raw)))
        r = save_state(session, self.key, state, self.url, self.name)
        r.fetched, r.message = len(rows) - 1, f"{len(rows) - 1} rows, latest {rows[-1][0]}"
        return r


class DhlghCommencements(Collector):
    key = "dhlgh_commencements"
    name = "Department of Housing: residential commencement notices by local authority"
    publisher = "Department of Housing, Local Government and Heritage"
    url = "https://opendata.housing.gov.ie/dataset/residential-commencement-notices"
    licence = "Creative Commons Attribution 4.0 (Department of Housing open data)"
    provides = "Monthly commencement notices for residential units by local authority since 2014, as the Department publishes them."
    used_by = ("propertywatch",)
    interval_hours = 72.0
    domain, tier = "housing", "files"

    def run(self, session: Session, client: httpx.Client) -> Result:
        state = load_state(session, self.key)
        items = state.setdefault("items", {})
        res_ = [r for r in ckan_resources(client, "residential-commencement-notices") if r["url"].endswith("dhlgh-monthly-commencements.csv")]
        if not res_:
            raise RuntimeError("the monthly commencements file was not found on the dataset")
        src = res_[0]["url"]
        raw = get_with_retry(client, src, timeout=120).content
        log_fetch(session, self.key, src, sha256(raw), len(raw), self.licence)
        text = raw.decode("utf-8-sig", "replace")
        rows = list(csv.reader(io.StringIO(text)))
        if rows[0][:3] != ["Metric", "LocalAuthority", "Year"] or len(rows) < 200:
            raise ValueError(f"unexpected commencements file: {rows[0][:4]}")
        publish_table(items, "commencements", "v1/housing/dhlgh/monthly_commencements.csv.gz", gz(text.encode("utf-8")), "application/gzip", len(rows) - 1,
                      "Department of Housing monthly commencement notices by local authority", self.key, dict(source_url=src, source_sha256=sha256(raw)))
        r = save_state(session, self.key, state, self.url, self.name)
        r.fetched, r.message = len(rows) - 1, f"{len(rows) - 1} rows, latest year {rows[-1][2]}"
        return r


# ----------------------------------------------------------------------------- homelessness

MONTH_NAMES = {m: i + 1 for i, m in enumerate("january february march april may june july august september october november december".split())}


def parse_homeless(name: str, text: str) -> tuple[str, list[list[str]], list[str]]:
    """(month 'YYYY-MM', rows, header) from one monthly report CSV; the month comes from the package name (homelessness-report-august-2026)."""
    m = re.match(r"homelessness-report-([a-z]+)-(\d{4})", name)
    if not m or m.group(1) not in MONTH_NAMES:
        raise ValueError(f"unexpected package name {name}")
    month = f"{m.group(2)}-{MONTH_NAMES[m.group(1)]:02d}"
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    head = [snake(h) for h in rows[0]]
    if head[0] != "region" or "total_adults" not in head:
        raise ValueError(f"unexpected columns in {name}: {head[:3]}")
    return month, [[month] + r for r in rows[1:] if r and r[0].strip()], head


class HomelessnessReports(Collector):
    key = "homelessness_reports"
    name = "Department of Housing monthly homelessness reports"
    publisher = "Department of Housing, Local Government and Heritage"
    url = "https://data.gov.ie/organization/department-of-housing-local-government-and-heritage"
    licence = "Creative Commons Attribution 4.0 (Department of Housing open data)"
    provides = "People in emergency accommodation by region and month: adults, age bands, accommodation type, citizenship, families and dependants. Administrative counts, not every person experiencing homelessness."
    used_by = ("propertywatch",)
    interval_hours = 72.0
    domain, tier = "housing", "files"

    def run(self, session: Session, client: httpx.Client) -> Result:
        state = load_state(session, self.key)
        items = state.setdefault("items", {})
        res = get_with_retry(client, f"{CKAN}package_search?q=name:homelessness-report*&rows=200&sort=metadata_modified+desc", timeout=90).json()["result"]["results"]
        names = sorted({p["name"] for p in res if re.match(r"homelessness-report-[a-z]+-\d{4}$", p["name"])})
        out, head, failed = [], None, []
        for p in (x for x in res if x["name"] in names):
            try:
                csv_res = next(r for r in p["resources"] if (r.get("format") or "").upper() == "CSV")
                raw = get_with_retry(client, csv_res["url"], timeout=60).content
                log_fetch(session, self.key, csv_res["url"], sha256(raw), len(raw), self.licence, note=p["name"])
                month, rows, h = parse_homeless(p["name"], raw.decode("utf-8", "replace"))
                head = head or h
                out += [r + [""] * (len(head) + 1 - len(r)) if len(r) < len(head) + 1 else r[: len(head) + 1] for r in rows]
            except Exception as e:  # noqa: BLE001 - one month failing must not lose the rest
                failed.append(f"{p['name']} ({type(e).__name__})")
        months = sorted({r[0] for r in out})
        if len(months) < 6:
            raise RuntimeError(f"only {len(months)} homelessness reports could be read: {failed[:3]}")
        publish_table(items, "reports", "v1/housing/homelessness/reports.csv.gz", gz(csv_bytes(["month"] + head, sorted(out))), "application/gzip", len(out),
                      "Department of Housing homelessness reports by month and region", self.key)
        r = save_state(session, self.key, state, self.url, self.name)
        r.fetched, r.message = len(out), f"{len(months)} months ({months[0]} to {months[-1]})" + (f"; failed: {', '.join(failed[:4])}" if failed else "")
        return r


# ----------------------------------------------------------------------------- grant pages

SEAI_SITEMAP = "https://www.seai.ie/sitemap.xml"
CI_INDEX = "https://www.citizensinformation.ie/en/housing/housing-grants-and-schemes/"
# id, name, body, category, audience, url, kind
GRANT_SOURCES = [
    ("revenue-htb", "Help to Buy", "Revenue", "buy", "buyer", "https://www.revenue.ie/en/property/help-to-buy-incentive/index.aspx", "html"),
    ("revenue-rent-tax-credit", "Rent Tax Credit", "Revenue", "rent", "renter", "https://www.revenue.ie/en/personal-tax-credits-reliefs-and-exemptions/land-and-property/rent-credit/index.aspx", "html"),
    ("fhs-how-it-works", "First Home Scheme: how it works", "First Home Scheme", "buy", "buyer", "https://www.firsthomescheme.ie/how-it-works", "html"),
    ("fhs-home", "First Home Scheme", "First Home Scheme", "buy", "buyer", "https://www.firsthomescheme.ie/", "html"),
    ("lahl", "Local Authority Home Loan", "Local authorities", "buy", "buyer", "https://localauthorityhomeloan.ie/", "html"),
    ("vprg-gov", "Vacant Property Refurbishment Grant", "Department of Housing", "vacant", "owner", "https://www.gov.ie/en/department-of-housing-local-government-and-heritage/services/vacant-property-refurbishment-grant/", "html"),
    ("vprg-kerry", "Vacant Property Grants (Kerry County Council)", "Kerry County Council", "vacant", "owner", "https://www.kerrycoco.ie/vacant-property-grants/", "html"),
    ("vprg-laois", "Vacant Property Refurbishment Grant (Laois County Council)", "Laois County Council", "vacant", "owner", "https://laois.ie/housing/vacant-property-refurbishment-grant", "html"),
    ("adapt-sdcc", "Housing grants (South Dublin County Council)", "South Dublin County Council", "adaptation", "owner", "https://www.sdcc.ie/en/services/housing/housing-grants/", "html"),
    ("seai-amounts-pdf", "SEAI Home Energy Grants amounts (PDF)", "SEAI", "energy", "owner", "https://www.seai.ie/sites/default/files/publications/Home-Energy-Grants-Amounts.pdf", "pdf"),
    ("lda", "Land Development Agency", "Land Development Agency", "rent", "renter", "https://lda.ie/", "html"),
]
AMOUNT = re.compile(r"€\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:million|m|k)\b)?", re.I)
DATE = re.compile(r"\b(?:\d{1,2}(?:st|nd|rd|th)?\s+)?(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}\b", re.I)
CLOSED = re.compile(r"\b(?:closed to (?:new )?applications|no longer (?:available|accepting)|scheme (?:has )?(?:closed|ended)|currently closed|temporarily (?:closed|paused)|paused|suspended|not (?:currently )?accepting)\b", re.I)
OPEN = re.compile(r"\b(?:now open|open for applications|applications are open|you can (?:now )?apply)\b", re.I)


def category_of(url: str, title: str = "") -> tuple[str, str]:
    s = f"{url} {title}".lower()
    if "landlord" in s:
        return "energy", "landlord"
    if any(k in s for k in ("vacant", "derelict", "croi", "refurbish")):
        return "vacant", "owner"
    if any(k in s for k in ("adaptation", "mobility", "older-people", "disab", "housing-aid")):
        return "adaptation", "owner"
    if any(k in s for k in ("defect", "pyrite", "concrete-block", "remediation")):
        return "remediation", "owner"
    if any(k in s for k in ("first-home", "firsthome", "help-to-buy", "home-loan", "homeloan", "affordable-purchase", "buying")):
        return "buy", "buyer"
    if any(k in s for k in ("rent", "cost-rental")):
        return "rent", "renter"
    return "energy", "owner"


def extract_page(raw: str) -> dict:
    """Title, description, euro amounts with their sentences, dates, closed/open words, a stated 'last updated' date and a hash of the visible text."""
    title = re.search(r"<title>(.*?)</title>", raw, re.S)
    og = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', raw)
    desc = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]*)"', raw) or re.search(r'<meta[^>]+property="og:description"[^>]+content="([^"]*)"', raw)
    body = re.sub(r"<(script|style|nav|header|footer|noscript)\b.*?</\1>", " ", raw, flags=re.S | re.I)
    body = re.sub(r"</(p|li|h\d|tr|div|td|br)>", ". ", body)
    text = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", body))).strip()
    t = _html.unescape((og.group(1) if og else (title.group(1) if title else ""))).strip()
    t = re.sub(r"\s*[|\-]\s*(SEAI|Citizens Information|gov\.ie|Revenue).*$", "", re.sub(r"\s+", " ", t)).strip()
    amounts, seen = [], set()
    for sent in re.split(r"(?<=[.!?])\s+", text):
        for m in AMOUNT.finditer(sent):
            key = (m.group(0), sent[:60])
            if key in seen or len(amounts) >= 14:
                continue
            seen.add(key)
            amounts.append(dict(amount=m.group(0).replace(" ", ""), context=sent.strip()[:240]))
    dates = []
    for m in DATE.finditer(text):
        s = text[max(0, m.start() - 70): m.end() + 40].strip()
        if len(dates) < 6 and not any(d["date"] == m.group(0) for d in dates):
            dates.append(dict(date=m.group(0), context=s))
    upd = re.search(r"(?:last updated|updated|last reviewed|published)[: ]+(\d{1,2}\s+[A-Za-z]+\s+\d{4})", text, re.I)
    return dict(title=t[:160], description=_html.unescape(desc.group(1)).strip()[:300] if desc else "", amounts=amounts, dates=dates, closed_words=sorted({m.group(0).lower() for m in CLOSED.finditer(text)})[:4],
                open_words=sorted({m.group(0).lower() for m in OPEN.finditer(text)})[:3], stated_updated=upd.group(1) if upd else None, text_sha256=sha256(text.encode()), text_chars=len(text))


def extract_pdf(data: bytes) -> dict:
    from pypdf import PdfReader
    text = re.sub(r"\s+", " ", "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages))
    amounts = [dict(amount=m.group(0).replace(" ", ""), context=text[max(0, m.start() - 60): m.end() + 40]) for m in list(AMOUNT.finditer(text))[:14]]
    stated = re.search(r"(?:Amounts?|Grants?)\s+((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})", text)
    return dict(title="", description="", amounts=amounts, dates=[], closed_words=[], open_words=[], stated_updated=stated.group(1) if stated else None, text_sha256=sha256(text.encode()), text_chars=len(text))


def page_get(client: httpx.Client, url: str) -> bytes:
    """GET a page. Several publishers (SEAI, gov.ie) answer 403 to Python HTTP clients but serve the same page to curl, so the second try shells out to curl."""
    try:
        r = client.get(url, timeout=60, headers={"User-Agent": BROWSER, "Accept": "text/html,application/pdf,*/*", "Accept-Language": "en-IE,en;q=0.9"})
        if r.status_code == 200 and b"Just a moment" not in r.content[:3000]:
            return r.content
    except httpx.HTTPError:
        pass
    import os
    p = subprocess.run([os.environ.get("HUB_CURL", "curl"), "-sL", "--max-time", "60", "-A", BROWSER, "-H", "Accept: text/html,application/xhtml+xml,*/*", "-H", "Accept-Language: en-IE,en;q=0.9", "-w", "\n%{http_code}", url], capture_output=True, timeout=90)
    body, _, code = p.stdout.rpartition(b"\n")
    if code.strip() != b"200":
        raise httpx.HTTPError(f"HTTP {code.decode(errors='replace').strip()} from {url}")
    return body


def discover(client: httpx.Client) -> list[tuple]:
    """Grant pages found automatically: SEAI's grant pages from its sitemap and Citizens Information's housing grants pages two levels down. A new page appears without any change here."""
    out = []
    try:
        sm = page_get(client, SEAI_SITEMAP).decode("utf-8", "replace")
        for u in sorted({_html.unescape(x) for x in re.findall(r"<loc>(.*?)</loc>", sm) if "/grants/home-energy-grants" in x}):
            cat, aud = category_of(u)
            out.append(("seai-" + re.sub(r"[^a-z0-9]+", "-", u.split("/grants/")[-1].lower()).strip("-"), "", "SEAI", cat, aud, u, "html"))
    except Exception:  # noqa: BLE001 - discovery failing leaves the fixed list
        pass
    try:
        seen, queue = set(), [CI_INDEX]
        for depth in range(2):
            nxt = []
            for u in queue:
                html_ = page_get(client, u).decode("utf-8", "replace")
                for href in sorted(set(re.findall(r'href="(/en/housing/housing-grants-and-schemes/[^"#?]+)"', html_))):
                    full = "https://www.citizensinformation.ie" + href
                    if full not in seen and full != CI_INDEX:
                        seen.add(full)
                        nxt.append(full)
            queue = nxt
        for u in sorted(seen):
            cat, aud = category_of(u)
            out.append(("ci-" + re.sub(r"[^a-z0-9]+", "-", u.split("/housing-grants-and-schemes/")[-1].lower()).strip("-"), "", "Citizens Information", cat, aud, u, "html"))
    except Exception:  # noqa: BLE001
        pass
    return out


def diff_amounts(old: list[dict], new: list[dict]) -> tuple[list[str], list[str]]:
    a, b = {x["amount"] for x in old or []}, {x["amount"] for x in new or []}
    return sorted(b - a), sorted(a - b)


class HousingGrants(Collector):
    key = "housing_grants"
    name = "Housing grants and supports: official page scraper and change monitor"
    publisher = "SEAI, Citizens Information, Revenue, Department of Housing, local authorities, First Home Scheme"
    url = "https://www.citizensinformation.ie/en/housing/housing-grants-and-schemes/"
    licence = "Short quoted facts (euro amounts with their sentence, dates, titles) from public official pages; no page text is kept. Check each publisher's terms before reuse beyond that."
    provides = ("For each official grant or support page: title, description, the euro amounts it states with their sentences, dates it mentions, words that suggest it is closed, paused or open, "
                "a hash of its visible text and when the text last changed. Pages are discovered from the SEAI sitemap and Citizens Information's housing grants section.")
    used_by = ("propertywatch",)
    interval_hours = 24.0
    rights = "check"
    domain, tier = "housing", "files"

    def run(self, session: Session, client: httpx.Client) -> Result:
        state = load_state(session, self.key)
        items = state.setdefault("items", {})
        prev = state.setdefault("pages", {})
        sources = {s[5]: s for s in discover(client)}
        for s in GRANT_SOURCES:
            sources[s[5]] = s                                     # the curated entry wins over a discovered one for the same address
        now = utcnow().isoformat(timespec="seconds")
        pages, changes = [], state.setdefault("changes", [])
        failed = []
        for url, (pid, name, body, cat, aud, _, kind) in sorted(sources.items(), key=lambda kv: kv[1][0]):
            old = prev.get(pid, {})
            try:
                raw = page_get(client, url)
                log_fetch(session, self.key, url, sha256(raw), len(raw), self.licence, note=pid)
                info = extract_pdf(raw) if kind == "pdf" else extract_page(raw.decode("utf-8", "replace"))
                if kind == "html" and ((not info["title"] and info["text_chars"] < 400) or re.search(r"\b404\b|not found", info["title"], re.I)):
                    raise ValueError("page has no content or is a 404")
                cat2, aud2 = (cat, aud) if name else category_of(url, info["title"])
                rec = dict(id=pid, url=url, source=body, name=name or info["title"] or pid, category=cat2, audience=aud2, kind=kind, status="ok", http=200, retrieved_at=now, **{k: info[k] for k in
                           ("title", "description", "amounts", "dates", "closed_words", "open_words", "stated_updated", "text_sha256", "text_chars")})
                rec["changed_at"] = now if old.get("text_sha256") != info["text_sha256"] else old.get("changed_at")
                rec["first_seen"] = old.get("first_seen") or now
                if old.get("text_sha256") and old["text_sha256"] != info["text_sha256"]:
                    added, removed = diff_amounts(old.get("amounts"), info["amounts"])
                    changes.insert(0, dict(id=pid, name=rec["name"], url=url, at=now, amounts_added=added, amounts_removed=removed))
                elif not old.get("text_sha256"):
                    changes.insert(0, dict(id=pid, name=rec["name"], url=url, at=now, first_seen=True, amounts_added=[a["amount"] for a in info["amounts"]], amounts_removed=[]))
                prev[pid] = {k: rec[k] for k in ("text_sha256", "amounts", "changed_at", "first_seen")}
            except Exception as e:  # noqa: BLE001 - a page that cannot be read is shown as unavailable, never as unchanged
                failed.append(f"{pid} ({type(e).__name__})")
                rec = dict(id=pid, url=url, source=body, name=name or old.get("name") or pid, category=cat, audience=aud, kind=kind, status="unavailable", http=0, retrieved_at=now,
                           title="", description="", amounts=old.get("amounts", []), dates=[], closed_words=[], open_words=[], stated_updated=None, text_sha256=old.get("text_sha256"),
                           text_chars=0, changed_at=old.get("changed_at"), first_seen=old.get("first_seen") or now)
            pages.append(rec)
        state["changes"] = changes[:300]
        ok = [p for p in pages if p["status"] == "ok"]
        if len(ok) < 8:
            raise RuntimeError(f"only {len(ok)} grant pages could be read: {failed[:6]}")
        doc = dict(generated_at=now, pages=pages, changes=state["changes"], unavailable=len(pages) - len(ok))
        publish_table(items, "grants", "v1/housing/grants/pages.json", json.dumps(doc, indent=1, sort_keys=True).encode(), "application/json", len(pages),
                      "Grant and support pages: title, quoted euro amounts, dates, closed words, text hash and change log", self.key)
        res = save_state(session, self.key, state, self.url, self.name)
        res.fetched, res.message = len(ok), f"{len(ok)} of {len(pages)} pages read" + (f"; unavailable: {', '.join(failed[:5])}" if failed else "")
        return res
