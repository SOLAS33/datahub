"""Tenders and climate collectors: parsing both eTenders header layouts, TED hits, Met Éireann daily files, publish integration."""
from __future__ import annotations

import os

os.environ.setdefault("HUB_DATABASE_URL", "sqlite://")

from s33hub import artifacts  # noqa: E402
from s33hub.collectors.climate import parse_daily  # noqa: E402
from s33hub.collectors.tenders import normalise_etenders, parse_ted  # noqa: E402
from s33hub.db import SessionLocal, init_db  # noqa: E402
from s33hub.publish import build  # noqa: E402

OLD = ('Tender ID,Contracting Authority,Tender Name,Notice Published Date,Directive,Competition Type,Main Cpv Code,Main Cpv Code Description,'
       'Additional CPV Codes on CFT,Spend Category,Contract Type,Threshold Level,Procedure,Tender Submission Deadline,Evaluation Type,'
       'Notice Estimated Value,Contract Duration (Months),Cancelled Date,Award Published,Awarded Value,No of Bids Received,'
       'No of SMEs Bids Received,Awarded Suppliers,No of Awarded SMEs,TED Notice Link,TED CAN Link,Platform\n'
       '71011,Cork City Council,Kinsale Road Landfill,02/01/2013,NULL,Bespoke,45200000,Works.,452,Civils,Works,National,Restricted Procedure,'
       '08/02/2013,NULL,"1,500,000",24,NULL,31/05/2013,1200000,4,1,"  A Ltd ;B  Ltd",1,NULL,NULL,EUS Platform\n')
NEW = ('Tender ID,Parent Agreement ID,Contracting Authority,Name of Client Contracting Authority,Agreement Owner,Tender/Contract Name,'
       'Notice Published Date / Contract Created Date,Directive,Competition Type,Main Cpv Code,Main Cpv Code Description,Additional CPV Codes on CFT,'
       'Spend Category,Contract Type,Threshold Level,Procedure,Tender Submission Deadline,Evaluation Type,Sum of Notice Estimated Value (€),'
       'Sum of Contract Duration (Months),Cancelled Date,Award Published,Sum of Awarded Value (€),Sum of No of Bids Received,'
       'Sum of No of SMEs Bids Received,Awarded Suppliers,Sum of No of Awarded SMEs,TED Notice Link,TED CAN Link,Platform,Source\n'
       '8565893,1810904,ATU,,,VEX Equipment,,NULL,Bespoke,30200000,Equip.,x,Supplies,Supplies,National,DPS,,NULL,,,,21/07/2026,30000,,,Innovation First Trading Sarl,,,,'
       'ED Platform,OpenData-DPSTenders\n')


def test_etenders_old_and_new_layouts_normalise_alike():
    old = normalise_etenders(OLD)[0]
    assert old["tender_id"] == "71011" and old["published"] == "2013-01-02" and old["deadline"] == "2013-02-08"
    assert old["estimated_value_eur"] == "1500000" and old["awarded_value_eur"] == "1200000" and old["bids"] == "4"
    assert old["suppliers"] == "A Ltd; B Ltd" and old["directive"] == "" and old["cancelled"] == ""
    new = normalise_etenders(NEW)[0]
    assert new["tender_id"] == "8565893" and new["parent_id"] == "1810904" and new["awarded_value_eur"] == "30000"
    assert new["suppliers"] == "Innovation First Trading Sarl" and new["award_published"] == "2026-07-21" and new["source"] == "OpenData-DPSTenders"
    assert new["title"] == "VEX Equipment" and new["procedure"] == "DPS"
    piped = normalise_etenders(NEW.replace("Innovation First Trading Sarl", "| Alanna Homes Ltd | Cairn Homes"))[0]
    assert piped["suppliers"] == "Alanna Homes Ltd; Cairn Homes"


def test_met_eireann_daily_file():
    text = ("Station Name: DUBLIN AIRPORT\nStation Height: 71 M \nLatitude:53.428  ,Longitude: -6.241\n\n\n  date - 00 to 00 UTC\n\n"
            "date,ind,maxtp,ind,mintp,igmin,gmin,ind,rain,cbl,wdsp,ind,hm,ind,ddhm,ind,hg,soil,pe,evap,smd_wd,smd_md,smd_pd\n"
            "30-aug-2026,0,18.8,0,12.5,0,8.2,0,11.6,993.9,6.0,0,12,0,250,0,17,2.8,0,1040,16.975,1.6,2.2\n"
            "01-sep-2026,0,17.0,0,11.0,0,8.0,1,0.0,990.0,9.0,0,20,0,250,0,31,2.8,0,1040,1,1,1\n")
    facts, rows = parse_daily(text)
    assert facts["name"] == "DUBLIN AIRPORT" and facts["lat"] == 53.428 and facts["lon"] == -6.241
    assert rows[0][0] == "2026-08-30" and rows[1][0] == "2026-09-01" and len(rows) == 2
    assert rows[0][1] == "11.6" and rows[0][2] == "0" and rows[1][2] == "1"       # rain value and its quality indicator


def test_ted_hit_parses_winner_value_bids():
    n = {"publication-number": "632973-2026", "notice-type": "can-standard", "publication-date": "2026-09-15+02:00",
         "buyer-name": {"eng": ["The Office of Government Procurement"]}, "title-proc": {"eng": "Provision of electricity"},
         "organisation-name-tenderer": {"eng": ["Energia", "Energia"]}, "classification-cpv": ["09310000", "65310000", "09310000"],
         "tender-value": ["103520982"], "total-value": 103520982, "received-submissions-type-val": ["4"], "winner-size": ["large"],
         "contract-nature-main-proc": "supplies", "links": {"html": {"ENG": "https://ted.europa.eu/en/notice/-/detail/632973-2026"}}}
    r = parse_ted(n)
    assert r["winners"] == "Energia" and r["award_value"] == 103520982 and r["bids"] == 4 and r["cpv"] == "09310000"
    assert r["buyer"].startswith("The Office") and r["publication_date"].isoformat() == "2026-09-15" and r["url"].endswith("632973-2026")


def test_publish_includes_collector_artifacts_and_ted_by_year():
    init_db()
    artifacts.put("v1/tenders/etenders_notices.csv.gz", b"x", "application/gzip", 1, "d", "etenders_opendata")
    with SessionLocal() as s:
        files = build(s, 1)
    assert "v1/tenders/etenders_notices.csv.gz" in files
    artifacts.clear()
