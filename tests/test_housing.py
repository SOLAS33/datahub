"""Housing sources: parsers on small realistic samples."""
from __future__ import annotations

import pytest

from s33hub.collectors.housing import category_of, diff_amounts, extract_page, extract_pdf, filter_rents, parse_homeless


def test_rent_rows_keep_the_totals_and_drop_blanks():
    lines = ['"STATISTIC","Statistic Label","TLIST(H1)","HalfYear","C1","Number of Bedrooms","C2","Property Type","C3","Location","UNIT","VALUE"',
             '"RIH02","x","20081","2008H1","-","All bedrooms","-","All property types","110200","Carlow Town","Euro",""',
             '"RIH02","x","20251","2025H1","-","All bedrooms","-","All property types","1","Galway City","Euro","1900.5"',
             '"RIH02","x","20251","2025H1","-","Two bed","-","All property types","1","Galway City","Euro","2100"',
             '"RIH02","x","20251","2025H1","-","Two bed","-","Apartment","1","Galway City","Euro","2000"']
    head, rows = filter_rents(lines)
    assert head[0] == "half_year" and rows == [["2025H1", "Galway City", "All bedrooms", "All property types", "1900.5"], ["2025H1", "Galway City", "Two bed", "All property types", "2100"]]


def test_homelessness_month_comes_from_the_package_name_and_columns_are_checked():
    csv_text = "Region,Total Adults,Male Adults\nDublin,5000,3000\nCork,300,200\n"
    month, rows, head = parse_homeless("homelessness-report-august-2026", csv_text)
    assert month == "2026-08" and head[:2] == ["region", "total_adults"] and rows[0] == ["2026-08", "Dublin", "5000", "3000"]
    with pytest.raises(ValueError):
        parse_homeless("homelessness-report-augusto-2026", csv_text)
    with pytest.raises(ValueError):
        parse_homeless("homelessness-report-august-2026", "Area,Count\nDublin,1\n")


PAGE = """<html><head><title>Vacant Property Refurbishment Grant | Citizens Information</title>
<meta name="description" content="A grant to bring vacant properties back into use."><script>var x=1;</script></head>
<body><nav>Menu €999 nav</nav><main><h1>Vacant Property Refurbishment Grant</h1>
<p>A grant of up to €50,000 is available. A top-up of up to €20,000 applies to derelict homes. This scheme is no longer available for new applications from 2 March 2026.</p>
<p>Last updated 8 October 2026</p></main><footer>€1 footer</footer></body></html>"""


def test_a_grant_page_gives_title_amounts_with_sentences_dates_and_closed_words():
    r = extract_page(PAGE)
    assert r["title"] == "Vacant Property Refurbishment Grant" and r["description"].startswith("A grant to bring")
    amounts = {a["amount"]: a["context"] for a in r["amounts"]}
    assert "€50,000" in amounts and "€20,000" in amounts and "€999" not in amounts and "€1" not in amounts      # navigation and footer text are ignored
    assert "up to €50,000" in amounts["€50,000"]
    assert r["closed_words"] == ["no longer available"] and r["dates"][0]["date"] == "2 March 2026" and r["stated_updated"] == "8 October 2026"
    assert len(r["text_sha256"]) == 64 and "navigation" not in str(r)


def test_the_text_hash_changes_only_when_the_visible_text_changes():
    a, b = extract_page(PAGE), extract_page(PAGE.replace("<script>var x=1;</script>", "<script>var y=2;</script>"))
    c = extract_page(PAGE.replace("€50,000", "€60,000"))
    assert a["text_sha256"] == b["text_sha256"] != c["text_sha256"]
    added, removed = diff_amounts(a["amounts"], c["amounts"])
    assert added == ["€60,000"] and removed == ["€50,000"]


def test_category_from_the_address():
    assert category_of("https://www.seai.ie/grants/home-energy-grants/vacant-derelict-homes")[0] == "vacant"
    assert category_of("https://www.seai.ie/grants/home-energy-grants/supports-for-landlords") == ("energy", "landlord")
    assert category_of("https://x/housing-supports-for-older-people-and-people-with-disabilities/mobility-aids-grant-scheme")[0] == "adaptation"
    assert category_of("https://www.firsthomescheme.ie/how-it-works")[0] == "buy"
    assert category_of("https://www.seai.ie/grants/home-energy-grants/individual-grants/heat-pump-systems") == ("energy", "owner")


def test_a_pdf_states_its_own_date(monkeypatch):
    class P:
        def __init__(self, t): self.t = t
        def extract_text(self): return self.t
    class R:
        def __init__(self, f): self.pages = [P("Heat Pump €6,500 €4,500 Home Energy Grant Amounts December 2022")]
    monkeypatch.setattr("pypdf.PdfReader", R)
    r = extract_pdf(b"x")
    assert r["stated_updated"] == "December 2022" and [a["amount"] for a in r["amounts"]] == ["€6,500", "€4,500"]
