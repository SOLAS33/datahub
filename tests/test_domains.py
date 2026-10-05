"""Domains and the new sources: collector contract, per-domain publishing, and each source's parser against realistic samples."""
from __future__ import annotations

import io
import json
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path

os.environ.setdefault("HUB_DATABASE_URL", "sqlite://")

import pytest  # noqa: E402

from s33hub import artifacts  # noqa: E402
from s33hub.collectors import CORE, DOMAINS, REGISTRY, Collector  # noqa: E402
from s33hub.collectors.energy_intl import parse_generation, parse_intensity  # noqa: E402
from s33hub.collectors.environment import parse_erddap, parse_opw  # noqa: E402
from s33hub.collectors.governance import parse_bill, parse_member  # noqa: E402
from s33hub.collectors.property import normalise_ppr  # noqa: E402
from s33hub.collectors.statistics import jsonstat_rows  # noqa: E402
from s33hub.collectors.tables import add_daily, gz, publish_table  # noqa: E402
from s33hub.db import SessionLocal, init_db  # noqa: E402
from s33hub.models import DailyStat  # noqa: E402
from s33hub.publish import DiskUploader, Uploader, publish  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ the registry contract: what every source must declare

def test_every_collector_declares_what_the_catalogue_needs():
    keys = [c.key for c in REGISTRY.values()]
    assert len(keys) == len(set(keys))
    for k, c in REGISTRY.items():
        assert k == c.key and c.name and c.publisher and c.licence and c.provides and c.url.startswith("http"), k
        assert c.domain == CORE or re.fullmatch(r"[a-z_]+", c.domain), k
        assert c.tier in ("rows", "files", "mirror") and c.rights in ("open", "check") and c.interval_hours > 0, k
        assert isinstance(c, Collector)


def test_ci_matrix_runs_every_domain():
    wf = (ROOT / ".github" / "workflows" / "hub-domains.yml").read_text(encoding="utf-8")
    listed = set(re.findall(r"^\s+- ([a-z_]+)\s*$", wf.split("matrix:")[1].split("concurrency:")[0], re.M))
    assert listed == set(DOMAINS), (listed ^ set(DOMAINS))


def test_core_keeps_the_original_sources_and_nothing_new():
    core = {k for k, c in REGISTRY.items() if c.domain == CORE}
    assert core == {"eirgrid_live", "eirgrid_dd", "weather_forecasts", "weather_obs", "weather_warnings", "planning_npad_dc", "acp_cases_dc", "cso_mec02"}


# ------------------------------------------------------------------ publishing a domain

class FakeSource(Collector):
    key, name, publisher, licence, provides, url = "fake_src", "Fake", "Nobody", "CC BY 4.0", "Test data", "https://example.org"
    domain, tier = "testdom", "files"

    def run(self, session, client):  # pragma: no cover - not used
        raise NotImplementedError


def test_domain_publish_writes_files_catalogue_status_and_skips_unchanged(tmp_path, monkeypatch):
    monkeypatch.setitem(REGISTRY, "fake_src", FakeSource())
    init_db()
    artifacts.clear()
    items: dict = {}
    assert publish_table(items, "a", "v1/test/a.csv.gz", gz(b"x,y\n1,2\n"), "application/gzip", 1, "A table", "fake_src")
    assert not publish_table(items, "a", "v1/test/a.csv.gz", gz(b"x,y\n1,2\n"), "application/gzip", 1, "A table", "fake_src")   # same bytes: nothing to publish
    with SessionLocal() as s:
        info = publish(s, DiskUploader(tmp_path), domain="testdom")
        assert info["uploaded"] >= 3 and info["snapshot_bytes"] == 0                  # a domain publishes files, never a snapshot
        assert (tmp_path / "v1/test/a.csv.gz").exists()
        cat = json.loads((tmp_path / "v1/catalog/testdom.json").read_text())
        assert cat["domain"] == "testdom" and any(f["path"] == "v1/test/a.csv.gz" and f["publisher"] == "Nobody" for f in cat["files"])
        st = json.loads((tmp_path / "v1/status/testdom.json").read_text())
        assert [x["key"] for x in st["sources"]] == ["fake_src"] and st["sources"][0]["tier"] == "files"
        artifacts.clear()
        again = publish(s, DiskUploader(tmp_path), domain="testdom")
        assert again["uploaded"] == 0 or again["uploaded"] < info["uploaded"]          # unchanged files are not uploaded again
    artifacts.clear()


def test_put_many_attempts_every_upload_then_raises(monkeypatch):
    calls = []

    class Flaky(Uploader):
        def put(self, path, data, ctype, cache):
            calls.append(path)
            if path.endswith("bad"):
                raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        Flaky(workers=3).put_many([("a", b"", "t", "c"), ("bad", b"", "t", "c"), ("c", b"", "t", "c")])
    assert sorted(calls) == ["a", "bad", "c"]


def test_daily_aggregates_are_idempotent():
    init_db()
    with SessionLocal() as s:
        t1, t2 = datetime(2026, 10, 5, 10, 0), datetime(2026, 10, 5, 11, 0)
        assert add_daily(s, "ds", "st", "v", t1, 2.0) and add_daily(s, "ds", "st", "v", t2, 6.0)
        assert not add_daily(s, "ds", "st", "v", t2, 99.0) and not add_daily(s, "ds", "st", "v", t1, 99.0)     # re-reading the same feed changes nothing
        r = s.query(DailyStat).filter_by(dataset="ds").one()
        assert (r.n, r.vmin, r.vmax, r.vsum) == (2, 2.0, 6.0, 8.0)
        s.rollback()


# ------------------------------------------------------------------ parsers

