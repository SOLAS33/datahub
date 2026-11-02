"""Hub: workbook regional-period mapping, mirrors, publishing (dry run) on an in-memory database."""
from __future__ import annotations

import os

os.environ.setdefault("HUB_DATABASE_URL", "sqlite://")

import json  # noqa: E402

from s33hub.collectors.eirgrid import to_utc  # noqa: E402
from s33hub.collectors.mirrors import store_mirror  # noqa: E402
from s33hub.db import SessionLocal, init_db  # noqa: E402
from s33hub.models import HubEvent, Mirror  # noqa: E402
from s33hub.publish import Uploader, build, catalog  # noqa: E402


class FakeWS:
    title = "Regional Wind & Solar"

    def __init__(self, rows):
        self.rows = rows

    def iter_rows(self, values_only=True):
        return iter(self.rows)


def test_regional_periods_dated_by_closing_column():
    from s33hub.collectors.eirgrid_dd import parse_regional
    hdr_years = ["Controllable Wind \nBy Region", None, 2020, None, None, None, None, "2021 (Upgraded to monthly reporting)"] + [None] * 4 + [None]
    hdr = [None, "Cap (MW)", "Qtr1", "Qtr2", "Qtr3", "Qtr4", 2020, "Jan", "Feb", "Mar", "Qtr1", 2021, "Technology"]
    row = ["SW", 1500, 0.01, 0.02, 0.03, 0.04, 0.025, 0.05, 0.06, 0.07, 0.06, 0.08, None]
    farm = [None] * 12 + [None]
    stats, farms = parse_regional(FakeWS([["title"], hdr_years, hdr, row, farm]))
    got = {(s["period"], s["metric"]): round(s["value"], 3) for s in stats}
    assert got[("2020Q4", "dd_pct")] == 4.0 and got[("2020", "dd_pct")] == 2.5
    assert got[("2021-01", "dd_pct")] == 5.0 and got[("2021Q1", "dd_pct")] == 6.0 and got[("2021", "dd_pct")] == 8.0


def test_to_utc():
    assert to_utc("04-Oct-2026 11:45:00").hour == 10


def test_mirror_versions_and_events():
    init_db()
    s = SessionLocal()
    r1 = store_mirror(s, "k", b'{"a":1}', "json", 1, [1], "u", "K")
    s.commit()
    r2 = store_mirror(s, "k", b'{"a":1}', "json", 1, [2], "u", "K")
    s.commit()
    r3 = store_mirror(s, "k", b'{"a":2}', "json", 1, [3], "u", "K")
    s.commit()
    assert (r1.created, r2.changed, r3.changed) == (1, 0, 1)
    assert s.query(HubEvent).filter_by(kind="mirror_changed").count() == 1
    m = s.query(Mirror).filter_by(key="k").one()
    assert m.versioned_path.startswith("v1/mirror/k/") and m.versioned_path.endswith(".json")


def test_publish_build_dry_run():
    init_db()
    s = SessionLocal()
    files = build(s)
    assert "v1/weather/points.csv" in files and "v1/status.json" in files
    st = json.loads(files["v1/status.json"]["data"])
    assert {x["key"] for x in st["sources"]} >= {"eirgrid_live", "planning_npad_dc", "cso_mec02"}
    assert json.loads(catalog([], files))["files"] == []
    Uploader(dry_run=True).put("x", b"", "text/plain", "no-store")


def _hub_client(monkeypatch, codes):
    import httpx

    from s33hub import client
    pauses, calls = [], []
    monkeypatch.setattr(client.time, "sleep", pauses.append)

    def fake_get(url, **_):
        calls.append(url)
        return httpx.Response(codes[len(calls) - 1], request=httpx.Request("GET", url))
    monkeypatch.setattr(client.httpx, "get", fake_get)
    return client, pauses, calls


def test_client_waits_out_a_burst_of_504s(monkeypatch):
    client, pauses, calls = _hub_client(monkeypatch, [504, 504, 200])
    assert client._get("https://x/y").status_code == 200
    assert len(calls) == 3 and pauses == [0, 10, 20]


def test_client_does_not_retry_a_missing_file(monkeypatch):
    import httpx
    import pytest
    client, _, calls = _hub_client(monkeypatch, [404])
    with pytest.raises(httpx.HTTPStatusError):
        client._get("https://x/y")
    assert len(calls) == 1


def test_client_gives_up_after_the_last_pause(monkeypatch):
    import httpx
    import pytest
    client, pauses, calls = _hub_client(monkeypatch, [504] * 4)
    with pytest.raises(httpx.HTTPStatusError):
        client._get("https://x/y")
    assert len(calls) == 4 and sum(pauses) == 70
