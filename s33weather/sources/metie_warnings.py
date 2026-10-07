"""Met Éireann national weather warnings (JSON feed behind met.ie's warnings page).

An empty list means no warnings are in force. Field names are read defensively because the
feed is not formally versioned; anything unrecognised is kept in `raw` for traceability.
Region codes are Met Éireann's EI-prefixed county codes (see COUNTY_CODES).
"""
from __future__ import annotations

import json
from datetime import datetime

import httpx

from ..http import get
from ..models import Fetch, Provenance, Warning, sha256, utcnow

KEY = "metie_warnings"
PUBLISHER = "Met Éireann"
LICENCE = "CC BY 4.0 - Met Éireann (attribution required)"
URL = "https://www.met.ie/warningsxml/rss.xml"          # the documented RSS feed; each item links to a CAP file. The older JSON file returns 404 (checked 2026-10-06).
JSON_URL = "https://www.met.ie/Open_Data/json/warning_IRELAND.json"   # kept for parse() and its tests
LANDING = "https://www.met.ie/warnings"

# FIPS 10-4 county codes, which the warning feed uses. A code not listed here is shown as-is
# rather than guessed.
COUNTY_CODES = {
    "EI01": "Carlow", "EI02": "Cavan", "EI03": "Clare", "EI04": "Cork", "EI06": "Donegal", "EI07": "Dublin",
    "EI10": "Galway", "EI11": "Kerry", "EI12": "Kildare", "EI13": "Kilkenny", "EI14": "Leitrim", "EI15": "Laois",
    "EI16": "Limerick", "EI18": "Longford", "EI19": "Louth", "EI20": "Mayo", "EI21": "Meath", "EI22": "Monaghan",
    "EI23": "Offaly", "EI24": "Roscommon", "EI25": "Sligo", "EI26": "Tipperary", "EI27": "Waterford",
    "EI29": "Westmeath", "EI30": "Wexford", "EI31": "Wicklow",
}


def _t(s) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse(raw: bytes) -> list[Warning]:
    data = json.loads(raw)
    items = data if isinstance(data, list) else data.get("warnings", []) if isinstance(data, dict) else []
    out = []
    for w in items:
        level = str(w.get("level") or w.get("colour") or w.get("severity") or "").lower()
        regions = w.get("regions") or w.get("counties") or []
        if isinstance(regions, str):
            regions = [r.strip() for r in regions.split(",") if r.strip()]
        out.append(Warning(
            id=str(w.get("capId") or w.get("id") or f"{w.get('type')}-{w.get('onset')}"), source=KEY, level=level,
            type=str(w.get("type") or w.get("event") or "").strip(), headline=str(w.get("headline") or "").strip(),
            description=str(w.get("description") or "").strip(), regions=[COUNTY_CODES.get(r, r) for r in regions],
            onset=_t(w.get("onset")), expiry=_t(w.get("expiry") or w.get("expires")), issued=_t(w.get("issued")),
            updated=_t(w.get("updated")), status=w.get("status"), raw=w))
    return out


CAP = "{urn:oasis:names:tc:emergency:cap:1.2}"
LEVELS = {"moderate": "yellow", "severe": "orange", "extreme": "red", "minor": "yellow"}


def rss_links(raw: bytes) -> list[str]:
    """CAP file links from the warnings RSS feed. An empty list means the feed was read and no warning is in force."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(raw)
    if root.tag != "rss":
        raise ValueError(f"unexpected warnings feed root element: {root.tag}")
    return [(i.findtext("link") or "").strip() for i in root.iter("item") if (i.findtext("link") or "").strip()]


def parse_cap(raw: bytes) -> list[Warning]:
    import xml.etree.ElementTree as ET
    a = ET.fromstring(raw)
    if a.tag != CAP + "alert":
        raise ValueError(f"unexpected CAP root element: {a.tag}")
    ident, sent, status = (a.findtext(CAP + "identifier") or "").strip(), a.findtext(CAP + "sent"), a.findtext(CAP + "status")
    out = []
    for info in a.findall(CAP + "info"):
        if (info.findtext(CAP + "language") or "en").lower()[:2] != "en":
            continue
        params = {(p.findtext(CAP + "valueName") or ""): (p.findtext(CAP + "value") or "") for p in info.findall(CAP + "parameter")}
        level = next((x.strip().lower() for x in params.get("awareness_level", "").split(";") if x.strip().lower() in ("yellow", "orange", "red")), "") or LEVELS.get((info.findtext(CAP + "severity") or "").lower(), "")
        areas = [x for ar in info.findall(CAP + "area") for x in [(ar.findtext(CAP + "areaDesc") or "").strip()] if x]
        out.append(Warning(id=ident, source=KEY, level=level, type=(info.findtext(CAP + "event") or "").strip(), headline=(info.findtext(CAP + "headline") or "").strip(),
                           description=(info.findtext(CAP + "description") or "").strip(), regions=areas, onset=_t(info.findtext(CAP + "onset")), expiry=_t(info.findtext(CAP + "expires")),
                           issued=_t(sent), updated=_t(info.findtext(CAP + "effective") or sent), status=status, raw={"identifier": ident, "parameters": params}))
    return out


def fetch(client: httpx.Client) -> Fetch:
    """Read the RSS feed, then each CAP file it lists. Any failure raises, so an unreadable feed is never reported as "no warnings"."""
    r = get(client, URL)
    links = rss_links(r.content)
    body, records = bytearray(r.content), []
    for link in links:
        c = get(client, link)
        body += c.content
        records += parse_cap(c.content)
    prov = Provenance(source=KEY, publisher=PUBLISHER, url=URL, licence=LICENCE, retrieved_at=utcnow(), sha256=sha256(bytes(body)), bytes=len(body))
    return Fetch(records=records, provenance=prov)
