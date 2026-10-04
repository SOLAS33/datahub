"""Source adapters. Each module exposes KEY, PUBLISHER, LICENCE and a `fetch(...)` that
returns a models.Fetch (records + provenance). Forecast sources share the signature
fetch(client, location) so they can be swapped or added without touching consumers."""
from . import metie, metie_obs, metie_warnings, metno

FORECAST_SOURCES = {metie.KEY: metie, metno.KEY: metno}

__all__ = ["FORECAST_SOURCES", "metie", "metie_obs", "metie_warnings", "metno"]
