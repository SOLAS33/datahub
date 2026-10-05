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

## Sources

| Key | Source | Every | Used by |
|---|---|---|---|
| `eirgrid_live` | EirGrid Smart Grid Dashboard: 15-min wind, solar, demand (ALL/ROI/NI), SNSP, CO2, interconnectors, fuel mix, EirGrid's own forecasts | 1 h | gridwatch, dcwatch |
| `eirgrid_dd` | EirGrid DD Summary Report workbook: monthly dispatch-down, causes, regional rates, farm list, each figure with its cell | 6 h | gridwatch |
| `weather_forecasts` | Met Éireann (HARMONIE/ECMWF) and MET Norway point forecasts at 23 points | 3 h | gridwatch |
| `weather_obs` | Met Éireann hourly observations, 18 stations | 1 h | gridwatch |
| `weather_warnings` | Met Éireann warnings | 1 h | gridwatch |
| `planning_npad_dc` | Raw mirror: council planning filings mentioning a data centre (ArcGIS) | 24 h | dcwatch |
| `acp_cases_dc` | Raw mirror: An Coimisiún Pleanála cases mentioning a data centre | 24 h | dcwatch |
| `cso_mec02` | Raw mirror: CSO MEC02 data-centre electricity (CSV as published) | 24 h | dcwatch |
| `etenders_opendata` | OGP eTenders open dataset: every competition since 2013 with awards (suppliers, value, bids). Normalised CSV plus the untouched original | 24 h (file changes ~quarterly) | tenderwatch |
| `ted_ireland` | TED notices from Irish buyers: contract notices (open tenders) and award notices (winner, value, bids), from 2024 | 6 h | tenderwatch |
| `climate_daily` | Met Éireann daily climate records, ~520 stations with decades of rain, temperature, wind, sunshine. Refreshed a chunk of stations per run | 3 h (new data monthly) | tenderwatch |

The current list, with health and last success, is at `/v1/status.json`. The file list is `/v1/catalog.json`.

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
| `ledger/YYYY-MM.csv` | Every payload: source, URL, time, SHA-256, model run |
| `events.json` | New workbook months, warnings, records, source outages and recoveries |

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
.venv\Scripts\python -m tools.run --all --no-publish     # collect only
.venv\Scripts\python -m pytest
```

Production runs `.github/workflows/hub.yml` hourly. It uses org secret `CF_API_TOKEN` and org
variable `CF_ACCOUNT_ID`, and keeps its database on the `state` branch.
