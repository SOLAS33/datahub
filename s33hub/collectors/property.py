"""Property Price Register: every residential property sale notified to the Property Services Regulatory Authority since 2010.

Source: https://www.propertypriceregister.ie/ (a zip of one CSV, ~700,000 sales, updated as sales are registered). Published under CC BY 4.0.

Privacy: the register includes the full street address of each home. The hub republishes the sale (date, county, price, flags,
type, size band) and only the Eircode *routing key* (its first three characters, an area of about a postcode district), not the
address. That keeps the analysis value (price by area and time) and avoids republishing a searchable list of individual homes.
Published as one file per year, plus monthly county medians.
"""
from __future__ import annotations

import csv
import io
import re
import statistics
import zipfile
from collections import defaultdict
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from s33weather.models import sha256

from .base import Collector, Result, get_with_retry, log_fetch
from .tables import Tally, csv_bytes, decode, gz, load_state, publish_table, save_state

URL = "https://www.propertypriceregister.ie/website/npsra/ppr/npsra-ppr.nsf/Downloads/PPR-ALL.zip/$FILE/PPR-ALL.zip"
OUT = ["sale_date", "county", "eircode_routing_key", "price_eur", "not_full_market_price", "vat_exclusive", "description", "size_band"]


def _price(v: str) -> str:
    v = re.sub(r"[^\d.]", "", (v or "").replace(",", ""))
    try:
        return f"{float(v):.2f}".rstrip("0").rstrip(".") if v else ""
    except ValueError:
        return ""


def _yes(v: str) -> str:
    return "1" if (v or "").strip().lower() == "yes" else "0"


def normalise_ppr(text: str) -> list[list[str]]:
    rd = csv.reader(io.StringIO(text, newline=""))
    head = [h.strip() for h in next(rd)]
    col = {h.split(" (")[0].lower(): i for i, h in enumerate(head)}
    need = ("date of sale", "county", "eircode", "price", "not full market price", "vat exclusive", "description of property", "property size description")
    idx = [col.get(n) for n in need]
    if any(i is None for i in idx[:4]):
        raise ValueError(f"unexpected Property Price Register header: {head}")
    out = []
    for r in rd:
        if len(r) < 5:
            continue
        g = lambda i: (r[i].strip() if i is not None and i < len(r) else "")  # noqa: E731
        try:
            d = datetime.strptime(g(idx[0]), "%d/%m/%Y").date().isoformat()
        except ValueError:
            continue
        out.append([d, g(idx[1]), g(idx[2]).replace(" ", "")[:3].upper(), _price(g(idx[3])), _yes(g(idx[4])), _yes(g(idx[5])), g(idx[6]), g(idx[7])])
    return out


class PropertyPriceRegister(Collector):
    key = "property_price_register"
    name = "Residential Property Price Register"
    publisher = "Property Services Regulatory Authority (PSRA)"
    url = "https://www.propertypriceregister.ie/"
    licence = "Creative Commons Attribution 4.0"
    provides = "Every residential sale since 2010: date, county, Eircode routing key, price, market-price and VAT flags, type, size band. One file per year, plus monthly county medians. No street addresses."
    interval_hours = 24.0
    domain, tier = "property", "files"

    def run(self, session: Session, client: httpx.Client) -> Result:
        state = load_state(session, self.key)
        items = state.setdefault("items", {})
        head = client.head(URL)
        lm, ln = head.headers.get("last-modified", ""), head.headers.get("content-length", "")
        if items.get("_source", {}).get("last_modified") == lm and items.get("_source", {}).get("content_length") == ln and lm:
            return Result(message=f"unchanged at source (last modified {lm})")
        raw = get_with_retry(client, URL, timeout=600).content
        digest = sha256(raw)
        log_fetch(session, self.key, URL, digest, len(raw), self.licence)
        z = zipfile.ZipFile(io.BytesIO(raw))
        rows = normalise_ppr(decode(z.read(z.namelist()[0])))
        if len(rows) < 100_000:
            raise ValueError(f"register looks wrong: {len(rows)} sales")
        by_year: dict[str, list] = defaultdict(list)
        monthly: dict[tuple, list[float]] = defaultdict(list)
        for r in rows:
            by_year[r[0][:4]].append(r)
            if r[3] and r[4] == "0":
                monthly[(r[0][:7], r[1])].append(float(r[3]))
        tally = Tally()
        for y, rs in sorted(by_year.items()):
            if publish_table(items, y, f"v1/property/ppr/{y}.csv.gz", gz(csv_bytes(OUT, sorted(rs))), "application/gzip", len(rs), f"Property Price Register sales in {y}", self.key):
                tally.published += 1
            else:
                tally.unchanged += 1
        med = csv_bytes(["month", "county", "sales", "median_price_eur", "mean_price_eur"],
                        ([m, c, len(v), round(statistics.median(v)), round(sum(v) / len(v))] for (m, c), v in sorted(monthly.items())))
        publish_table(items, "_monthly", "v1/property/ppr_monthly_county.csv.gz", gz(med), "application/gzip", len(monthly),
                      "Monthly median and mean sale price by county (full-market-price sales only)", self.key)
        items["_source"] = dict(last_modified=lm, content_length=ln, sha256=digest, sales=len(rows), newest_sale=max(r[0] for r in rows))
        res = save_state(session, self.key, state, self.url, self.name)
        res.fetched, res.message = len(rows), f"{len(rows):,} sales; {tally.message(len(by_year))}; newest {items['_source']['newest_sale']}"
        return res
