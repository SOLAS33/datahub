# Solas33 Data Hub

**https://data.solas33.com**

The hub collects each public source once, stores it with full provenance, and publishes tidy
datasets plus a database snapshot. Every Solas33 site reads from it. Adding a site therefore
adds no new scraping, and stays within the free tiers.

```
public sources ──► hub (this repo, hourly GitHub Actions, free because the repo is public)
                    ├─ collectors: each source fetched once, every payload logged (URL, time, SHA-256)
                    └─ publish: changed files only ──► Cloudflare R2 ──► data.solas33.com (Worker)
                                                                        ▲
                         gridwatch, dcwatch, future sites ─ read ───────┘
```

## How the hub is organised

The hub is **domains**, not one pile. `core` is the original hub (grid, weather, planning mirrors): one database, one snapshot `hub.sqlite.gz`, an hourly job. Every other domain is an independent job with its own small database and its own published files, run every 3 hours by a CI matrix (`.github/workflows/hub-domains.yml`). A site downloads only the files it needs; a slow or failing domain cannot hold up another; the shared snapshot does not grow. `/v1/catalog.json` and `/v1/status.json` are the merge of every domain.

| Domain | Sources |
|---|---|
| core | EirGrid live and dispatch-down, Met Éireann/MET Norway forecasts, observations, warnings, planning and An Coimisiún Pleanála mirrors, CSO data-centre electricity |
| procurement | eTenders open dataset (87k competitions, awards), TED notices from Irish buyers |
| climate | Met Éireann daily climate records, ~520 stations |
| environment | OPW river and lake levels, Marine Institute tide gauges (daily min/max/mean), EPA: 42 GeoServer layers (licensed facilities, emission points, water and air monitoring) and bathing water |
| business | Companies Registration Office: every company (825k), new registrations by month, filed accounts |
| statistics | 48 CSO PxStat tables, 8 Eurostat datasets |
| property | Residential Property Price Register (no street addresses), monthly county medians |
| transport | NTA GTFS agencies, routes, stops |
| governance | Oireachtas members and bills |
| energy_intl | Great Britain carbon intensity and generation mix |
| catalogue | index of all ~22,000 data.gov.ie datasets; mirrors of selected Revenue, TII and Dublin counter datasets |

Full register with licences, cadence, tier and rights: [docs/SOURCES.md](docs/SOURCES.md) (generated from the code). To add a source: [docs/ADDING_A_SOURCE.md](docs/ADDING_A_SOURCE.md).

## Sources in the core hub

| Key | Source | Every | Used by |
|---|---|---|---|
| `eirgrid_live` | EirGrid Smart Grid Dashboard: 15-min wind, solar, demand, SNSP, CO2, interconnectors, fuel mix; EirGrid's own forecasts. Backfills 400 days on first run. | 1 h | gridwatch, dcwatch |
| `eirgrid_dd` | EirGrid DD Summary Report workbook (monthly dispatch-down, causes, regional rates, farm list) | 6 h | gridwatch |
| `weather_forecasts` | Met Éireann + MET Norway point forecasts at 23 points | 3 h | gridwatch |
| `weather_obs` | Met Éireann hourly observations, 18 stations | 1 h | gridwatch |
| `weather_warnings` | Met Éireann warnings | 1 h | gridwatch |
| `planning_npad_dc`, `acp_cases_dc`, `cso_mec02` | Raw mirrors for DCWatch | 24 h | dcwatch |

## Published layout (`/v1/…`)