def test_opw_feed_keeps_valid_numeric_readings_and_stations():
    doc = {"features": [
        {"properties": {"station_ref": "0000001041", "station_name": "Sandy Mills", "sensor_ref": "0001", "region_id": 3, "datetime": "2026-10-05T14:30:00Z", "value": "0.329", "err_code": 99},
         "geometry": {"type": "Point", "coordinates": [-7.57, 54.83]}},
        {"properties": {"station_ref": "0000001041", "sensor_ref": "0002", "datetime": "2026-10-05T14:00:00Z", "value": "", "err_code": 99}, "geometry": {"coordinates": [-7.57, 54.83]}},
        {"properties": {"station_ref": "0000002", "sensor_ref": "0001", "datetime": "2026-10-05T14:00:00Z", "value": "1.0", "err_code": 31}, "geometry": {"coordinates": [0, 0]}}]}
    readings, stations = parse_opw(doc)
    assert [(r["station"], r["sensor"], r["value"]) for r in readings] == [("1041", "0001", 0.329)]      # blank value and flagged reading dropped
    assert stations == [["1041", "Sandy Mills", 3, 54.83, -7.57]]


def test_erddap_csv_skips_the_units_row():
    rows = parse_erddap("time,station_id,Water_Level_LAT\nUTC,,metres\n2026-10-05T10:00:00Z,Skerries,2.7\n2026-10-05T10:05:00Z,Skerries,NaN\n")
    assert len(rows) == 2 and rows[0]["station_id"] == "Skerries" and rows[1]["Water_Level_LAT"] == "NaN"


def test_jsonstat_flatten():
    doc = {"id": ["geo", "time"], "size": [2, 2], "dimension": {"geo": {"category": {"index": {"IE": 0, "EU": 1}}}, "time": {"category": {"index": ["2024", "2025"]}}},
           "value": {"0": 1.5, "1": 2.5, "3": 9.0}, "status": {"1": "p"}}
    cols, rows = jsonstat_rows(doc)
    assert cols == ["geo", "time", "value", "status"]
    assert rows == [["IE", "2024", 1.5, ""], ["IE", "2025", 2.5, "p"], ["EU", "2025", 9.0, ""]]


def test_property_register_drops_the_street_address_and_keeps_the_routing_key():
    text = ('Date of Sale (dd/mm/yyyy),Address,County,Eircode,Price (€),Not Full Market Price,VAT Exclusive,Description of Property,Property Size Description\n'
            '"01/03/2025","5 Braemor Drive, Churchtown","Dublin","D14 AB12","€343,000.00","No","Yes","Second-Hand Dwelling house /Apartment",""\n')
    rows = normalise_ppr(text)
    assert rows == [["2025-03-01", "Dublin", "D14", "343000", "0", "1", "Second-Hand Dwelling house /Apartment", ""]]
    assert "Braemor" not in repr(rows) and "AB12" not in repr(rows)
    with pytest.raises(ValueError):
        normalise_ppr("a,b,c\n1,2,3\n")


def test_oireachtas_parsers_tolerate_missing_fields():
    m = parse_member({"member": {"memberCode": "X.D.2024", "fullName": "Ann Example", "uri": "u", "memberships": [{"membership": {
        "house": {"showAs": "34th Dáil"}, "parties": [{"party": {"showAs": "Labour Party"}}], "represents": [{"represent": {"showAs": "Cork East"}}], "dateRange": {"start": "2024-11-29", "end": None}}}]}}, "dail")
    assert m[:7] == ["X.D.2024", "Ann Example", "dail", "34th Dáil", "Labour Party", "Cork East", "2024-11-29"]
    assert parse_member({"member": {"memberCode": "Y", "fullName": "No Memberships"}}, "seanad")[1] == "No Memberships"
    b = parse_bill({"bill": {"billYear": "2026", "billNo": "94", "billType": "Public", "shortTitleEn": "<p>Carriage <b>Bill</b></p>", "status": "Current", "originHouse": {"showAs": "Dáil Éireann"},
                             "mostRecentStage": {"event": {"showAs": "First Stage"}}, "sponsors": [{"sponsor": {"by": {"showAs": "Deputy A"}}}], "lastUpdated": "2026-10-02T11:02:39+00:00"}})
    assert b[:8] == ["2026", "94", "Public", "Carriage Bill", "Current", "Dáil Éireann", "First Stage", "Deputy A"] and b[8] == "2026-10-02"


def test_gb_carbon_parsers_skip_missing_actuals():
    ci = {"data": [{"from": "2026-10-03T00:00Z", "intensity": {"forecast": 105, "actual": 83}}, {"from": "2026-10-03T00:30Z", "intensity": {"forecast": 90, "actual": None}}]}
    assert parse_intensity(ci) == [(datetime(2026, 10, 3, 0, 0), 83.0)]
    gen = {"data": [{"from": "2026-10-03T00:00Z", "generationmix": [{"fuel": "wind", "perc": 48.4}, {"fuel": "coal", "perc": 0}]}]}
    assert parse_generation(gen) == [(datetime(2026, 10, 3, 0, 0), "wind", 48.4), (datetime(2026, 10, 3, 0, 0), "coal", 0.0)]


def test_gtfs_reference_tables_are_read_by_name():
    from s33hub.collectors.transport import KEEP, read_table
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("stops.txt", "stop_id,stop_name,stop_lat,stop_lon,extra\n1,Main St,53.3,-6.2,x\n")
    rows = read_table(zipfile.ZipFile(io.BytesIO(buf.getvalue())), "stops.txt", KEEP["stops.txt"])
    assert rows == [["1", "", "Main St", "53.3", "-6.2", "", "", ""]]
