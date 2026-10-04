"""Forecast verification. A forecast that is sold must publish how good it is; these are the
standard measures, computed from matched (forecast, observed) pairs.

* bias (mean error), MAE, RMSE, and normalised MAE (as % of a reference such as capacity)
* skill score vs a reference forecast (persistence, climatology, or someone else's
  forecast): SS = 1 - MAE_model / MAE_reference (1 = perfect, 0 = no better, <0 = worse)
* empirical quantiles of the error, by lead-time bucket - used to put honest P10/P90 bands
  around new forecasts.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

LEAD_BUCKETS = [(0, 6, "0-6 h"), (6, 24, "6-24 h"), (24, 48, "Day 2"), (48, 72, "Day 3"), (72, 120, "Days 4-5"), (120, 240, "Days 6-10")]


@dataclass
class Scores:
    n: int
    bias: float | None
    mae: float | None
    rmse: float | None
    nmae_pct: float | None = None
    skill_vs_ref: float | None = None
    ref_mae: float | None = None


def scores(pairs: list[tuple[float, float]], normaliser: float | None = None, ref_pairs: list[tuple[float, float]] | None = None) -> Scores:
    """pairs: [(forecast, observed)]. ref_pairs: the reference forecast on the SAME cases."""
    pairs = [(f, o) for f, o in pairs if f is not None and o is not None]
    n = len(pairs)
    if not n:
        return Scores(0, None, None, None)
    errs = [f - o for f, o in pairs]
    mae = sum(abs(e) for e in errs) / n
    s = Scores(n=n, bias=sum(errs) / n, mae=mae, rmse=math.sqrt(sum(e * e for e in errs) / n),
               nmae_pct=(100 * mae / normaliser) if normaliser else None)
    if ref_pairs:
        rp = [(f, o) for f, o in ref_pairs if f is not None and o is not None]
        if rp:
            s.ref_mae = sum(abs(f - o) for f, o in rp) / len(rp)
            s.skill_vs_ref = (1 - mae / s.ref_mae) if s.ref_mae else None
    return s


def bucket(lead_h: float) -> str | None:
    for lo, hi, name in LEAD_BUCKETS:
        if lo <= lead_h < hi:
            return name
    return None


def quantile(values: list[float], q: float) -> float | None:
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    pos = (len(v) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def error_quantiles(errors: list[float], qs=(0.1, 0.5, 0.9)) -> dict[float, float | None]:
    return {q: quantile(errors, q) for q in qs}
