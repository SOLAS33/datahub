"""s33weather: parsers on real recorded payloads, physics sanity, blending, verification."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from s33weather import points
from s33weather.blend import blend
from s33weather.calibrate import fit_grid, linear_fit
from s33weather.energy import RenewableModel
from s33weather.models import Location
from s33weather.physics import solar, thermo, wind
from s33weather.sources import metie, metie_obs, metie_warnings, metno
from s33weather.timeseries import to_hourly
from s33weather.verify import bucket, error_quantiles, scores

FX = Path(__file__).parent / "fixtures"
LOC = Location(id="sw-kerry-north", name="test", lat=52.25, lon=-9.45)


def test_metie_parse_fixture():
    steps, runs = metie.parse((FX / "metie_forecast.xml").read_bytes(), LOC)
    assert len(steps) > 100
    assert {r["model"] for r in runs} == {"harmonie", "ecmwf"}
    s0 = steps[0]
    assert s0.wind_speed is not None and 0 <= s0.wind_speed < 60
    assert s0.model == "harmonie" and s0.issued_at is not None
    assert any(s.ghi_wm2 for s in steps)                          # HARMONIE gives irradiance
    assert any(s.model == "ecmwf" for s in steps)                 # and hands over to ECMWF later
    assert all(s.valid_at.tzinfo is not None for s in steps)
    with_precip = [s for s in steps if s.precip_period_h]
    assert with_precip and all(s.precip_period_h > 0 for s in with_precip)


def test_metno_parse_fixture():
    steps, issued = metno.parse((FX / "metno_complete.json").read_bytes(), LOC)
    assert len(steps) > 60 and issued is not None
    assert steps[0].wind_speed is not None and steps[0].cloud_pct is not None
    assert steps[0].ghi_wm2 is None                                # MET Norway has no irradiance


def test_obs_station_check_rejects_substituted_station():
    raw = (FX / "metie_obs_malin.json").read_bytes()
    obs = metie_obs.parse(raw, "malin-head", "Malin Head")
    assert obs and all(o.observed_at.tzinfo is not None for o in obs)
    kmh = [o for o in obs if o.wind_speed is not None][0]
    assert kmh.wind_speed < 60                                     # converted from km/h
    with pytest.raises(ValueError, match="station mismatch"):
        metie_obs.parse(raw, "valentia", "Valentia")


def test_warnings_parse_shapes():
    assert metie_warnings.parse(b"[]") == []
    w = metie_warnings.parse(b'[{"id":"x1","type":"Wind","level":"Orange","headline":"Storm","description":"d","regions":["EI11","EI99"],'
                             b'"onset":"2026-10-05T06:00:00+01:00","expiry":"2026-10-05T18:00:00+01:00"}]')[0]
    assert w.level == "orange" and w.regions == ["Kerry", "EI99"] and w.onset.utcoffset().total_seconds() == 3600


def test_shear_density_and_curves():
    assert wind.shear_power_law(10, 10, 90, 0.14) == pytest.approx(10 * 9 ** 0.14)
    assert wind.air_density(15, 1013.25, 0, 0, 0) == pytest.approx(1.225, abs=0.01)
    assert wind.air_density(-5, 1030, 80) > wind.air_density(25, 990, 80)
    pc = wind.PowerCurve()
    assert pc(2) == 0 and pc(13) == 1 and pc(30) == 0
    assert 0 < pc(26) < 1                                           # storm ride-through ramp
    vals = [pc(v / 10) for v in range(30, 125)]
    assert all(b >= a - 1e-9 for a, b in zip(vals, vals[1:]))      # monotonic up to rated
    fleet = wind.FleetCurve()
    assert 0 < fleet(4) < pc(6) and fleet(12.5) < 1 and fleet(40) < 0.05
    assert wind.compass(225) == "SW" and wind.beaufort(10.0) == 5
    assert wind.circular_mean_direction([350, 10]) == pytest.approx(0, abs=1e-6) or wind.circular_mean_direction([350, 10]) == pytest.approx(360, abs=1e-6)


def test_sun_position_dublin_solstice_noon():
    t = datetime(2026, 6, 21, 12, 25, tzinfo=timezone.utc)        # ~local solar noon in Dublin
    zen, az = solar.sun_position(t, 53.35, -6.26)
    assert zen == pytest.approx(90 - (90 - 53.35 + 23.44), abs=0.6)
    assert az == pytest.approx(180, abs=5)
    assert solar.pv_capacity_factor(datetime(2026, 12, 21, 0, 0, tzinfo=timezone.utc), 53.35, -6.26, cloud_pct=0) == 0
    assert 0.5 < solar.pv_capacity_factor(t, 53.35, -6.26, cloud_pct=0) < 1.0


def test_thermo():
    assert thermo.rh_from_dewpoint(20, thermo.dewpoint_c(20, 60)) == pytest.approx(60, abs=0.5)
    assert thermo.heating_degree_hours(10) == 5.5


def test_blend_and_hourly():
    a, _ = metie.parse((FX / "metie_forecast.xml").read_bytes(), LOC)
    b, _ = metno.parse((FX / "metno_complete.json").read_bytes(), LOC)
    ha, hb = to_hourly(a), to_hourly(b)
    assert all(s.valid_at.minute == 0 for s in ha)
    out = blend({"metie": ha, "metno": hb})
    both = [x for x in out if len(x.members) == 2]
    assert both and all(x.wind_speed_spread is not None for x in both)
    x = both[0]
    lo, hi = sorted(m.wind_speed for m in x.members.values())
    assert lo - 1e-9 <= x.wind_speed <= hi + 1e-9


def test_renewable_model_on_fixtures():
    a, _ = metie.parse((FX / "metie_forecast.xml").read_bytes(), LOC)
    wp = [Location(id="p", name="p", lat=52.25, lon=-9.45, weight=1, tags=("SW", "wind"))]
    sp = [Location(id="p", name="p", lat=52.25, lon=-9.45, weight=1, tags=("SW", "solar"))]
    m = RenewableModel(wp, sp, {"SW": 1000.0}, {"SW": 100.0})
    hours = m.forecast({"metie": {"p": a}})
    assert hours and all(0 <= h.wind_mw <= 1000 for h in hours if h.wind_mw is not None)
    assert all(h.wind_p10 <= h.wind_mw <= h.wind_p90 for h in hours if h.wind_mw is not None)
    assert any((h.solar_mw or 0) > 0 for h in hours) and all(0 <= (h.solar_mw or 0) <= 110 for h in hours)


def test_points_cover_every_region():
    regs = {p.tags[0] for p in points.wind_points()}
    assert regs == set(points.WIND_CAPACITY_MW)
    assert {p.tags[0] for p in points.solar_points()} == set(points.SOLAR_CAPACITY_MW)


def test_verification_and_calibration():
    s = scores([(10, 8), (5, 6)], normaliser=100, ref_pairs=[(12, 8), (2, 6)])
    assert s.mae == 1.5 and s.bias == 0.5 and s.ref_mae == 4 and s.skill_vs_ref == pytest.approx(0.625)
    assert bucket(3) == "0-6 h" and bucket(30) == "Day 2" and bucket(500) is None
    q = error_quantiles(list(range(11)))
    assert q[0.5] == 5
    fit = fit_grid(lambda p, x: p["a"] * x, [(1, 2), (2, 4), (3, 6)], {"a": [1, 2, 3]}, prior={"a": 1})
    assert fit.params == {"a": 2} and fit.mae == 0 and fit.baseline_mae > 0
    assert linear_fit([1, 2, 3], [3, 5, 7]) == pytest.approx((1, 2))
    assert math.isfinite(fit.mae)
