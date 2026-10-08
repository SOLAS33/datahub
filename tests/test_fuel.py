"""Fuel sources: each parser against a small realistic sample."""
from __future__ import annotations

from datetime import datetime

import pytest

from s33hub.collectors.fuel import (find_bulletin_links, nora_xls_link, page_signature, parse_archive, parse_balance_sheet, parse_brent, parse_bulletin_country_taxes,
                                    parse_bulletin_prices, parse_carbon_schedule, parse_ecb, parse_mot, parse_mot_page, parse_nora_sheet, parse_year_table, seai_links)


def test_bulletin_links_are_found_on_the_page():
    html = ('<a href="/document/download/aaa_en?filename=Weekly_Oil_Bulletin_Prices_History_maticni_4web.xlsx">h</a>'
            '<a href="/document/download/bbb_en?filename=Oil_Bulletin_Duties_and_taxes.xlsx">t</a><a href="/x.css">c</a>')
    got = find_bulletin_links(html)
    assert got["history"].startswith("https://energy.ec.europa.eu/document/download/aaa") and "Duties_and_taxes" in got["taxes"]


def test_bulletin_prices_join_with_and_without_tax_and_keep_gaps():
    head = ("Consumer prices", "CTR", "IE_price_with_tax_euro95", "IE_price_with_tax_diesel", "EU_price_with_tax_euro95", "GR_price_with_tax_euro95")
    wo = ("Prices wo", "CTR", "IE_price_wo_tax_euro95", "IE_price_wo_tax_diesel", "EU_price_wo_tax_euro95", "GR_price_wo_tax_euro95")
    d = datetime(2026, 10, 5)
    rows = parse_bulletin_prices([head, (None, None, "1000 l", "1000 l", "1000 l", "1000 l"), (d, "IE_", 1982.7, 2163.7, 1997.96, None)],
                                 [wo, (None, None, "1000 l", "1000 l", "1000 l", "1000 l"), (d, "IE_", 1108.07, None, 900.0, None)])
    assert ["2026-10-05", "IE", "petrol", "EUR per 1000 l", 1982.7, 1108.07] in rows
    assert ["2026-10-05", "IE", "diesel", "EUR per 1000 l", 2163.7, None] in rows           # a missing tax-exclusive value stays missing
    assert any(r[1] == "EU27" for r in rows)
    assert not any(r[1] in ("GR", "EL") for r in rows)                                     # a column with no value on a date makes no row


def test_country_tax_block_carries_the_country_code_down_the_block():
    sheet = [(None,) * 8] * 4 + [("AT_", datetime(2026, 1, 1), 5, 6, None, None, None, None), ("IE_", datetime(2026, 3, 25), 419.88, 263.11, 20.92, None, None, None),
                                 (None, datetime(2026, 1, 1), 584.18, 452.72, 47.36, 14.78, None, None), ("IT_", datetime(2026, 1, 1), 1, 1, 1, 1, 1, 1)]
    got = parse_bulletin_country_taxes(sheet)
    assert got == [["2026-03-25", 419.88, 263.11, 20.92, None, None, None], ["2026-01-01", 584.18, 452.72, 47.36, 14.78, None, None]]


MOT = """
Mineral Oil Tax                      Rates effective             from 15 April 2026
Light Oil:   ERN
Petrol………………...………………  8014  8514  7014  X101  7514  Y101           €338.58*                 €164.30                     €502.88*
Aviation gasoline………………  8012  8512  €338.58*  €164.30  €502.88*
Heavy Oil:
Used as           a propellant………………………….....  8108  8508  €181.81*  €190.04  €371.85*
Used for  air navigation………  8106  €181.81*  €190.04  €371.85*
Kerosene used other than as a propellant…………...  8102  €00.00  €160.81  €160.81
Other heavy oil including marked gas oil………………  81038503  €00.00*  €172.14  €172.14
Substitute Fuel:
Used as a propellant instead of petrol…………  8126  €338.58*  €164.30  €502.88*
Mineral Oil Tax   Rates effective from 8 April 2009 to 9 December 2009
Petrol…………………  8014  7014  €508.79
Used as  a propellant………………  8108  7108  €409.20*
Solid Fuel Carbon Tax Rates
"""


def test_mot_periods_are_read_and_the_parts_add_up():
    rows = parse_mot(MOT)
    cur = {r["product"]: r for r in rows if r["valid_from"] == "2026-04-15"}
    assert (cur["petrol"]["non_carbon"], cur["petrol"]["carbon"], cur["petrol"]["total"]) == (338.58, 164.30, 502.88)
    assert cur["diesel"]["total"] == 371.85 and cur["marked_gas_oil"]["total"] == 172.14 and cur["kerosene"]["non_carbon"] == 0.0
    old = {r["product"]: r for r in rows if r["valid_from"] == "2009-04-08"}
    assert old["petrol"]["total"] == 508.79 and old["petrol"]["carbon"] == 0.0 and old["diesel"]["valid_to"] == "2009-12-09"


