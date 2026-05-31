"""Fixed-income interest math: FD (cumulative & non-cumulative) and RD.

All functions are pure Python — no DB access, no external APIs.
Interest calculations follow Indian bank conventions (RBI guidelines).

Compounding frequencies:
  quarterly    → n = 4   (default for Indian banks; RBI mandates this for FDs)
  monthly      → n = 12
  half_yearly  → n = 2
  annual       → n = 1
  simple       → no compounding; used for some short-tenure products

Non-cumulative FDs pay interest periodically (monthly/quarterly/annual)
rather than accumulating. The principal is returned at maturity.
"""

from __future__ import annotations

from datetime import date, timedelta
from dateutil.relativedelta import relativedelta


_COMPOUNDING_N = {
    "quarterly":   4,
    "monthly":     12,
    "half_yearly": 2,
    "annual":      1,
}

_PAYOUT_MONTHS = {
    "monthly":   1,
    "quarterly": 3,
    "annual":    12,
}


def _n(compounding: str) -> int:
    return _COMPOUNDING_N.get(compounding, 4)


def _years(start: date, end: date) -> float:
    return (end - start).days / 365.0


# ---------- Cumulative FD ----------

def fd_cum_maturity(principal: float, rate: float, start: date, maturity: date,
                    compounding: str = "quarterly") -> float:
    """Maturity value of a cumulative FD: P × (1 + r/n)^(n×t)."""
    t = _years(start, maturity)
    if t <= 0:
        return principal
    r = rate / 100.0
    if compounding == "simple":
        return principal * (1 + r * t)
    n = _n(compounding)
    return principal * (1 + r / n) ** (n * t)


def fd_cum_value_on(principal: float, rate: float, start: date, maturity: date,
                    on_date: date, compounding: str = "quarterly") -> float:
    """Current value of a cumulative FD on a given date (principal + accrued interest)."""
    if on_date <= start:
        return principal
    effective_end = min(on_date, maturity)
    return fd_cum_maturity(principal, rate, start, effective_end, compounding)


# ---------- Non-cumulative FD ----------

def fd_non_cum_payout(principal: float, rate: float, payout_frequency: str = "quarterly") -> float:
    """Interest paid out per payout period for a non-cumulative FD."""
    months = _PAYOUT_MONTHS.get(payout_frequency, 3)
    return principal * (rate / 100.0) * (months / 12.0)


def fd_non_cum_interest_earned(principal: float, rate: float, start: date,
                                maturity: date, on_date: date,
                                payout_frequency: str = "quarterly") -> float:
    """Total interest paid out up to on_date (already received by the depositor)."""
    if on_date <= start:
        return 0.0
    months = _PAYOUT_MONTHS.get(payout_frequency, 3)
    effective_end = min(on_date, maturity)
    elapsed_months = (effective_end.year - start.year) * 12 + (effective_end.month - start.month)
    periods = elapsed_months // months
    return fd_non_cum_payout(principal, rate, payout_frequency) * periods


# ---------- Recurring Deposit ----------

def rd_tenure_months(start: date, maturity: date) -> int:
    delta = relativedelta(maturity, start)
    return delta.years * 12 + delta.months


def rd_maturity(instalment: float, rate: float, start: date, maturity: date,
                initial_deposit: float = 0.0) -> float:
    """Maturity value of an RD using Indian bank quarterly-compounding formula.

    Each instalment M deposited in month k earns interest for (n-k) months:
      contribution_k = M × (1 + r/4)^((n-k)/3)
    Total maturity = Σ contributions + initial_deposit component (if any)

    initial_deposit: optional lump-sum deposited on start_date that earns
    compound interest for the full tenure (treated as a small cumulative FD).
    """
    n = rd_tenure_months(start, maturity)
    if n <= 0:
        return instalment
    r = rate / 100.0
    quarterly_rate = r / 4.0

    # Monthly instalment component
    total = 0.0
    for k in range(n):
        remaining_months = n - k
        quarters = remaining_months / 3.0
        total += instalment * (1 + quarterly_rate) ** quarters

    # Initial lump-sum component (compounds for the full tenure)
    if initial_deposit and initial_deposit > 0:
        t = _years(start, maturity)
        total += fd_cum_maturity(initial_deposit, rate, start, maturity, "quarterly")

    return total


