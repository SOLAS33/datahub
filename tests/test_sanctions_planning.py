"""EU sanctions (enterprises only), the planning pipeline summary, and the sanctions name-match flag in the company risk rules."""
from __future__ import annotations

from datetime import date

from s33hub import risk
from s33hub.collectors.governance import normalise_sanctions
from s33hub.collectors.property import MAJOR_UNITS, outcome, summarise_planning

HEAD = "fileGenerationDate;Entity_LogicalId;Entity_DesignationDate;Entity_SubjectType_ClassificationCode;Entity_Regulation_Programme;Entity_Regulation_PublicationUrl;NameAlias_WholeName;Address_CountryIso2Code\n"


def test_sanctions_keeps_enterprises_and_all_their_names_but_no_individuals():
    csv_text = HEAD + "\n".join([
        "x;1;2022-03-15;enterprise;UKR;https://eur-lex.example/1;Volga Trading LLC;RU",
        "x;1;2022-03-15;enterprise;UKR;https://eur-lex.example/1;VOLGA TRADE;",
        "x;1;2022-03-15;enterprise;UKR;https://eur-lex.example/1;Volga Trading LLC;RU",     # duplicate row for another address
        "x;2;2020-01-01;person;TERR;https://eur-lex.example/2;John Smith;IE",
        "x;3;2019-05-05;enterprise;SYR;https://eur-lex.example/3;;SY",                      # no name: nothing to match
    ]) + "\n"
    rows = normalise_sanctions(csv_text)
    assert {r[1] for r in rows} == {"Volga Trading LLC", "VOLGA TRADE"}
    assert all(r[0] == "1" and r[2] == "2022-03-15" and r[3] == "UKR" and r[4] == "RU" for r in rows)
    assert not any("Smith" in r[1] for r in rows)


def feat(**a):
    base = dict(PlanningAuthority="Cork City Council", ApplicationNumber="A/1", DevelopmentDescription="House extension", ApplicationStatus="APPLICATION FINALISED",
                ApplicationType="PERMISSION", Decision="CONDITIONAL", NumResidentialUnits=1, FloorArea=40, AreaofSite=0.1, ReceivedDate=1735689600000,   # 2025-01-01
                DecisionDate=1740000000000, LinkAppDetails="https://x/1", DevelopmentPostcode="T12 AB34")
    base.update(a)
    return {"attributes": base}


def test_outcomes():
    assert outcome("CONDITIONAL", "x") == "granted" and outcome("REFUSED", "") == "refused" and outcome("", "WITHDRAWN") == "withdrawn" and outcome("", "") == "other"


def test_planning_summary_counts_and_lists_only_major_applications_without_addresses():
    feats = [feat(), feat(ApplicationNumber="A/2", Decision="REFUSED"),
             feat(ApplicationNumber="A/3", DevelopmentDescription="  Large  residential\n scheme ", NumResidentialUnits=MAJOR_UNITS + 10, FloorArea=9000, DevelopmentAddress="12 Real Street"),
             feat(ReceivedDate=None)]
    monthly, major = summarise_planning(feats)
    assert monthly == [["2025-01", "Cork City Council", 3, 2, 1, 0, 62, 61, 9080]]
    assert len(major) == 1 and major[0][1] == "A/3" and major[0][11] == "Large residential scheme" and major[0][10] == "T12"
    assert "Real Street" not in " ".join(str(x) for x in major[0])


def rec(**k):
    base = dict(n="1", nm="VOLGA TRADING LIMITED", st="Normal", sg="L", ty="LTD", rg="2015-03-01", ds="", sd="", ar="2026-03-01", ac="2025-06-30", nc="4690", ea="D02", ne="")
    base.update(k)
    return base


def test_sanctions_flag_is_amber_worded_as_name_match_only():
    sx = {"id": "1", "n": "Volga Trading", "p": "UKR", "d": "2022-03-15"}
    flags, level = risk.assess(rec(), date(2026, 10, 6), [("2025-12-01", "2025-06-30")], None, sx)
    f = [x for x in flags if x[0] == "SANCTIONS_NAME"]
    assert f and f[0][1] == "amber" and level == "amber"
    assert "not an identification" in f[0][2] and "2022-03-15" in f[0][2]


def test_no_sanctions_flag_without_a_match_or_for_a_dissolved_company():
    assert not [x for x in risk.assess(rec(), date(2026, 10, 6), [("2025-12-01", "2025-06-30")], None, None)[0] if x[0] == "SANCTIONS_NAME"]
    sx = {"id": "1", "n": "Volga Trading", "p": "UKR", "d": ""}
    flags, level = risk.assess(rec(sg="D", st="Dissolved"), date(2026, 10, 6), None, None, sx)
    assert not [x for x in flags if x[0] == "SANCTIONS_NAME"] and level == "grey"


def test_build_records_attaches_the_match():
    rows = [["1", "VOLGA TRADING LIMITED", "Normal", "1", "LTD", "1", "2015-03-01", "", "", "2026-03-01", "2025-06-30", "4690", "D02", "", ""]]
    recs = risk.build_records(rows, {"1": [("2025-12-01", "2025-06-30")]}, {}, date(2026, 10, 6), {"1": {"id": "9", "n": "Volga Trading", "p": "UKR", "d": "2022-03-15"}})
    assert recs["1"]["sx"]["id"] == "9" and recs["1"]["lv"] == "amber"