def test_mot_that_does_not_add_up_is_refused():
    with pytest.raises(ValueError):
        parse_mot(MOT.replace("€502.88*\nAviation", "€512.88*\nAviation", 1))


def test_carbon_schedules():
    text = "Amount SFCT rate per tonne of fuel\n 1 May 2025 €63.50 €167.24 €116.43 €57.70 €86.54\n 14 October 2026 €71.00 €187.00 €130.18 €64.52 €96.76\n"
    got = parse_carbon_schedule(text, r"Amount\s+SFCT\s+rate", ["coal", "peat_briquettes", "milled_peat", "other_peat"])
    assert got[1] == dict(valid_from="2026-10-14", per_tonne_co2=71.0, coal=187.0, peat_briquettes=130.18, milled_peat=64.52, other_peat=96.76)
    gas = parse_carbon_schedule("Natural Gas Carbon Tax Rate\n 1 May 2025 €63.50 €11.48 0.9017\n", r"Natural\s+Gas\s+Carbon\s+Tax\s+Rate", ["rate_per_mwh_gcv", "ncv_to_gcv_factor"])
    assert gas == [dict(valid_from="2025-05-01", per_tonne_co2=63.5, rate_per_mwh_gcv=11.48, ncv_to_gcv_factor=0.9017)]


def test_mot_page_cross_check_values():
    t = "Mineral Oil Tax effective from 15 April 2026. Light Oil Petrol €338.58 €164.30 €502.88 Heavy Oil Used as a propellant €181.81 €190.04 €371.85 Published: 29 September 2026"
    p = parse_mot_page(t)
    assert p["effective"] == "2026-04-15" and p["petrol"][2] == 502.88 and p["diesel"][2] == 371.85 and p["published"] == "29 September 2026"


def test_brent_and_ecb():
    assert parse_brent([["2026-10-06", 125.44], ["2026-10-05", ""], [None, 5]]) == [["2026-10-06", 125.44]]
    fx = parse_ecb("Date,USD,JPY,GBP\n2026-10-07,1.1177,176.85,0.84645\n2026-10-06,N/A,1,0.8\n1999-01-04,1.1789,133.73,0.7111\n")
    assert fx[-1] == ["2026-10-07", 1.1177, 0.84645] and fx[1] == ["2026-10-06", None, 0.8]


def test_seai_links_and_archive():
    html = '<a href="/sites/default/files/2025-05/National-Energy-Balance.xlsx">a</a><a href="/sites/default/files/publications/Domestic-Fuel-Cost-Archive.xlsx">b</a>'
    got = seai_links(html)
    assert got["balance"].endswith("2025-05/National-Energy-Balance.xlsx") and got["archive"].startswith("https://www.seai.ie/")
    head = ("Date", "Kerosene (schedule)", "Machine Turf", "Special Notes")
    d = datetime(2026, 7, 1)
    rows = parse_archive([head, (d, 1.28, "n/a", "note")], [head, (d, 12.6, "n/a", "note")])
    assert rows == [["2026-07-01", "Kerosene (schedule)", 1.28, 12.6]]


def test_year_tables_and_balance():
    t = parse_year_table([("Transport", "NACE", 1990, 1991), ("Oil", None, 2017.4, None), ("", None, 1, 1)], "Transport")
    assert t == [["Transport", "Oil", "", 1990, 2017.4]]
    rows = [("2025 Interim                        Units = ktoe\n", "NACE (Rev 2)", " Coal", " Peat"), ("Imports", None, 163.2, None), ("Exports", None, 13.8, 0)]
    got = parse_balance_sheet(rows, "2025 Interim")
    assert got == [[2025, "interim", "Imports", "Coal", 163.2], [2025, "interim", "Exports", "Coal", 13.8], [2025, "interim", "Exports", "Peat", 0.0]]


def test_nora_link_and_sheet_with_biofuel_header_and_unreported_months():
    link = nora_xls_link('<a href="https://www.nora.ie/_files/ugd/54c1f3_x.xls?dn=Volumes%20of%20Oil%20Consumption.xls">v</a>')
    assert link.endswith("54c1f3_x.xls")
    rows = [[""] * 6, ["", "", "", "BIOFUEL IN", "", ""], ["", "Month", "GASOLINE", "GASOLINE", "KEROSENE", "ALL FUELS"], [""] * 6,
            ["", "July", 96.0, 10.0, 35.0, 141.0], ["", "August", 91.0, 9.0, 39.0, 139.0], ["", "September", 0, 0, 0, 0], ["", "Total", 1, 1, 1, 1]]
    got = parse_nora_sheet(rows, 2026)
    assert ["2026-07", "GASOLINE", 96.0] in got and ["2026-07", "BIOFUEL IN GASOLINE", 10.0] in got
    assert not any(r[0] == "2026-09" for r in got)


def test_page_signature_keeps_no_text():
    sig = page_signature(b"<html><title>Fuel Allowance</title><body><p>Last updated 8 October 2026</p><p>secret words</p></body></html>")
    assert sig["title"] == "Fuel Allowance" and sig["stated_date"] == "8 October 2026" and len(sig["text_sha256"]) == 64 and "secret" not in str(sig)
