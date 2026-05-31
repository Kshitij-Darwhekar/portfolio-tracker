from __future__ import annotations

from datetime import date
from typing import Iterable

from scipy.optimize import brentq


def _npv(rate: float, flows: list[tuple[date, float]], t0: date) -> float:
    return sum(amt / (1.0 + rate) ** ((d - t0).days / 365.0) for d, amt in flows)


def xirr(cashflows: Iterable[tuple[date, float]]) -> float | None:
    """Annualized internal rate of return for irregular cashflows.

    Outflows must be negative, inflows positive. Needs at least one of each.
    Returns None if no rate can be found.
    """
    flows = [(d, float(a)) for d, a in cashflows if a != 0]
    if len(flows) < 2:
        return None
    if not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        return None

    flows.sort(key=lambda x: x[0])
    t0 = flows[0][0]

    f = lambda r: _npv(r, flows, t0)

    # Try brentq first across a wide bracket
    lo, hi = -0.999, 100.0
    try:
        f_lo, f_hi = f(lo), f(hi)
        if f_lo * f_hi < 0:
            return brentq(f, lo, hi, maxiter=200, xtol=1e-7)
    except (ValueError, OverflowError):
        pass

    # Newton-Raphson fallback
    rate = 0.1
    for _ in range(200):
        try:
            v = f(rate)
            # numerical derivative
            h = 1e-6
            dv = (f(rate + h) - v) / h
            if dv == 0:
                break
            new_rate = rate - v / dv
            if abs(new_rate - rate) < 1e-7:
                return new_rate
            # keep within sane bounds
            if new_rate < -0.999:
                new_rate = -0.999 + (rate + 0.999) / 2
            rate = new_rate
        except (OverflowError, ZeroDivisionError):
            return None
    return None
