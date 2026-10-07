"""Met Éireann warnings from the RSS feed and CAP files (the old JSON feed returns 404)."""
import pytest

from s33weather.sources import metie_warnings as mw

RSS = b'<?xml version="1.0"?><rss version="2.0"><channel><item><title>x</title><link>https://cap.met.ie//a.xml</link></item><item><link>https://cap.met.ie//b.xml</link></item></channel></rss>'
EMPTY = b'<?xml version="1.0"?><rss version="2.0"><channel><title>Met</title></channel></rss>'
CAP = b"""<?xml version="1.0"?><alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><identifier>2.49.X</identifier><sent>2026-10-06T12:53:14+01:00</sent><status>Actual</status>
<info><language>en-GB</language><event>Wind</event><severity>Severe</severity><effective>2026-10-06T12:53:14+01:00</effective><onset>2026-10-07T20:00:00+01:00</onset><expires>2026-10-08T02:00:00+01:00</expires>
<headline>Orange wind warning</headline><description>Gusts of 100 km/h.</description><parameter><valueName>awareness_level</valueName><value>3; orange; Severe</value></parameter>
<area><areaDesc>Cork</areaDesc></area><area><areaDesc>Kerry</areaDesc></area></info></alert>"""


def test_rss_links_and_empty_feed():
    assert mw.rss_links(RSS) == ["https://cap.met.ie//a.xml", "https://cap.met.ie//b.xml"]
    assert mw.rss_links(EMPTY) == []          # read successfully: genuinely none in force


def test_bad_feed_raises_rather_than_looking_empty():
    with pytest.raises(Exception):
        mw.rss_links(b"<html>not found</html>")
    with pytest.raises(Exception):
        mw.rss_links(b"not xml at all")


def test_cap_parsing():
    (w,) = mw.parse_cap(CAP)
    assert w.id == "2.49.X" and w.level == "orange" and w.type == "Wind" and w.regions == ["Cork", "Kerry"]
    assert w.onset.isoformat().startswith("2026-10-07T20:00") and w.headline == "Orange wind warning"
