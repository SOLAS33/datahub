"""Every source the hub collects. Add a source: write a Collector, list it here.

Each collector declares its `domain`: "core" is the original hub (grid, weather, planning mirrors; one database, one snapshot, hourly job);
every other domain is a self-contained job with its own small database and its own published files (see docs/ADDING_A_SOURCE.md).
"""
from __future__ import annotations

from .base import Collector, last_run, make_client, run_collector
from .business import CroCompanies
from .catalogue import CkanOpenDatasets, DataGovIeCatalogue
from .climate import ClimateDaily
from .eirgrid import EirGridLive
from .eirgrid_dd import EirGridDispatchDown
from .energy_intl import NesoCarbon
from .environment import MarineTideGauges, OpwWaterLevels
from .epa import EpaBathingWater, EpaWfsLayers
from .governance import EuSanctions, Oireachtas
from .mirrors import AcpDataCentres, CsoMec02, PlanningDataCentres
from .property import PlanningPipeline, PropertyPriceRegister
from .statistics import CsoPxStat, Eurostat
from .water import UisceEireannAssets, UisceEireannTariffs
from .tenders import EtendersOpenData, TedIreland
from .transport import NtaGtfs
from .weather import WeatherForecasts, WeatherObservations, WeatherWarnings

REGISTRY: dict[str, Collector] = {c.key: c for c in (
    # core: the original hourly hub
    EirGridLive(), EirGridDispatchDown(), WeatherForecasts(), WeatherObservations(), WeatherWarnings(), PlanningDataCentres(), AcpDataCentres(), CsoMec02(),
    # procurement
    EtendersOpenData(), TedIreland(),
    # climate and environment
    ClimateDaily(), OpwWaterLevels(), MarineTideGauges(), EpaWfsLayers(), EpaBathingWater(), UisceEireannAssets(), UisceEireannTariffs(),
    # companies, statistics, property, transport, governance, energy abroad, catalogue
    CroCompanies(), CsoPxStat(), Eurostat(), PropertyPriceRegister(), PlanningPipeline(), NtaGtfs(), Oireachtas(), EuSanctions(), NesoCarbon(), DataGovIeCatalogue(), CkanOpenDatasets())}

CORE = "core"
DOMAINS = sorted({c.domain for c in REGISTRY.values()} - {CORE})


def for_domain(domain: str) -> dict[str, Collector]:
    return {k: c for k, c in REGISTRY.items() if c.domain == domain}


__all__ = ["CORE", "DOMAINS", "REGISTRY", "Collector", "for_domain", "last_run", "make_client", "run_collector"]
