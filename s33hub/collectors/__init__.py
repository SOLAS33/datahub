"""Every source the hub collects, in run order. Add a source: write a Collector, list it here."""
from __future__ import annotations

from .base import Collector, last_run, make_client, run_collector
from .eirgrid import EirGridLive
from .climate import ClimateDaily
from .eirgrid_dd import EirGridDispatchDown
from .mirrors import AcpDataCentres, CsoMec02, PlanningDataCentres
from .tenders import EtendersOpenData, TedIreland
from .weather import WeatherForecasts, WeatherObservations, WeatherWarnings

REGISTRY: dict[str, Collector] = {c.key: c for c in (
    EirGridLive(), EirGridDispatchDown(), WeatherForecasts(), WeatherObservations(), WeatherWarnings(),
    PlanningDataCentres(), AcpDataCentres(), CsoMec02(), EtendersOpenData(), TedIreland(), ClimateDaily())}

__all__ = ["REGISTRY", "Collector", "last_run", "make_client", "run_collector"]