| Path | What's there |
|---|---|
| `hub.sqlite.gz` (+ `.json` with its hash) | Full snapshot; sites read it with `s33hub.client` |
| `eirgrid/grid_15min/YYYY-MM.csv` | 15-minute grid readings |
| `eirgrid/forecast_snapshots/YYYY-MM.csv` | EirGrid's own forecasts |
| `eirgrid/fuel_mix/YYYY-MM.csv` | Fuel mix |
| `eirgrid/dispatch_down/{monthly,regional,farms,workbooks}.csv` | Workbook figures, with `workbook, sheet, cell` |
| `weather/forecast_latest.csv` | Latest forecasts |
| `weather/observations/YYYY-MM.csv` | Station observations |
| `weather/warnings.json` | Warnings |
| `weather/{points,stations}.csv` | Sampling points and stations |
| `mirror/<key>/latest.*` and dated, hashed versions | Raw mirrors |
| `tenders/etenders_notices.csv.gz`, `tenders/raw/<date>-<sha>.csv.gz` | eTenders competitions and awards, normalised; and the original file for audit |
| `tenders/ted_notices/YYYY.csv` | TED notices from Irish buyers by publication year |
| `climate/stations.csv`, `climate/daily/<id>.csv.gz` | Met Éireann stations and their daily records (units as published: mm, °C, knots) |
| `statistics/cso/<TABLE>.csv.gz`, `statistics/eurostat/<dataset>.csv.gz` (+ `index.csv` in each) | CSO PxStat and Eurostat tables |
| `property/ppr/<year>.csv.gz`, `property/ppr_monthly_county.csv.gz` | Property Price Register sales (no addresses) and monthly county medians |
| `water/opw_{stations.csv,latest.json}`, `water/opw_daily/<year>.csv`, `water/tide_daily/<year>.csv` | River/lake levels and tide gauges: stations, latest, daily min/max/mean |
| `transport/gtfs_{agency,routes,stops}.csv.gz` | NTA public transport reference tables |
| `governance/oireachtas_{members,bills}.csv.gz` | Oireachtas members and bills |
| `energy_intl/gb_carbon_daily/<year>.csv`, `energy_intl/gb_generation_mix_daily/<year>.csv` | GB carbon intensity and generation mix, daily |
| `business/cro/companies/<year>.csv.gz`, `business/cro/{registrations_monthly,recent_registrations}.csv.gz`, `business/cro/financial_statements/*.csv.gz` | CRO companies by registration year (no addresses), new-company counts, accounts filed |
| `environment/epa/<layer>.{geojson,csv}.gz`, `environment/epa/index.csv`, `environment/epa_bathing_water_*.csv.gz` | EPA layers and bathing water |
| `catalogue/datagovie_packages.csv.gz`, `open/index.csv`, `open/<dataset>/<file>.gz` | data.gov.ie index; mirrored Revenue, TII and Dublin counter datasets |
| `ledger/YYYY-MM.csv` (core), `ledger/<domain>/YYYY-MM.csv` | Every payload: source, URL, time, SHA-256, model run |
| `events.json` (core), `events/<domain>.json` | New workbook months, warnings, records, source outages and recoveries |
| `catalog.json`, `status.json` | Merged across every domain (also `catalog/<domain>.json`, `status/<domain>.json`) |

Licence: compiled data CC BY 4.0. Cite "Solas33 Data Hub" and the original publisher. Each
publisher's own terms also apply.

## Using it from a site

```python
from sqlalchemy.orm import sessionmaker
from s33hub.client import fetch_snapshot, hub_engine, fetch_mirror
from s33hub.db import HubBase

hub = hub_engine(fetch_snapshot("data/hub.db"))        # downloads only when changed
Session = sessionmaker(binds={HubBase: hub, MySiteBase: my_engine})
doc, provenance = fetch_mirror("planning_npad_dc")      # raw source data + where it came from
```

Install: `pip install "s33-datahub @ git+https://github.com/SOLAS33/datahub.git"`. This also
installs `s33weather` (see `s33weather/README.md`).

## Large files

A collector can hand the publisher files that are too big to be database rows with `s33hub.artifacts.put(path, bytes, type, rows, desc, source)` (used by eTenders and climate). They are uploaded when produced and stay on R2 otherwise.

## Adding a source

Write a `Collector` in `s33hub/collectors/`, register it in `collectors/__init__.py`, then add
its tables to `models.py` and its files to `publish.build`. Log every payload with `log_fetch`.

## Running locally

```powershell
python -m venv .venv; .venv\Scripts\python -m pip install -r requirements.txt
python -m tools.run --all --no-publish     # collect only (core)
.venv\Scripts\python -m tools.run --domain statistics --all --out .\out   # one domain, files into a local folder
.venv\Scripts\python -m pytest
```

Production runs `.github/workflows/hub.yml` hourly (core) and `.github/workflows/hub-domains.yml` every 3 hours (one job per domain). It uses org secret `CF_API_TOKEN` and org
variable `CF_ACCOUNT_ID`, and keeps its database on the `state` branch.
