"""Uisce Éireann workbooks and tariffs, and water-related planning applications."""
from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

from s33hub.collectors.property import summarise_planning, water_match
from s33hub.collectors.water import parse_tariffs, read_workbook, snake


def book(rows):
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def test_headers_become_snake_case_and_population_date_is_kept():
    head, rows, asof = read_workbook(book([["Water Services Area", "Water Supply Zone Name", "EDEN Water Supply Zone Code", "Owned By", "Population In Water Supply Zone as of 27.03.2026"],
                                           ["Wexford", "Raheen", "3300PUB1818", "Irish Water", 19], [None, None, None, None, None], ["Cork", "Bola", "1000PUB1", "Irish Water", 22.0]]))
    assert head == ["water_services_area", "name", "eden_code", "owned_by", "population"] and asof == "2026-03-27"
    assert rows == [["Wexford", "Raheen", "3300PUB1818", "Irish Water", "19"], ["Cork", "Bola", "1000PUB1", "Irish Water", "22"]]     # empty row dropped, 22.0 -> 22
    assert snake("L5 Number") == "asset_id" and snake("LA") == "local_authority"


PAGE = """<p>Water and wastewater tariff classes and rates effective 1/10/2025 – 30/09/2026 - Water Service Charges Wastewater Service Charges Combined Service Charges
Metered Tariffs Standing Charge (€/year) Volumetric Charge (€/m³) Standing Charge (€/year) Volumetric Charge (€/m³) Standing Charge (€/year) Volumetric Charge (€/m³)
Band 1 Class (&lt;1,000m³) 91.16 2.40 82.82 2.57 173.98 4.97 Band 2 Class (1,000m³ - 19,999m³) 239.48 1.84 261.70 2.50 501.18 4.34
Band 3 Class (20,000m³ - 249,999m³) 4,072.05 1.71 4,227.03 2.45 8,299.08 4.16 Band 4 Class (250,000m³-2,299,999m³) 45,382.72 1.52 42,440.49 2.40 87,823.21 3.92
Band 5 Class (&gt;2,300,000m³) 324,972.56 1.39 - - - -
Unmetered Tariffs - Water Service Charges Band 1* 313.39 337.76 651.15 Band 2 1,982.82 2,577.52 4,560.34</p>
<p>Water and wastewater tariff classes and rates effective 1/10/2026 – 30/09/2027 Metered tariff rates
Band 1 Class (&lt;1,000m³) €93.67 €2.37 €102.96 €2.43 €196.63 €4.80 Band 2 Class (1,000m³ - 19,999m³) €246.02 €1.84 €346.04 €2.41 €592.06 €4.25
Band 3 Class (20,000m³ - 249,999m³) €4,143.51 €1.66 €4,268.88 €2.38 €8,412.39 €4.04 Band 4 Class (250,000m³-2,299,999m³) €46,557.15 €1.47 €100,520.15 €2.37 €147,077.30 €3.84
Band 5 Class (&gt;2,300,000m³) €333,112.19 €1.45 - - - -
Unmetered Tariffs Band 1* €345.44 €417.16 €762.60 Band 2 €1,921.62 €2,635.23 €4,556.85</p>"""


def test_tariffs_are_read_for_both_periods_and_add_up():
    ps = parse_tariffs(PAGE)
    assert [(p["from"], p["to"]) for p in ps] == [("2025-10-01", "2026-09-30"), ("2026-10-01", "2027-09-30")]
    b1 = ps[1]["metered"][0]
    assert (b1["band"], b1["water_standing"], b1["wastewater_volumetric"], b1["combined_standing"], b1["combined_volumetric"]) == (1, 93.67, 2.43, 196.63, 4.80)
    b5 = ps[0]["metered"][4]
    assert b5["band"] == 5 and b5["water_standing"] == 324972.56 and b5["combined_standing"] is None
    assert ps[1]["unmetered"][0] == {"band": 1, "water": 345.44, "wastewater": 417.16, "combined": 762.6}


def test_a_figure_that_does_not_add_up_is_rejected():
    with pytest.raises(ValueError, match="combined does not equal"):
        parse_tariffs(PAGE.replace("173.98", "175.98"))


def test_a_changed_page_layout_fails_loudly():
    with pytest.raises(ValueError):
        parse_tariffs("<p>Our charges have moved. Please see the new page.</p>")


def test_water_match_keeps_infrastructure_and_drops_single_home_systems():
    assert "pumping station" in water_match("Construction of a wastewater pumping station and associated rising main")
    assert water_match("Extension of the Ballymore wastewater treatment plant")
    assert water_match("New 5 million litre service reservoir and watermain")
    assert water_match("Upgrade of Kilcock sewerage scheme including new pumping station")
    assert water_match("Install a septic tank and percolation area for a new dwelling") == ""
    assert water_match("Domestic wastewater treatment system and percolation area") == ""
    assert water_match("Dwelling house with a borehole for private water supply") == ""
    assert water_match("Permission for the construction of a detached 4 bedroom house, vehicular entrance, waste water treatment system and all site works") == ""
    assert water_match("Permission to construct dwelling house and connect to the public sewer and watermain") == ""
    assert water_match("Permission for 40 homes with surface water attenuation tank") == ""
    assert water_match("Boreholes for groundwater abstraction for industrial use")
    assert water_match("Group water scheme new treatment plant at Lough Cuilin")
    assert water_match("Dwelling and waste water treatment plant") == ""
    assert water_match("Construction of 188 dwellinghouses and a foul pumping station")


def test_planning_summary_lists_water_applications_without_addresses():
    f = {"attributes": dict(PlanningAuthority="Cork County Council", ApplicationNumber="W/1", DevelopmentDescription="Proposed new water treatment plant", ApplicationStatus="X", ApplicationType="P",
                            Decision="CONDITIONAL", NumResidentialUnits=0, FloorArea=100, ReceivedDate=1735689600000, DecisionDate=None, LinkAppDetails="https://x", DevelopmentAddress="1 Real Road")}
    monthly, major, water = summarise_planning([f])
    assert len(water) == 1 and water[0][1] == "W/1" and "water treatment" in water[0][8]
    assert "Real Road" not in " ".join(str(x) for x in water[0])
