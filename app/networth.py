"""Net worth computation: aggregates Equity, Mutual Funds, Fixed Income, and EPF.

Historical net worth uses cached prices (fast, no API calls).
Projection uses per-asset growth assumptions:
  Equity + MF : trailing 1-year XIRR (falls back to 12% if insufficient history)
  Fixed Income : deterministic fi_calc maturity values
  EPF          : current monthly contribution compounding at EPF_RATE
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

from sqlalchemy import select, desc as sa_desc
from sqlalchemy.orm import Session

from .db import BondDetail, EPFEntry, FixedIncome, Instrument, PriceCache, Transaction
from .fi_calc import value_on as fi_value_on

log = logging.getLogger(__name__)

EPF_RATE      = 8.25   # current EPF interest rate % p.a. (update when govt announces)
PROJ_MONTHS   = 36     # months to project into future
EQUITY_FALLBACK_RATE = 12.0  # % p.a. if XIRR can't be computed

# Standard Indian wealth milestones (₹)
MILESTONES = [
    50_000, 1_00_000, 2_00_000, 3_00_000, 5_00_000,
    10_00_000, 25_00_000, 50_00_000, 1_00_00_000, 2_00_00_000,
]

MILESTONE_LABELS = {
    50_000:      "₹50K",
    1_00_000:    "₹1L",
    2_00_000:    "₹2L",
    3_00_000:    "₹3L",
    5_00_000:    "₹5L",
    10_00_000:   "₹10L",
    25_00_000:   "₹25L",
    50_00_000:   "₹50L",
    1_00_00_000: "₹1Cr",
    2_00_00_000: "₹2Cr",
}


# ---------- helpers ----------

def _monthly_equity_value(db: Session, d: date) -> float:
    """Approximate equity+MF portfolio value on month d using price_cache."""
    from .corporate_actions import adjusted_holding_state, get_split_actions
    from .analytics import _txns_by_isin

    txns_up_to = [
        t for t in db.execute(select(Transaction)).scalars().all()
        if t.trade_date <= d and t.segment in ("EQ", "MF")
    ]
    if not txns_up_to:
        return 0.0

    bucket = _txns_by_isin(txns_up_to)
    total = 0.0
    for _key, group in bucket.items():
        sym = group[0].symbol.upper()
        sa = get_split_actions(db, sym)
        qty, avg, _, _ = adjusted_holding_state(group, sa)
        if qty <= 0:
            continue
        best_isin = next((t.isin for t in group if t.isin), None)
        inst = db.get(Instrument, best_isin) if best_isin else None
        if not inst:
            continue
        cache_key = inst.yf_ticker or best_isin
        if not cache_key:
            continue
        # Nearest cached price ≤ d
        row = db.execute(
            select(PriceCache.close)
            .where(PriceCache.key == cache_key, PriceCache.on_date <= d)
            .order_by(sa_desc(PriceCache.on_date)).limit(1)
        ).scalar()
        if row:
            total += qty * float(row)
    return total


def _epf_balance_on(entries: list[EPFEntry], on_date: date) -> float:
    """Running EPF balance up to on_date (contributions + interest credited)."""
    total = 0.0
    for e in entries:
        if e.month <= on_date:
            total += e.employee_share + e.employer_share  # interest rows also included
            total -= (e.employee_withdrawal + e.employer_withdrawal)
    return max(0.0, total)


def _fi_value_on(records: list[FixedIncome], on_date: date) -> float:
    return sum(fi_value_on(fi, on_date) for fi in records if fi.start_date <= on_date)


# ---------- main computation ----------

def compute_networth(db: Session) -> dict:
    today = date.today()

    # Current values
    from .analytics import compute_holdings, compute_fi_summary
    from .xirr import xirr as compute_xirr

    holdings   = compute_holdings(db)
    eq_val     = sum(h.current_value or 0 for h in holdings if h.segment == "EQ")
    mf_val     = sum(h.current_value or 0 for h in holdings if h.segment == "MF")
    fi_records = db.execute(select(FixedIncome)).scalars().all()
    fi_val     = _fi_value_on(fi_records, today)
    epf_entries = db.execute(select(EPFEntry).order_by(EPFEntry.month)).scalars().all()
    epf_val    = _epf_balance_on(epf_entries, today)
    total      = eq_val + mf_val + fi_val + epf_val

    # Allocation percentages
    def pct(v): return round(v / total * 100, 1) if total else 0

    # Historical monthly series (use first transaction date as start)
    all_txns = db.execute(select(Transaction).order_by(Transaction.trade_date)).scalars().all()
    if not all_txns:
        return _empty_response(today, eq_val, mf_val, fi_val, epf_val, total)

    hist_start = min(t.trade_date for t in all_txns).replace(day=1)
    history: list[dict] = []
    d = hist_start
    while d <= today:
        eq_h   = _monthly_equity_value(db, d)
        fi_h   = _fi_value_on(fi_records, d)
        epf_h  = _epf_balance_on(epf_entries, d)
        history.append({
            "month":  d.isoformat(),
            "equity": round(eq_h, 0),
            "fi":     round(fi_h, 0),
            "epf":    round(epf_h, 0),
            "total":  round(eq_h + fi_h + epf_h, 0),
        })
        d += relativedelta(months=1)

    # Growth rate for equity projection (trailing 1-year XIRR, else fallback)
    try:
        one_yr_ago = today - timedelta(days=365)
        txns_1y = [t for t in all_txns if t.trade_date >= one_yr_ago and t.segment in ("EQ", "MF")]
        if len(txns_1y) >= 2:
            from .analytics import portfolio_cashflows
            flows = portfolio_cashflows(db, txns_1y, include_fi=False)
            if (eq_val + mf_val) > 0:
                flows.append((today, eq_val + mf_val))
            eq_growth = compute_xirr(flows) or (EQUITY_FALLBACK_RATE / 100)
        else:
            eq_growth = EQUITY_FALLBACK_RATE / 100
    except Exception:
        eq_growth = EQUITY_FALLBACK_RATE / 100

    eq_growth = min(max(eq_growth, 0.0), 0.50)  # clamp to [0%, 50%]

    # EPF monthly contribution (use average of last 3 months)
    recent_epf = sorted(
        [e for e in epf_entries if e.entry_type == "contribution"],
        key=lambda e: e.month, reverse=True
    )[:3]
    epf_monthly = (
        sum(e.employee_share + e.employer_share for e in recent_epf) / len(recent_epf)
        if recent_epf else 0
    )
    epf_rate_monthly = (EPF_RATE / 100) / 12

    # Project PROJ_MONTHS into the future
    projection: list[dict] = []
    proj_eq  = eq_val + mf_val
    proj_fi  = fi_val
    proj_epf = epf_val
    proj_d   = today.replace(day=1) + relativedelta(months=1)

    for _ in range(PROJ_MONTHS):
        proj_eq  = proj_eq  * (1 + eq_growth / 12)
        proj_fi  = _fi_value_on(fi_records, proj_d)
        proj_epf = (proj_epf + epf_monthly) * (1 + epf_rate_monthly)
        proj_total = proj_eq + proj_fi + proj_epf
        projection.append({
            "month":       proj_d.isoformat(),
            "equity":      round(proj_eq, 0),
            "fi":          round(proj_fi, 0),
            "epf":         round(proj_epf, 0),
            "total":       round(proj_total, 0),
            "is_projected": True,
        })
        proj_d += relativedelta(months=1)

    # Milestone detection (historical + projected)
    all_points = history + projection
    milestone_hits: list[dict] = []
    prev_total = 0.0
    for point in all_points:
        t = point["total"]
        for m in MILESTONES:
            if prev_total < m <= t:
                milestone_hits.append({
                    "amount":      m,
                    "label":       MILESTONE_LABELS.get(m, f"₹{m:,}"),
                    "month":       point["month"],
                    "is_future":   point.get("is_projected", False),
                })
        prev_total = t

    # Bond holdings (from bond_details — self-contained, not in transactions table)
    from .bond_analytics import compute_bond_holdings
    bond_rows = compute_bond_holdings(db, today)
    bond_val  = sum(b["current_value"] or 0 for b in bond_rows)
    total    += bond_val   # add to total net worth

    # Bond type → asset category mapping
    _BOND_CAT = {"SGB": "gold", "corporate": "debt", "g-sec": "debt", "other": "other"}

    # Asset-class breakdown (Equity / Debt / Gold / Hybrid / Cash / Silver / EPF)
    from .categorizer import category_breakdown, ensure_all_categorised
    ensure_all_categorised(db)

    holdings_isin_val: dict[str, float] = {
        h.isin: (h.current_value or 0)
        for h in holdings if h.isin and (h.current_value or 0) > 0
    }
    cat_breakdown = category_breakdown(db, holdings_isin_val, fi_val, epf_val)

    # Add bond values by type
    for b in bond_rows:
        val = b["current_value"] or 0
        if val > 0:
            cat = _BOND_CAT.get(b["bond_type"], "other")
            cat_breakdown[cat] = round(cat_breakdown.get(cat, 0) + val, 2)
    cat_pcts = {k: round(v / total * 100, 1) if total else 0
                for k, v in cat_breakdown.items()}

    CAT_COLORS = {
        "equity":  "#58a6ff",
        "debt":    "#d29922",
        "gold":    "#f0c14b",
        "hybrid":  "#a371f7",
        "cash":    "#8b949e",
        "silver":  "#c0c0c0",
        "epf":     "#3fb950",
        "other":   "#444c56",
    }

    return {
        "current": {
            "equity": round(eq_val, 2),
            "mf":     round(mf_val, 2),
            "fi":     round(fi_val, 2),
            "epf":    round(epf_val, 2),
            "bonds":  round(bond_val, 2),
            "total":  round(total, 2),
        },
        "allocation": {
            "equity": pct(eq_val),
            "mf":     pct(mf_val),
            "fi":     pct(fi_val),
            "epf":    pct(epf_val),
        },
        "by_asset_class": {
            "breakdown": cat_breakdown,
            "pct":       cat_pcts,
            "colors":    {k: CAT_COLORS.get(k, "#444c56") for k in cat_breakdown},
        },
        "history":    history,
        "projection": projection,
        "milestones": milestone_hits,
        "assumptions": {
            "equity_growth_rate": round(eq_growth * 100, 2),
            "epf_rate":           EPF_RATE,
            "epf_monthly":        round(epf_monthly, 2),
        },
        "as_of": today.isoformat(),
    }


def _empty_response(today, eq, mf, fi, epf, total):
    return {
        "current": {"equity": eq, "mf": mf, "fi": fi, "epf": epf, "total": total},
        "allocation": {}, "history": [], "projection": [], "milestones": [],
        "assumptions": {}, "as_of": today.isoformat(),
    }