def rd_value_on(instalment: float, rate: float, start: date, maturity: date,
                on_date: date, initial_deposit: float = 0.0) -> float:
    """Current value of an RD on a given date = value of all instalments paid so far.

    Instalment counting: the first instalment is paid ON the start date (month 0).
    For on_date in month M (where M >= start month), instalments paid = M - start + 1.
    Example: start=March, on_date=May → (5-3)+1 = 3 instalments (Mar, Apr, May).
    """
    if on_date < start:
        return 0.0
    effective_end = min(on_date, maturity)
    n_total = rd_tenure_months(start, maturity)
    # +1 because the start month itself is instalment 0 (paid on start date)
    n_elapsed = min(
        (effective_end.year - start.year) * 12 + (effective_end.month - start.month) + 1,
        n_total
    )
    r = rate / 100.0
    quarterly_rate = r / 4.0

    # Monthly instalment component
    total = 0.0
    for k in range(n_elapsed):
        held_months = n_elapsed - k
        quarters = held_months / 3.0
        total += instalment * (1 + quarterly_rate) ** quarters

    # Initial deposit component (accruing since start_date, separate lump sum only)
    if initial_deposit and initial_deposit > 0:
        total += fd_cum_value_on(initial_deposit, rate, start, maturity, on_date, "quarterly")

    return total


# ---------- Unified interface ----------

def current_value(fi) -> float:
    """Current market-equivalent value of a FixedIncome record today."""
    today = date.today()
    return value_on(fi, today)


def value_on(fi, on_date: date) -> float:
    """Value of a FixedIncome record on a given date."""
    if fi.fi_type == "FD_CUM":
        return fd_cum_value_on(fi.amount, fi.interest_rate, fi.start_date,
                                fi.maturity_date, on_date, fi.compounding)
    elif fi.fi_type == "FD_NON_CUM":
        # Non-cumulative: principal is intact; track cumulative interest separately
        if on_date >= fi.maturity_date:
            return fi.amount  # principal returned at maturity
        return fi.amount      # principal unchanged while active
    else:  # RD
        return rd_value_on(fi.amount, fi.interest_rate, fi.start_date,
                           fi.maturity_date, on_date,
                           initial_deposit=fi.initial_deposit or 0.0)


def maturity_value(fi) -> float:
    """Final value at maturity date."""
    if fi.fi_type == "FD_CUM":
        return fd_cum_maturity(fi.amount, fi.interest_rate, fi.start_date,
                                fi.maturity_date, fi.compounding)
    elif fi.fi_type == "FD_NON_CUM":
        return fi.amount  # principal returned; interest was paid out periodically
    else:  # RD
        return rd_maturity(fi.amount, fi.interest_rate, fi.start_date, fi.maturity_date,
                           initial_deposit=fi.initial_deposit or 0.0)


def interest_earned_total(fi, on_date: date | None = None) -> float:
    """Total interest earned/accrued up to on_date (defaults to today)."""
    on_date = on_date or date.today()
    if fi.fi_type == "FD_CUM":
        v = fd_cum_value_on(fi.amount, fi.interest_rate, fi.start_date,
                             fi.maturity_date, on_date, fi.compounding)
        return v - fi.amount
    elif fi.fi_type == "FD_NON_CUM":
        return fd_non_cum_interest_earned(fi.amount, fi.interest_rate,
                                          fi.start_date, fi.maturity_date,
                                          on_date, fi.payout_frequency or "quarterly")
    else:  # RD
        invested = 0.0
        start = fi.start_date
        n_elapsed = min(
            (min(on_date, fi.maturity_date).year - start.year) * 12 +
            (min(on_date, fi.maturity_date).month - start.month),
            rd_tenure_months(start, fi.maturity_date)
        )
        invested = fi.amount * max(0, n_elapsed)
        return rd_value_on(fi.amount, fi.interest_rate, fi.start_date,
                           fi.maturity_date, on_date) - invested


