"""Raw mirrors: exact copies of public query results that a site parses with its own rules.

DCWatch classifies planning filings itself (what counts as a data centre, site grouping), so
the hub does not interpret them - it runs the same queries DCWatch used to run, combines the
pages into one JSON document, and keeps every distinct version with its SHA-256. DCWatch then
reads https://data.solas33.com/v1/mirror/<key>/latest.json instead of hitting the source.

Combined document shape (ArcGIS): {"key", "source_url", "requests": [urls], "retrieved_at",
"features": [...], "fetch_ids": [...]}. CSV mirrors keep the source text unchanged.
"""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from s33weather.models import sha256

from ..db import utcnow
from ..models import Mirror
from .base import Collector, Result, add_event, get_with_retry, log_fetch

DC_LIKE = "LIKE '%data cent%' OR {f} LIKE '%data-cent%' OR {f} LIKE '%datacent%'"


def _where(field: str) -> str:
    return f"{field} " + DC_LIKE.format(f=field)


def store_mirror(session: Session, key: str, body: bytes, ext: str, records: int, fetch_ids: list[int], url: str, name: str) -> Result:
    digest = sha256(body)
    m = session.scalar(select(Mirror).where(Mirror.key == key))
    now = utcnow()
    res = Result(fetched=records)
    if m is None:
        m = Mirror(key=key, sha256=digest, bytes=len(body), records=records, ext=ext, retrieved_at=now, changed_at=now, fetch_ids=fetch_ids, body=body)
        session.add(m)
        res.created = 1
    else:
        m.retrieved_at, m.fetch_ids = now, fetch_ids
        if m.sha256 != digest:
            m.sha256, m.bytes, m.records, m.body, m.changed_at = digest, len(body), records, body, now
            res.changed = 1
            add_event(session, "mirror_changed", f"{name}: source data changed ({records} records)", key=f"mirror:{key}:{digest[:16]}",
                      source_url=url, source=key)
    m.versioned_path = f"v1/mirror/{key}/{m.changed_at:%Y-%m-%d}-{m.sha256[:12]}.{ext}"
    res.message = f"{records} records, {'changed' if res.changed else 'new' if res.created else 'unchanged'} (sha {digest[:12]})"
    return res


class ArcGisMirror(Collector):
    api = ""
    params: dict = {}
    page = 2000
    interval_hours = 24.0

    def run(self, session: Session, client) -> Result:
        feats, urls, fids, offset = [], [], [], 0
        while True:
            r = get_with_retry(client, self.api, params=dict(self.params, resultOffset=offset, resultRecordCount=self.page, f="json"))
            fl = log_fetch(session, self.key, str(r.url), sha256(r.content), len(r.content), self.licence, note=f"page offset {offset}")
            fids.append(fl.id)
            urls.append(str(r.url))
            body = r.json()
            if "error" in body:
                raise RuntimeError(f"ArcGIS error: {body['error']}")
            page = body.get("features", [])
            feats += page
            if not body.get("exceededTransferLimit") or not page:
                break
            offset += len(page)
        doc = {"key": self.key, "source_url": self.url, "api": self.api, "requests": urls, "retrieved_at": utcnow().isoformat() + "Z",
               "fetch_ids": fids, "features": feats}
        # canonical bytes (sorted keys, no volatile fields) so an unchanged source gives an unchanged hash
        stable = json.dumps({"api": self.api, "features": feats}, sort_keys=True, separators=(",", ":")).encode()
        res = store_mirror(session, self.key, stable, "json", len(feats), fids, self.url, self.name)
        m = session.scalar(select(Mirror).where(Mirror.key == self.key))
        m.body = json.dumps(doc, separators=(",", ":")).encode()
        return res


class PlanningDataCentres(ArcGisMirror):
    key = "planning_npad_dc"
    name = "Irish Planning Applications - filings mentioning a data centre"
    publisher = "Department of Housing, Local Government and Heritage via data.gov.ie"
    url = "https://data.gov.ie/dataset/irishplanningapplications2"
    api = "https://services.arcgis.com/NzlPQPKn5QF9v2US/arcgis/rest/services/IrishPlanningApplications/FeatureServer/0/query"
    licence = "Creative Commons Attribution (see dataset page)"
    provides = "Raw ArcGIS features (attributes + point geometry) for every council planning filing whose description mentions a data centre."
    used_by = ("dcwatch",)
    params = dict(where=_where("DevelopmentDescription"),
                  outFields=("PlanningAuthority,ApplicationNumber,DevelopmentDescription,DevelopmentAddress,ApplicationStatus,ApplicationType,"
                             "ApplicantForename,ApplicantSurname,Decision,FloorArea,ReceivedDate,WithdrawnDate,DecisionDate,LinkAppDetails,"
                             "AppealDecision,GrantDate,ExpiryDate"),
                  returnGeometry="true", outSR=4326, orderByFields="OBJECTID")


class AcpDataCentres(ArcGisMirror):
    key = "acp_cases_dc"
    name = "An Coimisiún Pleanála cases - mentioning a data centre"
    publisher = "An Coimisiún Pleanála via data.gov.ie"
    url = "https://data.gov.ie/dataset/cases-2016-onwards-received-or-decided-by-an-bord-pleanala-on-or-after-1st-january-2016"
    api = "https://services-eu1.arcgis.com/o56BSnENmD5mYs3j/arcgis/rest/services/Cases_2016_Onwards/FeatureServer/3/query"
    licence = "Creative Commons Attribution (see dataset page)"
    provides = "Raw ArcGIS features (attributes + centroid) for appeals, SID cases and referrals mentioning a data centre."
    used_by = ("dcwatch",)
    params = dict(where=_where("DEVDESC"), outFields="ABPCASEID,DEVDESC,DEVADDRESS,LODGEDON,DECISION,DECIDED_ON,LINKABPWEB,PLANINGATY,CATEGORY",
                  returnGeometry="false", returnCentroid="true", outSR=4326, orderByFields="OBJECTID")


class CsoMec02(Collector):
    key = "cso_mec02"
    name = "CSO - Data Centres Metered Electricity Consumption (MEC02)"
    publisher = "Central Statistics Office (CSO)"
    url = "https://data.cso.ie/table/MEC02"
    api = "https://ws.cso.ie/public/api.restful/PxStat.Data.Cube_API.ReadDataset/MEC02/CSV/1.0/en"
    licence = "CC BY 4.0"
    provides = "Quarterly metered electricity (GWh): data centres vs all other customers, 2015 onward - the CSV exactly as published."
    used_by = ("dcwatch",)
    interval_hours = 24.0

    def run(self, session: Session, client) -> Result:
        r = get_with_retry(client, self.api)
        fl = log_fetch(session, self.key, self.api, sha256(r.content), len(r.content), self.licence)
        text = r.content.decode("utf-8-sig")
        if "VALUE" not in text.splitlines()[0]:
            raise ValueError("unexpected CSO CSV shape")
        return store_mirror(session, self.key, r.content, "csv", max(0, len(text.splitlines()) - 1), [fl.id], self.url, self.name)
