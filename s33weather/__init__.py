"""s33weather - Solas33's reusable weather forecasting toolkit.

Independent of any one website: sources -> common SI-unit records with provenance ->
hourly resampling -> multi-model blending with spread -> physics (wind power, solar PV,
thermodynamics) -> verification and calibration. See README.md.
"""
from .models import Fetch, Location, Observation, Provenance, Warning, WeatherStep

__version__ = "0.1.0"
__all__ = ["Fetch", "Location", "Observation", "Provenance", "Warning", "WeatherStep", "__version__"]
