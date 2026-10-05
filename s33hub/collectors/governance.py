"""Oireachtas open data: current members of the Dáil and Seanad, and every bill, as tables.

API: https://api.oireachtas.ie/v1/ (open, no key). Published under the Oireachtas open data licence (CC BY 4.0 compatible).
"""
from __future__ import annotations

import re

import httpx
from sqlalchemy.orm import Session

from s33weather.models import sha256

from .base import Collector, Result, get_with_retry, log_fetch
from .tables import csv_bytes, gz, load_state, publish_table, save_state

API = "https://api.oireachtas.ie/v1"
HOUSES = (("dail", 34), ("seanad", 27))


def _txt(s) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def parse_member(m: dict, house: str) -> list:
    m = m.get("member", m)
    mem = (m.get("memberships") or [{}])[-1].get("membership", {})
    party = ((mem.get("parties") or [{}])[0].get("party") or {}).get("showAs", "")
    rep = ((mem.get("represents") or [{}])[0].get("represent") or {}).get("showAs", "")
    dr = mem.get("dateRange") or {}
    return [m.get("memberCode"), m.get("fullName"), house, (mem.get("house") or {}).get("showAs", ""), party, rep, dr.get("start") or "", dr.get("end") or "", m.get("uri")]


def parse_bill(b: dict) -> list:
    b = b.get("bill", b)
    spons = [((s.get("sponsor") or {}).get("as") or {}).get("showAs") or ((s.get("sponsor") or {}).get("by") or {}).get("showAs") or "" for s in b.get("sponsors") or []]
    stage = ((b.get("mostRecentStage") or {}).get("event") or {}).get("showAs", "")
    return [b.get("billYear"), b.get("billNo"), b.get("billType"), _txt(b.get("shortTitleEn")), b.get("status"), (b.get("originHouse") or {}).get("showAs", ""), stage,
            "; ".join(s for s in spons if s)[:200], (b.get("lastUpdated") or "")[:10], b.get("uri")]


class Oireachtas(Collector):
    key = "oireachtas"
    name = "Oireachtas open data: members and bills"
    publisher = "Houses of the Oireachtas"
    url = "https://data.oireachtas.ie/"
    licence = "Oireachtas open data licence (compatible with CC BY 4.0)"
    provides = "Current members of the Dáil and Seanad (party, constituency) and every bill (title, status, stage, sponsor, origin house)."
    interval_hours = 24.0
    domain, tier = "governance", "files"
    rights = "check"        # confirm the Oireachtas open data licence wording before any paid use

    def pages(self, client: httpx.Client, path: str, params: dict, size: int = 500) -> list[dict]:
        out, skip = [], 0
        while True:
            r = get_with_retry(client, f"{API}/{path}", params=dict(params, limit=size, skip=skip), timeout=180).json()
            res = r.get("results", [])
            out += res
            skip += size
            if len(res) < size or skip >= (r.get("head", {}).get("counts", {}).get("resultCount") or 0):
                return out

    def run(self, session: Session, client: httpx.Client) -> Result:
        state = load_state(session, self.key)
        items = state.setdefault("items", {})
        members = []
        for house, no in HOUSES:
            for m in self.pages(client, "members", {"house_no": no, "chamber": house}):
                members.append(parse_member(m, house))
        bills = [parse_bill(b) for b in self.pages(client, "legislation", {})]
        if len(members) < 100 or len(bills) < 500:
            raise ValueError(f"Oireachtas feeds look wrong: {len(members)} members, {len(bills)} bills")
        log_fetch(session, self.key, f"{API}/members + {API}/legislation", sha256(repr((members, bills)).encode()), len(members) + len(bills), self.licence)
        publish_table(items, "members", "v1/governance/oireachtas_members.csv.gz",
                      gz(csv_bytes(["member_code", "name", "house", "house_term", "party", "represents", "from", "to", "uri"], sorted(members, key=lambda r: (r[2], r[1] or "")))),
                      "application/gzip", len(members), "Current members of the Dáil and Seanad", self.key)
        publish_table(items, "bills", "v1/governance/oireachtas_bills.csv.gz",
                      gz(csv_bytes(["year", "number", "type", "short_title", "status", "origin_house", "most_recent_stage", "sponsors", "last_updated", "uri"], sorted(bills, key=lambda r: (str(r[0]), str(r[1]))))),
                      "application/gzip", len(bills), "Every bill before the Oireachtas, with status and stage", self.key)
        res = save_state(session, self.key, state, self.url, self.name)
        res.fetched, res.message = len(members) + len(bills), f"{len(members)} members, {len(bills)} bills"
        return res
