"""Net worth computation: aggregates Equity, Mutual Funds, Fixed Income, and EPF.

Historical net worth uses cached prices (fast, no API calls).
Projection uses per-asset growth assumptions:
  Equity + MF : trailing 1-year XIRR (falls back to 12% if insufficient history)
  Fixed Income : deterministic fi_calc maturity values
  EPF          : current monthly contribution compounding at EPF_RATE
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

from sqlalchemy import select, desc as sa_desc
from sqlalchemy.orm import Session

from sqlalchemy import select as select  # re-export for local use
from .db import BondDetail, EPFEntry, FixedIncome, Instrument, NWSnapshot, PriceCache, Transaction
from .xirr import xirr as compute_xirr
from .fi_calc import value_on as fi_value_on

# ---------- in-process TTL cache ----------
# Caches the last computed networth so repeat page loads are <10ms.
# Invalidated on any write that changes portfolio data.
_nw_cache: dict = {}
_nw_lock = threading.Lock()
_NW_TTL = 180   # seconds

def _cache_get() -> dict | None:
    with _nw_lock:
        if _nw_cache and (time.time() - _nw_cache.get("_ts", 0)) < _NW_TTL:
            return _nw_cache.get("data")
    return None

def _cache_set(data: dict) -> None:
    with _nw_lock:
        _nw_cache["data"] = data
        _nw_cache["_ts"] = time.time()

def invalidate_nw_cache() -> None:
    """Call after any write that affects net worth (import, add txn, etc.)."""
    with _nw_lock:
        _nw_cache.clear()

log = logging.getLogger(__name__)

EPF_RATE      = 8.25   # current EPF interest rate % p.a. (update when govt announces)
PROJ_MONTHS   = 60     # months to project into future (5 years — enough to see compounding shape)
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

def _equity_monthly_batch(db: Session, all_txns: list,
                           months: list[date]) -> dict[date, float]:
    """Compute equity+MF portfolio value for ALL months in one efficient pass.

    Replaces calling _monthly_equity_value(d) for each month with:
    - Single event sweep per instrument (O(events), not O(months × events))
    - One price-range DB query per instrument covering the full date span
      instead of one query per instrument per month (60× fewer DB round-trips)

    Before: ~1,500 SQL queries for 50 months × 30 instruments
    After:  ~30 SQL queries (one per instrument)
    """
    from .corporate_actions import get_split_actions, QTY_SPLIT_TYPES
    from .analytics import _txns_by_isin
    from .prices import fill_forward

    if not months:
        return {}

    eq_mf_txns = [t for t in all_txns if t.segment in ("EQ", "MF")]
    if not eq_mf_txns:
        return {m: 0.0 for m in months}

    bucket = _txns_by_isin(eq_mf_txns)
    result = {m: 0.0 for m in months}
    start_d, end_d = min(months), max(months)

    for _key, group in bucket.items():
        sym = group[0].symbol.upper()
        split_actions = get_split_actions(db, sym)

        # Build sorted event list (buys before sells on same day, splits last)
        events: list[tuple] = []
        for t in sorted(group, key=lambda t: (t.trade_date, t.id)):
            events.append((t.trade_date, 0 if t.trade_type == "buy" else 1, t))
        for ex_d, act_type, ratio in split_actions:
            events.append((ex_d, 2, (act_type, ratio)))
        events.sort(key=lambda x: (x[0], x[1]))

        # Single sweep — compute qty at each month-end
        running = 0.0
        ev_idx = 0
        month_qty: dict[date, float] = {}
        for m in sorted(months):
            while ev_idx < len(events) and events[ev_idx][0] <= m:
                _, kind, payload = events[ev_idx]
                if kind == 0:
                    running += payload.quantity
                elif kind == 1:
                    running = max(0.0, running - payload.quantity)
                else:
                    act_type, ratio = payload
                    if act_type in QTY_SPLIT_TYPES:
                        running = float(int(running * ratio))
                ev_idx += 1
            month_qty[m] = max(0.0, running)

        # Resolve price cache key
        best_isin = next((t.isin for t in group if t.isin), None)
        inst = db.get(Instrument, best_isin) if best_isin else None
        if not inst:
            continue
        cache_key = inst.yf_ticker or best_isin
        if not cache_key:
            continue

        # ONE price query for the entire date range (not one per month)
        price_rows = db.execute(
            select(PriceCache.on_date, PriceCache.close).where(
                PriceCache.key == cache_key,
                PriceCache.on_date >= start_d - timedelta(days=30),
                PriceCache.on_date <= end_d,
            )
        ).all()
        if not price_rows:
            continue
        price_map = {r.on_date: float(r.close) for r in price_rows}
        ff = fill_forward(price_map, start_d - timedelta(days=30), end_d)

        # Multiply qty × price for each month
        for m in months:
            qty = month_qty.get(m, 0.0)
            if qty <= 0:
                continue
            price = ff.get(m)
            if price is None:
                avail = [d for d in ff if d <= m]
                if avail:
                    price = ff[max(avail)]
            if price:
                result[m] += qty * price

    return result


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
    # Serve from cache if fresh — avoids the expensive historical computation on every load.
    # Uses a quick transaction count as a change fingerprint: if new data was imported
    # since the cache was built, the count changes and we recompute.
    from sqlalchemy import func as sa_func
    txn_count = db.execute(sa_func.count(Transaction.id)).scalar() or 0
    cached = _cache_get()
    if cached is not None and cached.get("_fingerprint") == txn_count:
        return cached

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

    # Build list of all months to compute
    months: list[date] = []
    d = hist_start
    while d <= today:
        months.append(d)
        d += relativedelta(months=1)

    # Batch: one DB price query per instrument covers all months (not one per month)
    eq_monthly = _equity_monthly_batch(db, all_txns, months)

    history: list[dict] = []
    for m in months:
        eq_h  = eq_monthly.get(m, 0.0)
        fi_h  = _fi_value_on(fi_records, m)
        epf_h = _epf_balance_on(epf_entries, m)
        history.append({
            "month":  m.isoformat(),
            "equity": round(eq_h, 0),
            "fi":     round(fi_h, 0),
            "epf":    round(epf_h, 0),
            "total":  round(eq_h + fi_h + epf_h, 0),
        })

    # Growth rate for equity projection — use ALL-TIME portfolio XIRR.
    # The trailing 1-year XIRR is too volatile (historical imports can produce
    # 500%+ due to recently-added old transactions), so we use the full history
    # which gives a stable, realistic long-run rate.
    # Clamped to [0%, 30%] — 30% is already very optimistic for long-run equity.
    try:
        from .analytics import portfolio_cashflows
        all_eq_mf_txns = [t for t in all_txns if t.segment in ("EQ", "MF")]
        if len(all_eq_mf_txns) >= 2:
            flows_for_rate = portfolio_cashflows(db, all_eq_mf_txns, include_fi=False)
            if (eq_val + mf_val) > 0:
                flows_for_rate.append((today, eq_val + mf_val))
            eq_growth = compute_xirr(flows_for_rate) or (EQUITY_FALLBACK_RATE / 100)
        else:
            eq_growth = EQUITY_FALLBACK_RATE / 100
    except Exception:
        eq_growth = EQUITY_FALLBACK_RATE / 100

    eq_growth = min(max(eq_growth, 0.0), 0.30)  # clamp to [0%, 30%]

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

    # ---- Total Net Worth XIRR (Personal Rate of Return across all asset classes) ----
    # Aggregates every cashflow: EQ+MF purchases/sales, FI deposits, EPF contributions,
    # bond purchases, global equity (converted to INR). Terminal value = today's total NW.
    # This answers: "at what annualized rate has every rupee I've invested been compounding?"
    nw_xirr = None
    try:
        from .analytics import portfolio_cashflows, fi_cashflows_all
        from .db import BondDetail as _BD, GlobalEquityTransaction as _GET

        all_flows: list[tuple[date, float]] = []

        # Equity + MF (INR cashflows, switches excluded)
        all_flows.extend(portfolio_cashflows(db, all_txns, include_fi=False))

        # Fixed Income (FDs/RDs) — deterministic
        all_flows.extend(fi_cashflows_all(db))

        # EPF — employee + employer contributions as outflows, current balance as terminal
        from .db import EPFEntry as _EPF
        epf_rows = db.execute(select(_EPF).order_by(_EPF.month)).scalars().all()
        for e in epf_rows:
            if e.entry_type == "contribution":
                total_contribution = e.employee_share + e.employer_share
                if total_contribution > 0:
                    all_flows.append((e.month, -total_contribution))

        # Bonds (SGBs etc.) — purchase as outflow, current value as inflow
        for bd in db.execute(select(_BD)).scalars().all():
            qty = bd.quantity or 0
            if qty > 0:
                cost = qty * (bd.purchase_price or bd.issue_price)
                ref_date = bd.purchase_date or bd.issue_date
                all_flows.append((ref_date, -cost))
                # Current value as part of terminal (already in `total`)

        # Global equity — convert USD to INR using stored exchange rates
        ge_rows = db.execute(select(_GET).order_by(_GET.trade_date)).scalars().all()
        for t in ge_rows:
            rate = t.exchange_rate or 84.0
            inr  = t.amount_usd * rate
            if t.trade_type == "buy":
                all_flows.append((t.trade_date, -inr))
            else:
                all_flows.append((t.trade_date, inr))

        # Terminal value = today's total net worth (all assets)
        if total > 0:
            all_flows.append((today, total))

        if all_flows:
            nw_xirr = compute_xirr(all_flows)
    except Exception as _e:
        log.debug("NW XIRR failed: %s", _e)

    result = {
        "current": {
            "equity": round(eq_val, 2),
            "mf":     round(mf_val, 2),
            "fi":     round(fi_val, 2),
            "epf":    round(epf_val, 2),
            "bonds":  round(bond_val, 2),
            "total":  round(total, 2),
        },
        "nw_xirr": nw_xirr,
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
        "snapshots": [
            {"date": s.snap_date.isoformat(), "amount": s.amount, "label": s.label}
            for s in db.execute(
                select(NWSnapshot).order_by(NWSnapshot.snap_date)
            ).scalars().all()
        ],
        "_fingerprint": txn_count,
    }
    _cache_set(result)
    return result


def _empty_response(today, eq, mf, fi, epf, total):
    return {
        "current": {"equity": eq, "mf": mf, "fi": fi, "epf": epf, "total": total},
        "allocation": {}, "history": [], "projection": [], "milestones": [],
        "assumptions": {}, "as_of": today.isoformat(),
    }
