"""Public procurement: who is buying, what is open, and who won at what price.

Two sources, both public, collected once here and shared by every Solas33 site (TenderWatch reads the
published files; it never contacts these services itself):

* `etenders_opendata` - the Office of Government Procurement's open dataset of every competition
  published on eTenders since 2013, with award details (suppliers, awarded value, number of bids)
  for most. Updated about quarterly. Published normalised as v1/tenders/etenders_notices.csv.gz and,
  untouched, as v1/tenders/raw/<date>-<sha12>.csv.gz so any figure can be checked against the
  original.
* `ted_ireland` - EU Tenders Electronic Daily notices whose buyer is in Ireland (above-threshold
  procurement): contract notices (open tenders, with deadlines) and contract award notices
  (winner names, values, bids received). Updated daily; kept in the hub database and published by
  year as v1/tenders/ted_notices/YYYY.csv.

The two overlap for large contracts; they are published separately and never merged here. Every
notice keeps its source identifiers and a link back to the original.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import re
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from s33weather.models import sha256

from .. import artifacts
from ..db import utcnow
from ..models import Mirror, TedNotice
from .base import Collector, Result, add_event, get_with_retry, log_fetch
from .mirrors import store_mirror

CKAN = "https://data.gov.ie/api/3/action/package_show?id=contract-notices-published-on-etenders"
FALLBACK_CSV = "https://assets.gov.ie/static/documents/4d482e0e/Public_Procurement_Opendata_Dataset.csv"

# source column (after dropping "Sum of " and the euro sign) -> published column
ETENDERS_COLUMNS = {
    "Tender ID": "tender_id", "Parent Agreement ID": "parent_id", "Source": "source", "Platform": "platform",
    "Contracting Authority": "authority", "Name of Client Contracting Authority": "client_authority", "Agreement Owner": "agreement_owner",
    "Tender Name": "title", "Tender/Contract Name": "title",
    "Notice Published Date": "published", "Notice Published Date / Contract Created Date": "published",
    "Directive": "directive", "Competition Type": "competition_type", "Main Cpv Code": "cpv", "Main Cpv Code Description": "cpv_description",
    "Spend Category": "spend_category", "Contract Type": "contract_type", "Threshold Level": "threshold", "Procedure": "procedure",
    "Tender Submission Deadline": "deadline", "Evaluation Type": "evaluation_type",
    "Notice Estimated Value": "estimated_value_eur", "Contract Duration (Months)": "duration_months", "Cancelled Date": "cancelled",
    "Award Published": "award_published", "Awarded Value": "awarded_value_eur", "No of Bids Received": "bids",
    "No of SMEs Bids Received": "sme_bids", "Awarded Suppliers": "suppliers", "No of Awarded SMEs": "awarded_smes",
    "TED Notice Link": "ted_notice_link", "TED CAN Link": "ted_can_link",
}
ETENDERS_OUT = ["tender_id", "parent_id", "source", "platform", "authority", "client_authority", "agreement_owner", "title", "published", "directive",
                "competition_type", "cpv", "cpv_description", "spend_category", "contract_type", "threshold", "procedure", "deadline", "evaluation_type",
                "estimated_value_eur", "duration_months", "cancelled", "award_published", "awarded_value_eur", "bids", "sme_bids", "suppliers",
                "awarded_smes", "ted_notice_link", "ted_can_link"]
DATE_COLS = ("published", "deadline", "cancelled", "award_published")
NUM_COLS = ("estimated_value_eur", "duration_months", "awarded_value_eur", "bids", "sme_bids", "awarded_smes")


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _canon_header(h: str) -> str:
    h = h.strip().lstrip("﻿")
    h = re.sub(r"^Sum of ", "", h)
    h = re.sub(r"\s*\([^)]*\)\s*$", "", h) if h.startswith(("Notice Estimated Value", "Awarded Value")) else h
    return h


def _iso(v: str) -> str:
    v = (v or "").strip()
    if not v or v.upper() == "NULL":
        return ""
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _num(v: str) -> str:
    v = (v or "").strip().replace(",", "")
    if not v or v.upper() == "NULL":
        return ""
    try:
        f = float(v)
    except ValueError:
        return ""
    return str(int(f)) if f == int(f) else f"{f:.2f}"


def _text(v: str) -> str:
    v = (v or "").strip()
    return "" if v.upper() == "NULL" else re.sub(r"\s+", " ", v)


def normalise_etenders(text: str) -> list[dict]:
    """Rows of the OGP open dataset in one stable shape, whichever of its two header layouts the file uses.
    Dates become ISO, numbers plain, 'NULL' becomes empty, suppliers are ';'-separated with spacing tidied."""
    rd = csv.reader(io.StringIO(text, newline=""))
    header = [_canon_header(h) for h in next(rd)]
    idx: dict[str, int] = {}
    for i, h in enumerate(header):
        out = ETENDERS_COLUMNS.get(h)
        if out and out not in idx:
            idx[out] = i
    out_rows = []
    for r in rd:
        if not r or len(r) < 3:
            continue
        row = {k: (r[i] if i < len(r) else "") for k, i in idx.items()}
        for c in DATE_COLS:
            row[c] = _iso(row.get(c, ""))
        for c in NUM_COLS:
            row[c] = _num(row.get(c, ""))
        sup = _text(row.get("suppliers", ""))
        row["suppliers"] = "; ".join(s.strip() for s in sup.split(";") if s.strip())
        for c in ETENDERS_OUT:
            if c not in DATE_COLS and c not in NUM_COLS and c != "suppliers":
                row[c] = _text(row.get(c, ""))
        out_rows.append({c: row.get(c, "") for c in ETENDERS_OUT})
    return out_rows


def _csv_bytes(header: list[str], rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


def _gz(data: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as g:
        g.write(data)
    return buf.getvalue()


def _meta(session: Session, key: str) -> dict:
    m = session.scalar(select(Mirror).where(Mirror.key == key))
    try:
        return json.loads(m.body) if m and m.body else {}
    except ValueError:
        return {}


class EtendersOpenData(Collector):
    key = "etenders_opendata"
    name = "eTenders open dataset - every published competition since 2013, with awards"
    publisher = "Office of Government Procurement via data.gov.ie"
    url = "https://data.gov.ie/dataset/contract-notices-published-on-etenders"
    licence = "Creative Commons Attribution 4.0"
    provides = ("Every competition published on eTenders since 2013: authority, title, CPV, procedure, deadline, estimated value and, where awarded, "
                "suppliers, awarded value and number of bids. Normalised CSV plus the untouched original.")
    used_by = ("tenderwatch",)
    interval_hours = 24.0

    def locate(self, client: httpx.Client) -> str:
        try:
            r = get_with_retry(client, CKAN)
            res = [x for x in r.json()["result"]["resources"] if str(x.get("format", "")).upper() == "CSV" and x.get("url")]
            if res:
                return res[0]["url"]
        except Exception:  # noqa: BLE001 - the catalogue is a convenience; the known file still works
            pass
        return FALLBACK_CSV

    def run(self, session: Session, client: httpx.Client) -> Result:
        url = self.locate(client)
        prev = _meta(session, self.key)
        head = client.head(url)
        lm, ln = head.headers.get("last-modified", ""), head.headers.get("content-length", "")
        if prev and prev.get("url") == url and prev.get("last_modified") == lm and prev.get("content_length") == ln and prev.get("sha256"):
            return Result(message=f"unchanged at source (last modified {lm or 'n/a'})")
        r = get_with_retry(client, url)
        raw = r.content
        digest = sha256(raw)
        log_fetch(session, self.key, url, digest, len(raw), self.licence)
        if prev.get("sha256") == digest:   # modified header moved but the bytes did not
            meta = dict(prev, last_modified=lm, content_length=ln)
            return store_mirror(session, self.key, json.dumps(meta, sort_keys=True).encode(), "json", prev.get("rows", 0), [], self.url, self.name)
        rows = normalise_etenders(_decode(raw))
        if len(rows) < 1000 or "tender_id" not in rows[0] or not rows[0]["tender_id"]:
            raise ValueError(f"eTenders file looks wrong: {len(rows)} rows")
        awarded = sum(1 for x in rows if x["suppliers"])
        data = _csv_bytes(ETENDERS_OUT, ([x[c] for c in ETENDERS_OUT] for x in rows))
        today = utcnow().strftime("%Y-%m-%d")
        artifacts.put("v1/tenders/etenders_notices.csv.gz", _gz(data), "application/gzip", len(rows),
                      "Every eTenders competition since 2013, normalised: ISO dates, plain numbers, suppliers ';'-separated", self.key)
        artifacts.put(f"v1/tenders/raw/{today}-{digest[:12]}.csv.gz", _gz(raw), "application/gzip", len(rows),
                      "The OGP open dataset exactly as downloaded (gzip only), kept so any figure can be checked against the original", self.key)
        meta = dict(url=url, last_modified=lm, content_length=ln, sha256=digest, rows=len(rows), awarded_rows=awarded,
                    raw_path=f"v1/tenders/raw/{today}-{digest[:12]}.csv.gz", note="Original file checksum; normalised file is v1/tenders/etenders_notices.csv.gz")
        res = store_mirror(session, self.key, json.dumps(meta, sort_keys=True).encode(), "json", len(rows), [], self.url, self.name)
        res.fetched = len(rows)
        latest = max((x["published"] for x in rows if x["published"]), default="")
        add_event(session, "tenders_dataset", f"eTenders open dataset updated: {len(rows):,} competitions, {awarded:,} with awards, latest notice {latest}",
                  key=f"etenders:{digest[:16]}", source_url=self.url, source=self.key)
        return res


# --------------------------------------------------------------------------- TED

TED_API = "https://api.ted.europa.eu/v3/notices/search"
TED_FIELDS = ["publication-number", "notice-type", "publication-date", "buyer-name", "title-proc", "title-lot", "classification-cpv",
              "contract-nature-main-proc", "estimated-value-lot", "tender-value", "total-value", "organisation-name-tenderer", "winner-size",
              "winner-selection-status", "received-submissions-type-val", "deadline-receipt-tender-date-lot", "links"]
TED_FIRST_YEAR = 2024
TED_OUT = ["publication_number", "notice_type", "publication_date", "buyer", "title", "cpv", "nature", "estimated_value_eur", "award_value_eur",
           "winners", "winner_size", "bids", "deadline", "url"]


def _first_text(v) -> str:
    """TED returns multilingual fields as {lang: text or [text]}; prefer English, then any."""
    if isinstance(v, dict):
        for k in ("eng", *v.keys()):
            if k in v and v[k]:
                x = v[k]
                return _text(x[0] if isinstance(x, list) else str(x))
        return ""
    if isinstance(v, list):
        return _text(str(v[0])) if v else ""
    return _text(str(v)) if v is not None else ""


def _all_text(v) -> list[str]:
    if isinstance(v, dict):
        for k in ("eng", *v.keys()):
            if k in v and v[k]:
                x = v[k]
                return [_text(i) for i in (x if isinstance(x, list) else [x]) if _text(i)]
        return []
    if isinstance(v, list):
        return [_text(str(i)) for i in v if _text(str(i))]
    return [_text(str(v))] if v else []


def _ted_date(v) -> date | None:
    if isinstance(v, list):
        v = v[0] if v else None
    if not v:
        return None
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _ted_float(v) -> float | None:
    vals = v if isinstance(v, list) else [v]
    tot, ok = 0.0, False
    for x in vals:
        try:
            tot += float(x)
            ok = True
        except (TypeError, ValueError):
            continue
    return tot if ok else None


def parse_ted(n: dict) -> dict:
    """One TED search hit -> TedNotice columns."""
    nature = n.get("contract-nature-main-proc")
    cpvs = [c for c in dict.fromkeys(n.get("classification-cpv") or [])]
    winners = list(dict.fromkeys(_all_text(n.get("organisation-name-tenderer"))))
    est = n.get("estimated-value-lot")
    tv = n.get("total-value")
    award = float(tv) if isinstance(tv, (int, float)) else _ted_float(n.get("tender-value"))
    links = (n.get("links") or {}).get("html", {})
    num = n["publication-number"]
    bids = _ted_float(n.get("received-submissions-type-val"))
    return dict(publication_number=num, notice_type=n.get("notice-type"), publication_date=_ted_date(n.get("publication-date")),
                buyer=_first_text(n.get("buyer-name")), title=(_first_text(n.get("title-proc")) or _first_text(n.get("title-lot")))[:500],
                cpv=cpvs[0] if cpvs else None, cpv_all=",".join(cpvs[:8]) or None, nature=nature if isinstance(nature, str) else None,
                estimated_value=_ted_float(est), award_value=award, winners="; ".join(winners)[:1000] or None,
                winner_size=",".join(dict.fromkeys(n.get("winner-size") or [])) or None, bids=int(bids) if bids is not None else None,
                deadline=_ted_date(n.get("deadline-receipt-tender-date-lot")), url=links.get("ENG") or f"https://ted.europa.eu/en/notice/-/detail/{num}")


class TedIreland(Collector):
    key = "ted_ireland"
    name = "TED - EU tender and award notices from Irish buyers"
    publisher = "Publications Office of the European Union (TED)"
    url = "https://ted.europa.eu/en/"
    licence = "Reusable under the EU Commission reuse decision (CC BY 4.0 equivalent; attribute TED)"
    provides = ("Above-threshold Irish procurement: contract notices (open tenders with deadlines) and contract award notices "
                "(winner, value, bids received), by publication date. Every notice links to the original.")
    used_by = ("tenderwatch",)
    interval_hours = 6.0
    page = 250

    def search(self, client: httpx.Client, query: str, page: int) -> dict:
        body = {"query": query, "fields": TED_FIELDS, "page": page, "limit": self.page, "scope": "ACTIVE", "paginationMode": "PAGE_NUMBER"}
        last = None
        for attempt in range(3):
            r = client.post(TED_API, json=body)
            if r.status_code == 200:
                return r.json()
            last = RuntimeError(f"TED HTTP {r.status_code}: {r.text[:200]}")
            if r.status_code < 500 and r.status_code != 429:
                break
            import time
            time.sleep(2 * (attempt + 1))
        raise last  # type: ignore[misc]

    def fetch_range(self, session: Session, client: httpx.Client, start: date, end: date) -> tuple[int, int]:
        created = changed = 0
        page = 1
        q = f"organisation-country-buyer=IRL AND publication-date>={start:%Y%m%d} AND publication-date<={end:%Y%m%d}"
        while True:
            d = self.search(client, q, page)
            hits = d.get("notices", [])
            raw = json.dumps(hits, sort_keys=True).encode()
            log_fetch(session, self.key, f"{TED_API} [{start}..{end} page {page}]", sha256(raw), len(raw), self.licence)
            for n in hits:
                try:
                    row = parse_ted(n)
                except Exception:  # noqa: BLE001 - one malformed notice must not lose the page
                    continue
                cur = session.scalar(select(TedNotice).where(TedNotice.publication_number == row["publication_number"]))
                if cur is None:
                    session.add(TedNotice(**row, first_seen=utcnow()))
                    created += 1
                else:
                    if any(getattr(cur, k) != v for k, v in row.items()):
                        for k, v in row.items():
                            setattr(cur, k, v)
                        changed += 1
            session.flush()
            if len(hits) < self.page:
                break
            page += 1
        return created, changed

    def run(self, session: Session, client: httpx.Client) -> Result:
        newest = session.scalar(select(TedNotice.publication_date).order_by(TedNotice.publication_date.desc()).limit(1))
        today = utcnow().date()
        created = changed = 0
        if newest is None:     # first run: month by month from the first year we publish
            d = date(TED_FIRST_YEAR, 1, 1)
            while d <= today:
                nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
                c, ch = self.fetch_range(session, client, d, min(nxt - timedelta(days=1), today))
                created, changed = created + c, changed + ch
                session.commit()
                d = nxt
        else:                  # afterwards: the last few days again (notices are corrected and deadlines extended)
            created, changed = self.fetch_range(session, client, newest - timedelta(days=7), today)
        total = session.query(TedNotice).count()
        return Result(fetched=created + changed, created=created, changed=changed, message=f"{total:,} notices held")


def ted_files(session: Session) -> dict[str, dict]:
    """Published layout: v1/tenders/ted_notices/YYYY.csv"""
    out: dict[str, dict] = {}
    years = sorted({d.year for d in session.scalars(select(TedNotice.publication_date)) if d})
    for y in years:
        rows = session.scalars(select(TedNotice).where(TedNotice.publication_date >= date(y, 1, 1), TedNotice.publication_date < date(y + 1, 1, 1))
                               .order_by(TedNotice.publication_date, TedNotice.publication_number)).all()
        data = _csv_bytes(TED_OUT, ([r.publication_number, r.notice_type, r.publication_date.isoformat() if r.publication_date else "", r.buyer, r.title, r.cpv or "",
                                     r.nature or "", "" if r.estimated_value is None else f"{r.estimated_value:.2f}", "" if r.award_value is None else f"{r.award_value:.2f}",
                                     r.winners or "", r.winner_size or "", "" if r.bids is None else r.bids, r.deadline.isoformat() if r.deadline else "", r.url] for r in rows))
        out[f"v1/tenders/ted_notices/{y}.csv"] = dict(data=data, type="text/csv", rows=len(rows), source="ted_ireland",
                                                      desc="TED notices (contract notices and award notices) from Irish buyers, by publication year")
    return out
