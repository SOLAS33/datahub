"""Company risk rules (1.1): each flag fires for the stated reason and not otherwise; shards are stable; names normalise as agreed."""
from __future__ import annotations

import gzip
import json
from datetime import date

from s33hub import risk
from s33hub.names import NOT_A_COMPANY, clean_name, norm_name, search_key

TODAY = date(2026, 10, 6)


def co(**kw):
    base = dict(n="100", nm="ACME LIMITED", st="Normal", sg="L", ty="LTD", rg="2015-03-01", ds="", sd="2026-01-01", ar="2026-03-01", ac="2025-06-30", nc="4120", ea="D02", ne="")
    base.update(kw)
    return base


def codes(c, **kw):
    flags, level = risk.assess(c, TODAY, **kw)
    return {f[0] for f in flags}, level


def test_a_healthy_company_has_no_flags():
    assert codes(co()) == (set(), "green")


def test_status_groups():
    assert [risk.status_group(s) for s in ("Normal", "Liquidation", "Administration (UK)", "Examinership", "Strike Off Listed", "Dissolved", "Ceased IRL", "Struck Off", "Deleted CB Merger")] == list("LIIISDDDD")


def test_red_for_insolvency_and_strike_off_grey_for_dissolved():
    assert codes(co(st="Liquidation", sg="I")) == ({"INSOLVENCY"}, "red")
    assert codes(co(st="Strike Off Listed", sg="S")) == ({"STRIKE_OFF"}, "red")
    assert codes(co(st="Dissolved", sg="D", ar="", ac="")) == ({"NOT_LIVE"}, "grey")      # no filing flags on a company that no longer exists


def test_annual_return_rules_and_the_grace_period():
    assert codes(co(ar="2025-10-07")) == (set(), "green")                                   # 12 months old: normal
    assert codes(co(ar="2025-06-01")) == ({"AR_OVERDUE"}, "amber")                          # over 16 months
    assert codes(co(ar="")) == ({"AR_NONE"}, "amber")
    assert "AR_NONE" not in codes(co(ar="", rg="2026-01-01"))[0]                            # too young for the rule


def test_accounts_rules_use_a_realistic_threshold():
    assert codes(co(ac="2025-01-31")) == (set(), "green")                                   # 20 months: typical, not flagged
    assert codes(co(ac="2023-12-31")) == ({"ACCOUNTS_STALE"}, "amber")                      # over 30 months
    assert codes(co(ac=""))[0] == {"ACCOUNTS_NONE"}


def test_late_filing_needs_two_in_a_row():
    late = [("2025-12-01", "2024-12-31"), ("2024-12-01", "2023-12-31")]          # received ~11 months after period end, twice
    assert "LATE_FILER" in codes(co(), filings=late)[0]
    one_late = [("2025-02-01", "2024-12-31"), ("2024-12-01", "2023-12-31")]      # latest on time
    assert "LATE_FILER" not in codes(co(), filings=one_late)[0]
    assert "LATE_FILER" not in codes(co(), filings=late[:1])[0]


def test_new_company_and_new_and_large_contract_winner():
    young = co(rg="2026-04-01", ar="", ac="")
    assert codes(young) == ({"NEW"}, "green")                                              # information only
    assert codes(young, contracts={"w": 2, "v": 400000.0, "fy": "2026", "ly": "2026"}) == ({"NEW", "NEW_AND_LARGE"}, "amber")
    assert "NEW_AND_LARGE" not in codes(co(), contracts={"w": 5, "v": 9e6})[0]               # an established company is not flagged for winning


def test_name_change_is_information_not_alarm():
    assert codes(co(ne="2026-05-01")) == ({"NAME_CHANGED"}, "green")
    assert codes(co(ne="2015-03-01"))[0] == set()                                           # the effective date of the original name is not a change


def test_records_shards_and_search():
    rows = [["100", "ACME LIMITED", "Normal", "1", "LTD", "1", "2015-03-01", "", "2026-01-01", "2026-03-01", "2025-06-30", "4120", "D02", "", ""],
            ["1500", "ALPHA BUILD LIMITED", "Strike Off Listed", "1", "LTD", "1", "2015-03-01", "", "2026-08-01", "2025-03-01", "2024-06-30", "4120", "", "", ""]]
    recs = risk.build_records(rows, {"100": [("2025-12-01", "2024-12-31")]}, {"1500": {"w": 3, "v": 1.0}}, TODAY)
    assert recs["100"]["lv"] == "green" and recs["1500"]["lv"] == "red" and recs["1500"]["pc"]["w"] == 3
    shards = risk.company_shards(recs)
    assert set(shards) == {"v1/business/risk/company/0.json.gz", "v1/business/risk/company/1.json.gz"}      # numbers 100 -> shard 0, 1500 -> shard 1
    body = json.loads(gzip.decompress(shards["v1/business/risk/company/1.json.gz"]))
    assert body["v"] == risk.RULES_VERSION and body["c"]["1500"]["nm"] == "ALPHA BUILD LIMITED" and "generated" not in json.dumps(body)
    assert risk.company_shards(recs) == shards                                                                # same input, same bytes: unchanged shards are not re-uploaded
    srch = risk.search_shards(recs)
    assert "v1/business/risk/search/ac.json.gz" in srch and json.loads(gzip.decompress(srch["v1/business/risk/search/al.json.gz"]))[0][:2] == ["1500", "ALPHA BUILD LIMITED"]
    assert json.loads(srch["v1/business/risk/search/index.json"])["split"] == []


def test_aggregates_cover_sectors_strike_off_and_events():
    rows = [["1", "A LIMITED", "Normal", "1", "LTD", "1", "2015-03-01", "", "2026-01-01", "2024-01-01", "2025-06-30", "4120", "", "", ""],
            ["2", "B LIMITED", "Strike Off Listed", "1", "LTD", "1", "2015-03-01", "", "2026-08-01", "", "", "4120", "D01", "", ""],
            ["3", "C LIMITED", "Liquidation", "1", "LTD", "1", "2015-03-01", "", "2026-09-01", "", "", "6420", "", "", ""]]
    agg = risk.aggregates(risk.build_records(rows, {}, {}, TODAY), TODAY)
    sector = agg["v1/business/risk/sector_filing.csv"][0].decode().splitlines()
    assert sector[0].startswith("nace_division,live_companies") and "41,1,1,0,0,0,0,1" in sector                  # one live company with an overdue annual return, one struck-off listed
    assert "64,0,0,0,0,0,1,0" in sector
    assert gzip.decompress(agg["v1/business/risk/strike_off_list.csv.gz"][0]).decode().splitlines()[1].startswith("2,B LIMITED,2026-08-01")
    assert b"2026-09" in gzip.decompress(agg["v1/business/risk/status_events_monthly.csv.gz"][0])


def test_names():
    assert norm_name("Sisk Ltd.") == norm_name("SISK LIMITED") and norm_name("Alpha Build") != norm_name("Alpha Builders")
    assert clean_name("An Garda Siochana_1192") == "An Garda Siochana" and clean_name("Route 66") == "Route 66"
    assert NOT_A_COMPANY.search("Deloitte Ireland LLP") and not NOT_A_COMPANY.search("Alpha Build Ltd") and not NOT_A_COMPANY.search("Philips Electronics")
    assert search_key("O'Brien & Sons (Cork) Ltd.") == "obriensonscorkltd"
