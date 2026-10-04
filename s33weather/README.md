# s33weather

Solas33's reusable weather toolkit. It was built for GridWatch Ireland and is meant to back any
future project that needs weather: energy, agriculture, events, logistics. It has no web or
database code and depends only on `httpx` and `tzdata`.

```bash
pip install "s33weather @ git+https://github.com/SOLAS33/gridwatch.git"
python -m s33weather point 53.35 -6.26     # blended hourly forecast for any point
python -m s33weather renewables            # national wind + solar output forecast (Ireland)
python -m s33weather warnings              # Met Éireann warnings in force
```

## Principles

- **Traceable.** Every fetch returns a `Fetch(records, provenance)`. The `Provenance` holds the
  exact URL, publisher, licence, retrieval time, the SHA-256 of the raw response, and the
  model runs that produced it. Store it next to anything you derive.
- **One shape for every source.** All sources map to `WeatherStep` / `Observation` / `Warning`
  in SI units and timezone-aware UTC. A field the source doesn't give is `None`, never
  guessed.
- **Commercially usable.** Only sources whose licence allows commercial use: Met Éireann
  (CC BY 4.0) and MET Norway (CC BY 4.0 / NLOD). Open-Meteo's free tier is non-commercial
  only and is deliberately not used.
- **Fail loudly on shape changes.** Parsers check what they read: the station-name check in
  `metie_obs`, layout checks in consumers. A wrong number is worse than a missing one.

## Modules

| Module | What it does |
|---|---|
| `sources.metie` | Met Éireann point forecast: HARMONIE-AROME 2.5 km hourly to ~54 h, then ECMWF to 10 days. Wind, gust, direction, temperature, dew point, humidity, pressure, cloud layers, **global irradiance**, precipitation with min/max/probability. Every step is tagged with the model and run that produced it. (Plain http only; https returns 404.) |
| `sources.metno` | MET Norway Locationforecast 2.0 "complete" (ECMWF-based, ~9 days). Independent second model. Needs an identifying User-Agent (set `S33W_USER_AGENT`). |
| `sources.metie_obs` | Met Éireann hourly station observations (today). km/h → m/s. Rejects replies where the station name doesn't match the one requested (unknown slugs silently return Dublin Airport). |
| `sources.metie_warnings` | Met Éireann national warnings (level, type, counties, onset/expiry). |
| `timeseries` | Puts any source on an hourly grid: linear for scalars, vector for direction; no extrapolation; precipitation never interpolated. |
| `blend` | Multi-model consensus per hour with inter-model spread; weights by source and lead time (default: HARMONIE 2:1 in its first 54 h). |
| `physics.wind` | Shear (power and log law), moist air density at hub height, IEC density-adjusted speed, parametric power curves (IEC I/II/III, offshore) with storm ride-through, Gaussian-smoothed fleet curves, capacity factor chain, vector helpers, Beaufort, compass. |
| `physics.solar` | NOAA sun position, Haurwitz clear sky, Kasten-Czeplak cloud attenuation, Erbs diffuse split, isotropic plane-of-array, NOCT cell temperature, PV output per kWp. |
| `physics.thermo` | Dew point and humidity, wind chill, heat index, heating and cooling degree hours (SEAI 15.5 °C base). |
| `energy` | `RenewableModel`: per-point weather → regional and national wind/solar MW with P10/P90 (inter-model spread, or measured error quantiles once they exist), with fittable `RenewableParams`. |
| `verify` | Bias, MAE, RMSE, normalised MAE, skill vs a reference forecast, lead-time buckets, error quantiles. |
| `calibrate` | Exhaustive grid-search fit (MAE, robust, no local minima) and least squares. Reports the data it used. |
| `points` | Ireland: 17 wind and 6 solar sampling points weighted by EirGrid regional capacity; 18 verified Met Éireann station slugs. |

## Adding a source

Write `sources/<name>.py` with `KEY`, `PUBLISHER`, `LICENCE` and
`fetch(client, location) -> Fetch` returning `WeatherStep`s, then register it in
`sources/__init__.py:FORECAST_SOURCES`. Blending, the energy model and the CLI pick it up
automatically. Add a recorded payload to `tests/fixtures` and a parser test.

Good next candidates: ECMWF open data (GRIB, CC BY 4.0), DWD ICON (open), and Copernicus
ERA5 reanalysis (needs a free account; history for calibration and back-testing).
