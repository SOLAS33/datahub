"""Model calibration against measured output, without heavy dependencies.

`fit_grid` does an exhaustive search over a small parameter grid, minimising mean absolute
error (robust to the occasional bad meter reading). With 2-3 parameters and a few thousand
pairs this is fast in pure Python and, unlike gradient methods, can't land in a silly local
minimum. `linear_fit` gives ordinary least squares y = a + b x for simple bias correction.

Every fit returns the data it used (n, period) so a published model can say exactly what it
was calibrated on.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Fit:
    params: dict[str, float]
    mae: float
    n: int
    baseline_mae: float | None = None   # MAE of the prior (uncalibrated) parameters on the same data
    notes: str = ""
    grid: dict[str, list[float]] = field(default_factory=dict)


def fit_grid(predict: Callable[[dict, object], float], cases: list[tuple[object, float]], grid: dict[str, list[float]],
             prior: dict[str, float] | None = None) -> Fit | None:
    """predict(params, case_input) -> value; cases = [(case_input, observed)]."""
    if not cases:
        return None
    names = list(grid)
    best: tuple[float, dict] | None = None
    for combo in itertools.product(*(grid[n] for n in names)):
        p = dict(zip(names, combo))
        mae = sum(abs(predict(p, x) - y) for x, y in cases) / len(cases)
        if best is None or mae < best[0]:
            best = (mae, p)
    base = sum(abs(predict(prior, x) - y) for x, y in cases) / len(cases) if prior else None
    return Fit(params=best[1], mae=best[0], n=len(cases), baseline_mae=base, grid=grid)


def linear_fit(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    pts = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pts)
    if n < 3:
        return None
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    if sxx == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in pts) / sxx
    return my - b * mx, b