def interest_this_fy(fi, today: date | None = None) -> float:
    """Interest earned within the current Indian financial year (Apr 1 – today).

    For cumulative FD: accrued interest delta between FY start and today.
    For non-cumulative FD: payouts received within the FY.
    For RD: value delta between FY start and today minus new instalments paid.
    """
    today = today or date.today()
    fy_year = today.year if today.month >= 4 else today.year - 1
    fy_start = date(fy_year, 4, 1)

    if fi.start_date >= today:
        return 0.0

    eff_fy_start = max(fi.start_date, fy_start)

    if fi.fi_type == "FD_CUM":
        v_now  = fd_cum_value_on(fi.amount, fi.interest_rate, fi.start_date,
                                  fi.maturity_date, today, fi.compounding)
        v_then = fd_cum_value_on(fi.amount, fi.interest_rate, fi.start_date,
                                  fi.maturity_date, eff_fy_start, fi.compounding)
        return max(0.0, v_now - v_then)

    elif fi.fi_type == "FD_NON_CUM":
        total_now  = fd_non_cum_interest_earned(fi.amount, fi.interest_rate,
                                                fi.start_date, fi.maturity_date,
                                                today, fi.payout_frequency or "quarterly")
        total_then = fd_non_cum_interest_earned(fi.amount, fi.interest_rate,
                                                fi.start_date, fi.maturity_date,
                                                eff_fy_start, fi.payout_frequency or "quarterly")
        return max(0.0, total_now - total_then)

    else:  # RD
        v_now  = rd_value_on(fi.amount, fi.interest_rate, fi.start_date,
                              fi.maturity_date, today)
        v_then = rd_value_on(fi.amount, fi.interest_rate, fi.start_date,
                              fi.maturity_date, eff_fy_start)
        # Subtract new instalments paid within the FY.
        # n_now uses +1 (start month is instalment 0).
        # n_then does NOT use +1 — it counts months paid strictly before the FY start month.
        n_now  = min((today.year - fi.start_date.year) * 12 +
                     (today.month - fi.start_date.month) + 1,
                     rd_tenure_months(fi.start_date, fi.maturity_date))
        n_then = max(0, (eff_fy_start.year - fi.start_date.year) * 12 +
                        (eff_fy_start.month - fi.start_date.month))
        new_instalments = fi.amount * max(0, n_now - n_then)
        return max(0.0, (v_now - v_then) - new_instalments)


def cashflows_for_xirr(fi) -> list[tuple[date, float]]:
    """Generate (date, amount) cashflows for XIRR inclusion in portfolio XIRR.

    Outflows are negative (money leaving your pocket), inflows are positive.
    """
    today = date.today()
    flows: list[tuple[date, float]] = []

    if fi.fi_type == "FD_CUM":
        flows.append((fi.start_date, -fi.amount))
        if fi.maturity_date <= today:
            # Already matured — use actual maturity value as historical inflow
            flows.append((fi.maturity_date, maturity_value(fi)))
        else:
            # Not yet matured — use today's accrued value as terminal inflow
            flows.append((today, fd_cum_value_on(fi.amount, fi.interest_rate,
                                                  fi.start_date, fi.maturity_date,
                                                  today, fi.compounding)))

    elif fi.fi_type == "FD_NON_CUM":
        flows.append((fi.start_date, -fi.amount))
        freq = fi.payout_frequency or "quarterly"
        months = _PAYOUT_MONTHS.get(freq, 3)
        payout = fd_non_cum_payout(fi.amount, fi.interest_rate, freq)
        # Add each payout that has occurred up to today
        d = fi.start_date + relativedelta(months=months)
        while d <= min(today, fi.maturity_date):
            flows.append((d, payout))
            d = d + relativedelta(months=months)
        # Principal return at maturity (or today if matured)
        if fi.maturity_date <= today:
            flows.append((fi.maturity_date, fi.amount))
        else:
            flows.append((today, fi.amount))  # treat principal as terminal value today

    else:  # RD
        init = fi.initial_deposit or 0.0
        if init > 0:
            flows.append((fi.start_date, -init))   # lump-sum outflow on day 0
        n = rd_tenure_months(fi.start_date, fi.maturity_date)
        for k in range(n):
            instalment_date = fi.start_date + relativedelta(months=k)
            if instalment_date > today:
                break
            flows.append((instalment_date, -fi.amount))
        if fi.maturity_date <= today:
            flows.append((fi.maturity_date, maturity_value(fi)))
        else:
            flows.append((today, rd_value_on(fi.amount, fi.interest_rate,
                                             fi.start_date, fi.maturity_date, today,
                                             initial_deposit=init)))

    return flows
