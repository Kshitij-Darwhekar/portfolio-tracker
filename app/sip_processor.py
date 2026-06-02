"""SIP Schedule processor — automatic NAV-based transaction generation.

How it works:
1. For each active SIPSchedule, generate all due SIP dates between
   start_date (or last_synced_date) and today.
2. For each date, fetch the official AMFI NAV from mfapi.in.
3. If the SIP date is a market holiday (no NAV), try up to 5 next days
   — the same logic fund houses use for allotment.
4. Calculate units = amount / NAV.
5. Return a list of preview items for user review before committing.

NAV accuracy: mfapi.in sources data directly from AMFI. Verified to match
CAS-imported NAVs to 4 decimal places across all tested dates.

Deduplication: a transaction is considered already imported if a transaction
exists for the same ISIN within ±3 days of the expected date with the same
segment='MF'. This prevents double-counting if the same period was also
imported from a CAS PDF.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Instrument, SIPSchedule, Transaction
from .prices import fetch_mf_history, resolve_mf_scheme_code

log = logging.getLogger(__name__)

_NAV_CACHE: dict[str, dict[date, float]] = {}   # scheme_code → {date: nav}

# Stamp duty rate on MF purchases — fixed by SEBI for all funds (introduced July 1, 2020)
# Deducted before unit allotment: net_amount = gross × (1 - 0.00005)
# E.g. ₹3,000 → stamp duty ₹0.15 → net investable ₹2,999.85
_STAMP_DUTY_RATE = 0.00005


def _net_investable(gross: float) -> float:
    """Amount after stamp duty deduction (what the fund actually invests in units)."""
    stamp_duty = round(gross * _STAMP_DUTY_RATE, 2)
    return round(gross - stamp_duty, 2)


def _nav_history(isin: str) -> dict[date, float]:
    """Fetch full NAV history for a fund, cached in memory."""
    scheme_code = resolve_mf_scheme_code(isin)
    if not scheme_code:
        return {}
    if scheme_code in _NAV_CACHE:
        return _NAV_CACHE[scheme_code]
    history = fetch_mf_history(scheme_code)   # already in prices.py
    _NAV_CACHE[scheme_code] = history
    return history


def _allotment_nav(isin: str, sip_date: date) -> tuple[date | None, float | None]:
    """Return (allotment_date, NAV) for a given SIP date.

    If sip_date is a market holiday, returns the next available trading day.
    Returns (None, None) if no NAV found within 5 days.
    """
    nav_history = _nav_history(isin)
    if not nav_history:
        return None, None
    for offset in range(5):
        check = sip_date + timedelta(days=offset)
        if check in nav_history:
            return check, nav_history[check]
    return None, None


def _already_imported(db: Session, isin: str, expected_date: date,
                       amount: float) -> bool:
    """Check if a transaction for this SIP already exists (from CAS or earlier sync)."""
    window_start = expected_date - timedelta(days=3)
    window_end   = expected_date + timedelta(days=5)
    existing = db.execute(
        select(Transaction).where(
            Transaction.isin    == isin,
            Transaction.segment == "MF",
            Transaction.trade_type == "buy",
            Transaction.trade_date >= window_start,
            Transaction.trade_date <= window_end,
        )
    ).scalars().all()
    if not existing:
        return False
    # Match by approximate amount (within 1%)
    for t in existing:
        txn_amount = t.quantity * t.price
        if abs(txn_amount - amount) / amount < 0.01:
            return True
    return False


def generate_sip_dates(schedule: SIPSchedule, today: date) -> list[date]:
    """All SIP dates for a schedule that are due but not yet processed."""
    # Start from the day AFTER last_synced_date, or from start_date
    from_date = schedule.start_date
    if schedule.last_synced_date and schedule.last_synced_date >= schedule.start_date:
        # Start from the month after last synced
        from_date = schedule.last_synced_date + relativedelta(months=1)
        from_date = from_date.replace(day=min(schedule.sip_day, 28))

    end = min(schedule.end_date or today, today)
    if from_date > end:
        return []

    dates: list[date] = []
    # Build first candidate date
    d = from_date.replace(day=min(schedule.sip_day, 28))
    if d < from_date:
        d += relativedelta(months=1)
        d = d.replace(day=min(schedule.sip_day, 28))

    while d <= end:
        dates.append(d)
        d += relativedelta(months=1)
        d = d.replace(day=min(schedule.sip_day, 28))

    return dates


def preview_sip_schedule(db: Session, schedule: SIPSchedule,
                         today: date | None = None) -> list[dict]:
    """Generate preview of transactions that WOULD be created — no DB writes.

    Returns list of dicts with status:
      'new'       — will be created on confirm
      'skipped'   — already imported (from CAS or earlier sync)
      'no_nav'    — NAV unavailable (future date or data gap)
    """
    today = today or date.today()
    due_dates = generate_sip_dates(schedule, today)
    preview: list[dict] = []

    for sip_date in due_dates:
        allotment_date, nav = _allotment_nav(schedule.isin, sip_date)

        if nav is None:
            preview.append({
                "sip_date":       sip_date.isoformat(),
                "allotment_date": None,
                "nav":            None,
                "units":          None,
                "amount":         schedule.amount,
                "status":         "no_nav",
                "note":           "NAV not available (future date or data gap)",
            })
            continue

        net   = _net_investable(schedule.amount)
        units = round(net / nav, 6)
        holiday_adjusted = allotment_date != sip_date

        stamp_duty = round(schedule.amount * _STAMP_DUTY_RATE, 2)

        if _already_imported(db, schedule.isin, allotment_date, schedule.amount):
            preview.append({
                "sip_date":       sip_date.isoformat(),
                "allotment_date": allotment_date.isoformat(),
                "nav":            nav,
                "units":          units,
                "amount":         schedule.amount,
                "status":         "skipped",
                "note":           "Already imported (from CAS or previous sync)",
            })
        else:
            note_parts = []
            if holiday_adjusted:
                note_parts.append(f"Holiday → allotted on {allotment_date}")
            note_parts.append(f"Stamp duty ₹{stamp_duty} (0.005%)")
            preview.append({
                "sip_date":       sip_date.isoformat(),
                "allotment_date": allotment_date.isoformat(),
                "nav":            nav,
                "units":          units,
                "amount":         schedule.amount,
                "net_amount":     net,
                "stamp_duty":     stamp_duty,
                "status":         "new",
                "note":           " · ".join(note_parts),
            })

    return preview


def commit_sip_schedule(db: Session, schedule: SIPSchedule,
                        today: date | None = None) -> dict:
    """Commit all 'new' transactions from a schedule preview to the DB.

    Only processes items with status='new'. Skips already-imported and
    no_nav items. Updates last_synced_date on the schedule.
    """
    today = today or date.today()
    preview = preview_sip_schedule(db, schedule, today)

    inserted = 0
    skipped  = 0
    no_nav   = 0
    last_date: date | None = None

    for item in preview:
        if item["status"] == "no_nav":
            no_nav += 1
            continue
        if item["status"] == "skipped":
            skipped += 1
            # Still advance last_synced_date past skipped items
            ad = date.fromisoformat(item["allotment_date"])
            if last_date is None or ad > last_date:
                last_date = ad
            continue

        # status == 'new' → create transaction
        allotment_date = date.fromisoformat(item["allotment_date"])
        trade_id = f"SIP|{schedule.isin}|{item['sip_date']}|{item['nav']:.4f}"

        # Check trade_id not already in DB (extra safety)
        exists = db.execute(
            select(Transaction).where(Transaction.trade_id == trade_id)
        ).scalar_one_or_none()
        if exists:
            skipped += 1
        else:
            stamp_duty = round(schedule.amount * _STAMP_DUTY_RATE, 2)
            txn = Transaction(
                trade_date   = allotment_date,
                symbol       = schedule.isin,
                isin         = schedule.isin,
                exchange     = "AMFI",
                segment      = "MF",
                trade_type   = "buy",
                quantity     = item["units"],
                price        = item["nav"],
                fees         = stamp_duty,   # stamp duty stored as fees for transparency
                notes        = f"SIP auto-import (schedule #{schedule.id})"
                               + (f" | {item['note']}" if item.get("note") else ""),
                source       = "sip_auto",
                folio        = schedule.folio,
                trade_id     = trade_id,
            )
            db.add(txn)
            inserted += 1

        if last_date is None or allotment_date > last_date:
            last_date = allotment_date

    db.flush()

    # Update last_synced_date
    if last_date:
        schedule.last_synced_date = last_date
    db.commit()

    # Ensure the instrument record exists for NAV fetching
    if inserted > 0:
        from .prices import get_or_create_instrument
        get_or_create_instrument(
            db, symbol=schedule.isin, isin=schedule.isin,
            segment="MF", exchange=None
        )
        db.commit()

    return {
        "inserted": inserted,
        "skipped":  skipped,
        "no_nav":   no_nav,
        "last_synced_date": last_date.isoformat() if last_date else None,
    }
