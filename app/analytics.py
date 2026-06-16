"""Portfolio analytics: holdings, XIRR, equity curve, benchmark comparison."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .corporate_actions import (
    adjusted_holding_state,
    cumulative_factor,
    dividend_cashflows,
    get_split_actions,
    qty_held_on,
)
from .db import CorporateAction, FixedIncome, Instrument, Transaction
from .fi_calc import (
    cashflows_for_xirr as fi_cashflows,
    current_value as fi_current_value,
    interest_earned_total,
    interest_this_fy,
    maturity_value as fi_maturity_value,
)
from .prices import (
    BENCHMARKS,
    INSTRUMENT_META_OVERRIDES,
    fetch_benchmark_series,
    fill_forward,
    get_close_series,
    get_or_create_instrument,
    latest_close,
)
from .xirr import xirr

import threading as _threading
import time as _time

# ---------- analytics result cache ----------
# compute_holdings and compute_equity_curve are pure (derived) computations that
# several endpoints re-run on every page load (the scipy XIRR solver + per-holding
# queries cost ~0.4s; the curve ~1.5s). Cache results in-process, keyed by a cheap
# data fingerprint: any write — add/edit/delete txn, import, corporate action, or
# Refresh prices — changes the fingerprint and forces a recompute. A short TTL
# backstops the rare in-place edit. IN-MEMORY ONLY — never touches the DB, so no
# data can be lost; the worst failure mode is a briefly-stale number.
_ANALYTICS_TTL = 1800  # 30 min — long, because the fingerprint busts the cache on any data change
_holdings_cache: dict = {}
_curve_cache: dict = {}
_generic_cache: dict = {}
_holdings_lock = _threading.Lock()
_curve_lock = _threading.Lock()
_generic_lock = _threading.Lock()

def cached_call(key, db: Session, compute):
    """Generic result cache for read endpoints (summary, xirr-analysis, realized-pnl,
    data-quality, …). `key` must capture every arg that affects the result (segment,
    period, dates). Auto-invalidates via the same data fingerprint as holdings/curve.
    Computed outside the lock so warm hits never block."""
    fp = _data_fingerprint(db)
    with _generic_lock:
        ent = _generic_cache.get(key)
        if ent and ent["fp"] == fp and (_time.time() - ent["ts"]) < _ANALYTICS_TTL:
            return ent["data"]
    data = compute()
    with _generic_lock:
        _generic_cache[key] = {"data": data, "fp": fp, "ts": _time.time()}
    return data

def _data_fingerprint(db: Session) -> tuple:
    """Cheap signature of every input that affects holdings/curve results, so the
    cache auto-invalidates on any data change (no fragile per-endpoint hooks):
      - count + max(id)            → catches add / delete / import
      - sums of qty / price / fees → catches in-place edits (PATCH)
      - corporate-action count     → catches splits / bonuses / dividends
    Price refreshes don't change transactions, so refresh-prices also calls
    invalidate_analytics_cache() explicitly. All aggregates over the (small)
    transactions table — ~1ms, no price_cache scan."""
    txn_count = db.execute(select(func.count(Transaction.id))).scalar() or 0
    txn_max   = db.execute(select(func.max(Transaction.id))).scalar() or 0
    ca_count  = db.execute(select(func.count(CorporateAction.id))).scalar() or 0
    sums = db.execute(select(
        func.coalesce(func.sum(Transaction.quantity), 0),
        func.coalesce(func.sum(Transaction.price), 0),
        func.coalesce(func.sum(Transaction.fees), 0),
    )).one()
    return (txn_count, txn_max, ca_count, float(sums[0]), float(sums[1]), float(sums[2]))

def invalidate_analytics_cache() -> None:
    """Drop cached holdings/curve immediately (optional; fingerprint also covers writes)."""
    with _holdings_lock:
        _holdings_cache.clear()
    with _curve_lock:
        _curve_cache.clear()
    with _generic_lock:
        _generic_cache.clear()

def compute_holdings(db: Session, today: date | None = None, segment: str | None = None) -> list[HoldingRow]:
    """Cached wrapper: returns the same result as _compute_holdings_impl, but
    reuses it across the several endpoints that need it within one page load."""
    today = today or date.today()
    key = (segment or "all", today.isoformat())
    fp = _data_fingerprint(db)
    with _holdings_lock:
        ent = _holdings_cache.get(key)
        if ent and ent["fp"] == fp and (_time.time() - ent["ts"]) < _ANALYTICS_TTL:
            return ent["data"]
        data = _compute_holdings_impl(db, today, segment)
        _holdings_cache[key] = {"data": data, "fp": fp, "ts": _time.time()}
        return data

def compute_equity_curve(db: Session, benchmarks: list[str] | None = None,
                         today: date | None = None, segment: str | None = None) -> dict:
    """Cached wrapper around _compute_equity_curve_impl (the heavy, uncached builder)."""
    today = today or date.today()
    key = (segment or "all", tuple(benchmarks or []), today.isoformat())
    fp = _data_fingerprint(db)
    with _curve_lock:
        ent = _curve_cache.get(key)
        if ent and ent["fp"] == fp and (_time.time() - ent["ts"]) < _ANALYTICS_TTL:
            return ent["data"]
        data = _compute_equity_curve_impl(db, benchmarks, today, segment)
        _curve_cache[key] = {"data": data, "fp": fp, "ts": _time.time()}
        return data


# ---------- holdings ----------

@dataclass
class HoldingRow:
    isin: str | None
    symbol: str
    display_name: str     # scheme name for MF (from instruments.name), symbol for EQ
    segment: str
    folio: str | None     # MF folio number (None for equities)
    quantity: float
    avg_cost: float       # weighted avg cost across remaining lots
    invested: float       # qty * avg_cost
    current_price: float | None
    current_value: float | None
    realized_pnl: float
    unrealized_pnl: float | None
    pct_return: float | None
    xirr: float | None
    day_change: float | None = None      # today's value change vs previous close (qty × Δprice)
    day_change_pct: float | None = None   # per-unit % change vs previous close


def _txns_by_isin(txns: list[Transaction]) -> dict[str, list[Transaction]]:
    """Group transactions by (segment, symbol) so inconsistent ISIN population
    (common in older Zerodha exports) doesn't split one holding into two rows."""
    bucket: dict[str, list[Transaction]] = defaultdict(list)
    for t in sorted(txns, key=lambda t: (t.trade_date, t.id)):
        k = f"{t.segment}:{t.symbol.upper()}"
        bucket[k].append(t)
    return bucket


def _holding_state(txns: list[Transaction]) -> tuple[float, float, float]:
    """Returns (qty_held, avg_cost, realized_pnl) using weighted-avg method."""
    qty = 0.0
    avg = 0.0
    realized = 0.0
    for t in txns:
        if t.trade_type == "buy":
            new_qty = qty + t.quantity
            if new_qty <= 0:
                qty, avg = 0.0, 0.0
                continue
            avg = (qty * avg + t.quantity * t.price + t.fees) / new_qty
            qty = new_qty
        else:  # sell
            sell_qty = min(t.quantity, qty)
            realized += sell_qty * (t.price - avg) - t.fees
            qty -= sell_qty
            if qty <= 1e-9:
                qty, avg = 0.0, 0.0
    return qty, avg, realized


def _per_holding_xirr(
    txns: list[Transaction],
    current_value: float | None,
    today: date,
    div_flows: list[tuple[date, float]] | None = None,
) -> float | None:
    flows: list[tuple[date, float]] = []
    for t in txns:
        gross = t.quantity * t.price
        if t.trade_type == "buy":
            flows.append((t.trade_date, -(gross + t.fees)))
        else:
            flows.append((t.trade_date, gross - t.fees))
    if div_flows:
        flows.extend(div_flows)
    if current_value and current_value > 0:
        flows.append((today, current_value))
    return xirr(flows)


def _compute_holdings_impl(db: Session, today: date | None = None, segment: str | None = None) -> list[HoldingRow]:
    today = today or date.today()
    q = select(Transaction)
    if segment:
        q = q.where(Transaction.segment == segment)
    txns = db.execute(q).scalars().all()
    bucket = _txns_by_isin(txns)

    rows: list[HoldingRow] = []
    for _key, group in bucket.items():
        first = group[0]
        sym = first.symbol.upper()
        split_actions = get_split_actions(db, sym)
        qty, avg, realized, frac_cash = adjusted_holding_state(group, split_actions)
        best_isin = next((t.isin for t in group if t.isin), None)
        inst = get_or_create_instrument(
            db,
            symbol=first.symbol,
            isin=best_isin,
            segment=first.segment,
            exchange=first.exchange,
        )
        cur_price = None
        cur_value = None
        if qty > 0 and inst is not None:
            try:
                # cache_only=False: if today's price isn't cached, fetch it.
                # After Refresh prices, the cache is warm so this is instant.
                # Using True here causes wrong XIRR when cache is cold.
                cur_price = latest_close(db, inst, today, cache_only=False)
            except Exception:
                cur_price = None
            if cur_price is not None:
                cur_value = qty * cur_price
        # Daily gain: today's price vs the previous close. The prev close is the
        # most recent cached close on/before yesterday — reuses latest_close, no
        # extra fetch (cache_only=True, since past closes are always cached).
        day_change = None
        day_change_pct = None
        if qty > 0 and cur_price is not None and inst is not None:
            try:
                prev_close = latest_close(db, inst, today - timedelta(days=1), cache_only=True)
            except Exception:
                prev_close = None
            if prev_close:
                day_change = qty * (cur_price - prev_close)
                day_change_pct = (cur_price - prev_close) / prev_close
        unrealized = (cur_value - qty * avg) if cur_value is not None else None
        pct = None
        if qty > 0 and avg > 0 and cur_price is not None:
            pct = (cur_price - avg) / avg
        div_flows = dividend_cashflows(db, sym, group, split_actions)
        # Add fractional share payout as a one-time inflow at today (approximate)
        frac_flows = [(today, frac_cash)] if frac_cash > 0 else []
        x = _per_holding_xirr(group, cur_value, today, div_flows + frac_flows) if (qty > 0 or any(t.trade_type == "sell" for t in group)) else None

        # For MF: display the full scheme name from instruments; for EQ: use symbol
        best_folio = next((t.folio for t in group if t.folio), None)
        display_name = (
            inst.name if inst and inst.name and first.segment == "MF"
            else first.symbol
        )

        rows.append(
            HoldingRow(
                isin=first.isin,
                symbol=first.symbol,
                display_name=display_name,
                segment=first.segment,
                folio=best_folio,
                quantity=qty,
                avg_cost=avg,
                invested=qty * avg,
                current_price=cur_price,
                current_value=cur_value,
                realized_pnl=realized,
                unrealized_pnl=unrealized,
                pct_return=pct,
                xirr=x,
                day_change=day_change,
                day_change_pct=day_change_pct,
            )
        )
    db.commit()
    rows.sort(key=lambda r: (r.current_value or 0), reverse=True)
    return rows


# ---------- period XIRR ----------

def _portfolio_value_on(db: Session, all_txns: list, on_date: date) -> float:
    """Estimated total portfolio market value on a specific historical date.

    Uses the price_cache (already populated from normal use). If a price for
    the exact date isn't cached, falls back to the nearest earlier cached close.
    Returns 0 if no prices are found (graceful degradation).
    """
    bucket = _txns_by_isin(all_txns)
    total = 0.0
    for _key, group in bucket.items():
        relevant = [t for t in group if t.trade_date <= on_date]
        if not relevant:
            continue
        sym = relevant[0].symbol.upper()
        split_actions = [sa for sa in get_split_actions(db, sym) if sa[0] <= on_date]
        qty, avg, _, _ = adjusted_holding_state(relevant, split_actions)
        if qty <= 0:
            continue
        best_isin = next((t.isin for t in relevant if t.isin), None)
        inst = get_or_create_instrument(
            db,
            symbol=relevant[0].symbol,
            isin=best_isin,
            segment=relevant[0].segment,
            exchange=relevant[0].exchange,
        )
        if inst is None:
            continue
        price = latest_close(db, inst, on_date)
        if price:
            total += qty * price
    return total


def compute_period_xirr(
    db: Session,
    from_date: date | None = None,
    to_date: date | None = None,
    segment: str | None = None,
) -> dict:
    """XIRR for portfolio + all benchmarks over a specific period.

    When from_date is set, the portfolio value on from_date-1 is used as the
    opening outflow (carry-in), and the portfolio value on to_date is the
    closing inflow. Transactions within the period are cashflows in between.
    This is the Modified Dietz / subperiod XIRR approach — it correctly
    isolates a year or FY without distortion from earlier periods.

    When from_date is None: standard all-time XIRR (original behaviour).
    """
    today = to_date or date.today()
    q = select(Transaction).order_by(Transaction.trade_date, Transaction.id)
    if segment:
        q = q.where(Transaction.segment == segment)
    all_txns = db.execute(q).scalars().all()

    if not all_txns:
        return {"portfolio_xirr": None, "benchmarks": {}, "period_label": "no data"}

    first_txn_date = min(t.trade_date for t in all_txns)

    if from_date is None or from_date <= first_txn_date:
        # All-time: use standard portfolio cashflows + current terminal value
        flows = portfolio_cashflows(db, all_txns)
        holdings = compute_holdings(db, today, segment=segment)
        current_value = sum(h.current_value or 0 for h in holdings)
        if current_value > 0:
            flows.append((today, current_value))
        period_txns = all_txns
        pre_txns: list = []
    else:
        # Period: opening value + period transactions + closing value
        pre_txns = [t for t in all_txns if t.trade_date < from_date]
        period_txns = [t for t in all_txns if from_date <= t.trade_date <= today]

        opening_value = _portfolio_value_on(db, all_txns, from_date - timedelta(days=1))
        closing_value = _portfolio_value_on(db, all_txns, today)

        flows = []
        if opening_value > 0:
            flows.append((from_date, -opening_value))
        for t in period_txns:
            gross = t.quantity * t.price
            flows.append((t.trade_date, -(gross + t.fees) if t.trade_type == "buy" else gross - t.fees))
        # dividend cashflows for the period
        bucket = _txns_by_isin(period_txns)
        for _key, group in bucket.items():
            sym = group[0].symbol.upper()
            sa = get_split_actions(db, sym)
            div_flows = dividend_cashflows(db, sym, group, sa)
            for d, a in div_flows:
                if from_date <= d <= today:
                    flows.append((d, a))
        if closing_value > 0:
            flows.append((today, closing_value))

    portfolio_xirr_val = xirr(flows) if flows else None

    # --- benchmark XIRRs for the same period ---
    bench_results: dict[str, dict] = {}
    start_date = first_txn_date

    for ticker, name in BENCHMARKS.items():
        series = fetch_benchmark_series(db, ticker, start_date, today, cache_only=True)
        if not series:
            bench_results[ticker] = {"name": name, "xirr": None}
            continue
        ff = fill_forward(series, start_date, today)
        sorted_bd = sorted(ff.keys())
        first_px = ff[sorted_bd[0]] if sorted_bd else None
        if not first_px:
            bench_results[ticker] = {"name": name, "xirr": None}
            continue

        if from_date is None or from_date <= first_txn_date:
            # All-time: replay all transactions
            units = 0.0
            sim_flows: list[tuple[date, float]] = []
            for t in sorted(all_txns, key=lambda t: (t.trade_date, t.id)):
                px = ff.get(t.trade_date) or first_px
                if not px:
                    continue
                cash = t.quantity * t.price + t.fees
                if t.trade_type == "buy":
                    units += cash / px
                    sim_flows.append((t.trade_date, -cash))
                else:
                    units_to_sell = min(units, cash / px)
                    index_proceeds = units_to_sell * px
                    units -= units_to_sell
                    if index_proceeds > 0:
                        sim_flows.append((t.trade_date, index_proceeds))
        else:
            # Period: pre-simulate to get opening units, then replay period
            units = 0.0
            for t in sorted(pre_txns, key=lambda t: (t.trade_date, t.id)):
                px = ff.get(t.trade_date) or first_px
                if not px:
                    continue
                cash = t.quantity * t.price + t.fees
                if t.trade_type == "buy":
                    units += cash / px
                else:
                    units_to_sell = min(units, cash / px)
                    units -= units_to_sell

            opening_px = ff.get(from_date - timedelta(days=1)) or ff.get(from_date) or first_px
            bench_opening = units * opening_px if opening_px else 0.0
            sim_flows = []
            if bench_opening > 0:
                sim_flows.append((from_date, -bench_opening))

            for t in sorted(period_txns, key=lambda t: (t.trade_date, t.id)):
                px = ff.get(t.trade_date) or first_px
                if not px:
                    continue
                cash = t.quantity * t.price + t.fees
                if t.trade_type == "buy":
                    units += cash / px
                    sim_flows.append((t.trade_date, -cash))
                else:
                    units_to_sell = min(units, cash / px)
                    index_proceeds = units_to_sell * px
                    units -= units_to_sell
                    if index_proceeds > 0:
                        sim_flows.append((t.trade_date, index_proceeds))

        # closing value
        avail = [d for d in series if d <= today]
        today_px = series[max(avail)] if avail else None
        sim_value = units * today_px if today_px else 0
        if sim_value > 0:
            sim_flows.append((today, sim_value))

        bench_results[ticker] = {
            "name": name,
            "xirr": xirr(sim_flows) if sim_flows else None,
            "sim_value": round(sim_value, 2),
        }

    db.commit()
    return {
        "from_date": from_date.isoformat() if from_date else None,
        "to_date": today.isoformat(),
        "portfolio_xirr": portfolio_xirr_val,
        "benchmarks": bench_results,
    }


# ---------- Fixed Income (FD / RD) ----------

TDS_THRESHOLD = 40_000.0   # ₹40,000 aggregate interest per bank per FY
TDS_RATE      = 0.10       # 10% TDS (PAN linked)


@dataclass
class FIHoldingRow:
    id: int
    fi_type: str              # FD_CUM | FD_NON_CUM | RD
    bank: str
    account_no: str | None
    amount: float             # principal (FD) or monthly instalment (RD)
    start_date: date
    maturity_date: date
    interest_rate: float
    compounding: str
    payout_frequency: str | None
    is_tax_saver: bool
    notes: str | None
    current_value: float
    maturity_value: float
    interest_earned: float    # total interest earned to date
    interest_this_fy: float   # interest accrued this financial year (taxable)
    tds_applicable: bool      # True when FY interest from this bank > ₹40k
    tds_estimate: float       # estimated TDS at 10%
    days_to_maturity: int     # negative = already matured
    xirr: float | None


def compute_fi_holdings(db: Session, today: date | None = None) -> list[FIHoldingRow]:
    """Compute current values and tax metrics for all FD/RD records."""
    today = today or date.today()
    records = db.execute(select(FixedIncome).order_by(FixedIncome.start_date)).scalars().all()

    # Aggregate FY interest per bank for TDS check
    bank_fy_interest: dict[str, float] = defaultdict(float)
    for fi in records:
        bank_fy_interest[fi.bank.lower()] += interest_this_fy(fi, today)

    rows: list[FIHoldingRow] = []
    for fi in records:
        cv     = fi_current_value(fi)
        mv     = fi_maturity_value(fi)
        ie     = interest_earned_total(fi, today)
        fy_int = interest_this_fy(fi, today)
        bank_total = bank_fy_interest[fi.bank.lower()]
        tds_app = bank_total > TDS_THRESHOLD
        tds_est = bank_total * TDS_RATE if tds_app else 0.0
        dtm     = (fi.maturity_date - today).days
        x       = xirr(fi_cashflows(fi)) if fi.start_date < today else None

        rows.append(FIHoldingRow(
            id=fi.id, fi_type=fi.fi_type, bank=fi.bank, account_no=fi.account_no,
            amount=fi.amount, start_date=fi.start_date, maturity_date=fi.maturity_date,
            interest_rate=fi.interest_rate, compounding=fi.compounding,
            payout_frequency=fi.payout_frequency, is_tax_saver=fi.is_tax_saver,
            notes=fi.notes, current_value=cv, maturity_value=mv,
            interest_earned=ie, interest_this_fy=fy_int,
            tds_applicable=tds_app, tds_estimate=tds_est,
            days_to_maturity=dtm, xirr=x,
        ))
    return rows


def compute_fi_summary(db: Session, today: date | None = None) -> dict:
    """Summary statistics for the Fixed Income portfolio."""
    today = today or date.today()
    rows = compute_fi_holdings(db, today)
    active   = [r for r in rows if r.days_to_maturity >= 0]
    matured  = [r for r in rows if r.days_to_maturity < 0]

    # For RDs: invested = instalments paid so far + initial deposit (if any).
    # +1 because the start month itself is instalment 0 (paid on start date).
    fi_records = {r.id: r for r in db.execute(select(FixedIncome)).scalars().all()}
    from .fi_calc import rd_tenure_months as _rd_months
    total_invested = 0.0
    for r in rows:
        if r.fi_type != "RD":
            total_invested += r.amount
        else:
            n_paid = min(
                max(0, (today.year - r.start_date.year) * 12 +
                        (today.month - r.start_date.month) + 1),
                _rd_months(r.start_date, r.maturity_date)
            )
            fi_rec = fi_records.get(r.id)
            init = (fi_rec.initial_deposit or 0.0) if fi_rec else 0.0
            total_invested += r.amount * n_paid + init
    total_current  = sum(r.current_value for r in rows)
    total_fy_int   = sum(r.interest_this_fy for r in rows)

    # TDS per bank
    bank_fy: dict[str, float] = defaultdict(float)
    for r in rows:
        bank_fy[r.bank] += r.interest_this_fy
    tds_banks = [
        {"bank": b, "fy_interest": v, "tds": v * TDS_RATE}
        for b, v in bank_fy.items() if v > TDS_THRESHOLD
    ]

    # Portfolio XIRR for FI only
    all_flows: list[tuple[date, float]] = []
    for fi in db.execute(select(FixedIncome)).scalars().all():
        all_flows.extend(fi_cashflows(fi))
    fi_xirr = xirr(all_flows) if all_flows else None

    return {
        "total_invested":    total_invested,
        "total_current":     total_current,
        "total_interest_earned": total_current - total_invested,
        "total_fy_interest": total_fy_int,
        "active_count":      len(active),
        "matured_count":     len(matured),
        "fi_xirr":           fi_xirr,
        "tds_warnings":      tds_banks,
        "as_of":             today.isoformat(),
    }


def fi_cashflows_all(db: Session) -> list[tuple[date, float]]:
    """All FI cashflows for inclusion in combined portfolio XIRR."""
    flows: list[tuple[date, float]] = []
    for fi in db.execute(select(FixedIncome)).scalars().all():
        flows.extend(fi_cashflows(fi))
    return flows


# ---------- data quality ----------

def find_orphan_sells(db: Session) -> list[dict]:
    """Sells that have no matching buy (IPO allotments, demerger receipts, off-market transfers).

    These are silent no-ops in holdings/P&L calculations — the sell is ignored
    because there are no bought shares to sell from. Each entry represents missing
    realized capital gains that need a corresponding buy transaction to be added.
    """
    txns = db.execute(select(Transaction).order_by(Transaction.trade_date, Transaction.id)).scalars().all()
    bucket = _txns_by_isin(txns)
    orphans: list[dict] = []
    for _key, group in bucket.items():
        sym = group[0].symbol.upper()
        buys  = [t for t in group if t.trade_type == "buy"]
        sells = [t for t in group if t.trade_type == "sell"]
        if not sells:
            continue
        split_actions = get_split_actions(db, sym)
        total_buy_adj = sum(
            t.quantity * cumulative_factor(split_actions, t.trade_date)
            for t in buys
        )
        total_sell = sum(t.quantity for t in sells)
        orphan_qty = max(0.0, total_sell - total_buy_adj)
        if orphan_qty > 0.001:
            for t in sells:
                orphans.append({
                    "symbol": sym,
                    "segment": t.segment,
                    "trade_date": t.trade_date.isoformat(),
                    "quantity": t.quantity,
                    "price": t.price,
                    "proceeds": round(t.quantity * t.price, 2),
                    "reason": "No buy transaction found — likely IPO allotment or corporate action receipt",
                })
    orphans.sort(key=lambda x: x["trade_date"])
    return orphans


# ---------- realized P&L by period ----------

def compute_realized_pnl_by_period(
    db: Session,
    from_date: date | None = None,
    to_date: date | None = None,
    segment: str | None = None,
) -> dict:
    """Realized P&L for sells whose trade_date falls within [from_date, to_date].

    Uses the same weighted-average cost basis as the full holdings calculation,
    but only counts the realized P&L from sells in the date window.

    Also returns STCG/LTCG split (equity held < 12 months = short-term,
    ≥ 12 months = long-term), which matters for Indian income tax.

    Cost basis is always derived from the full transaction history — we don't
    artificially restrict which buys are visible. Only the SELLS are filtered.
    """
    q = select(Transaction).order_by(Transaction.trade_date, Transaction.id)
    if segment:
        q = q.where(Transaction.segment == segment)
    txns = db.execute(q).scalars().all()
    bucket = _txns_by_isin(txns)

    total_realized = 0.0
    stcg = 0.0   # held < 12 months → taxed at 20%
    ltcg = 0.0   # held ≥ 12 months → taxed at 12.5% above ₹1.25L

    breakdown: list[dict] = []

    for _key, group in bucket.items():
        sym = group[0].symbol.upper()
        split_actions = get_split_actions(db, sym)

        # Replay all events chronologically, but only accumulate realized P&L
        # for sells that fall within the requested period.
        from .corporate_actions import QTY_SPLIT_TYPES

        all_events: list[tuple] = []
        for t in sorted(group, key=lambda t: (t.trade_date, t.id)):
            kind = 0 if t.trade_type == "buy" else 1
            all_events.append((t.trade_date, kind, t))
        for ex_date, action_type, ratio in split_actions:
            all_events.append((ex_date, 2, (action_type, ratio)))
        all_events.sort(key=lambda x: (x[0], x[1]))

        qty = 0.0
        avg = 0.0
        # Also track a simple lot list for STCG/LTCG attribution
        lots: list[tuple[date, float, float]] = []  # (buy_date, qty, cost_per_share)

        for ev_date, kind_order, payload in all_events:
            if kind_order == 0:                   # buy
                t = payload
                cost_per = (t.quantity * t.price + t.fees) / t.quantity
                lots.append((t.trade_date, t.quantity, cost_per))
                new_qty = qty + t.quantity
                avg = (qty * avg + t.quantity * cost_per) / new_qty
                qty = new_qty
            elif kind_order == 1:                 # sell
                t = payload
                sell_qty = min(t.quantity, qty)
                if sell_qty <= 0:
                    continue
                gain = sell_qty * (t.price - avg) - t.fees
                realized_this = gain

                # Only count if sell date is within requested period
                in_period = (
                    (from_date is None or t.trade_date >= from_date) and
                    (to_date   is None or t.trade_date <= to_date)
                )
                if in_period:
                    total_realized += realized_this
                    # STCG/LTCG: use FIFO to attribute holding period
                    remaining = sell_qty
                    stcg_portion = 0.0
                    ltcg_portion = 0.0
                    for i, (buy_dt, lot_qty, lot_cost) in enumerate(lots):
                        if remaining <= 0:
                            break
                        used = min(remaining, lot_qty)
                        lot_gain = used * (t.price - lot_cost)
                        days_held = (t.trade_date - buy_dt).days
                        if days_held < 365:
                            stcg_portion += lot_gain
                        else:
                            ltcg_portion += lot_gain
                        remaining -= used
                        lots[i] = (buy_dt, lot_qty - used, lot_cost)
                    lots = [(d, q, c) for d, q, c in lots if q > 0.001]
                    stcg += stcg_portion
                    ltcg += ltcg_portion
                    if abs(realized_this) > 0.01:
                        breakdown.append({
                            "symbol": sym,
                            "sell_date": t.trade_date.isoformat(),
                            "qty": sell_qty,
                            "sell_price": t.price,
                            "avg_cost": avg,
                            "realized": round(realized_this, 2),
                            "stcg": round(stcg_portion, 2),
                            "ltcg": round(ltcg_portion, 2),
                        })
                qty -= sell_qty
                if qty <= 1e-9:
                    qty, avg = 0.0, 0.0
            else:                                 # corporate action
                action_type, ratio = payload
                if action_type == "demerger":
                    avg = avg * ratio
                elif action_type in QTY_SPLIT_TYPES:
                    new_raw = qty * ratio
                    new_floored = float(int(new_raw))
                    qty = new_floored
                    if qty > 0 and ratio > 0:
                        avg = avg / ratio
                    # Adjust per-lot quantities and cost basis so STCG/LTCG
                    # attribution stays correct after each bonus/split event.
                    lots = [(d, q * ratio, c / ratio) for d, q, c in lots]

    breakdown.sort(key=lambda x: x["sell_date"])
    return {
        "from_date": from_date.isoformat() if from_date else None,
        "to_date": to_date.isoformat() if to_date else None,
        "total_realized": round(total_realized, 2),
        "stcg": round(stcg, 2),
        "ltcg": round(ltcg, 2),
        "breakdown": breakdown,
    }


# ---------- portfolio-level summary ----------

def portfolio_cashflows(
    db: Session, txns: list[Transaction], include_fi: bool = False
) -> list[tuple[date, float]]:
    """Cashflows from the investor's perspective including dividends.

    Buy:      outflow of (qty*price + fees)  → negative
    Sell:     inflow  of (qty*price - fees)  → positive
    Dividend: inflow  of qty_held * ₹/share  → positive
    FI:       included when include_fi=True (combined portfolio view)
    """
    flows: list[tuple[date, float]] = []
    for t in txns:
        gross = t.quantity * t.price
        if t.trade_type == "buy":
            flows.append((t.trade_date, -(gross + t.fees)))
        else:
            flows.append((t.trade_date, gross - t.fees))

    # append dividend cashflows per symbol
    bucket = _txns_by_isin(txns)
    for _key, group in bucket.items():
        sym = group[0].symbol.upper()
        split_actions = get_split_actions(db, sym)
        div_flows = dividend_cashflows(db, sym, group, split_actions)
        flows.extend(div_flows)

    if include_fi:
        flows.extend(fi_cashflows_all(db))

    return flows


def _is_transfer(txn: Transaction) -> bool:
    """True if a transaction is a switch/transfer (not a cash flow from/to bank).

    Switches: Switch Over In/Out, Lateral Shift In/Out, STP In/Out.
    These move money between funds but no new money enters/leaves the bank account.
    They should NOT count toward 'invested' (what actually came from your pocket).
    """
    notes = (txn.notes or "").lower()
    return any(kw in notes for kw in (
        "lateral shift in", "lateral shift out",
        "switch over in", "switch over out",
        "switch in", "switch out",
        "stp in", "stp out",
    ))


def compute_bank_invested(txns: list[Transaction]) -> float:
    """Total money ever paid from bank — gross invested (INDMoney-style).

    Counts only PURCHASE cash flows (SIPs, lump sums) that originated from
    the bank. Switches/transfers are excluded because no new money left the bank.
    Redemption proceeds are NOT subtracted — this matches INDMoney's definition
    of "invested" which shows lifetime gross deployment, not net.

    Example:
      Invest ₹1,000 in Fund A (bank outflow)  → invested = ₹1,000
      Switch Fund A → Fund B (no bank flow)    → invested = ₹1,000  ✓
      Redeem Fund B for ₹1,200 (bank inflow)   → invested = ₹1,000  ✓ (not ₹0)
    """
    total = 0.0
    for t in txns:
        if _is_transfer(t):
            continue
        if t.trade_type == "buy":
            total += t.quantity * t.price + t.fees
        # Sells/redemptions deliberately not subtracted — gross definition
    return total


def compute_summary(db: Session, today: date | None = None, segment: str | None = None) -> dict:
    today = today or date.today()
    holdings = compute_holdings(db, today, segment=segment)
    invested = sum(h.invested for h in holdings)
    # eq_mf_value is the EQ+MF-only terminal value used for XIRR.
    # It must NOT include FI so that the XIRR cashflows and terminal value are consistent.
    eq_mf_value   = sum(h.current_value or 0 for h in holdings)
    current_value = eq_mf_value   # display value; FI will be added below for cards only
    realized = sum(h.realized_pnl for h in holdings)
    unrealized = current_value - invested if invested else 0
    total_pnl = realized + unrealized

    # Add FI to wealth display numbers. FI has no switches so bank_invested = full amount.
    # FI is intentionally excluded from XIRR — its cashflows would distort the
    # NIFTY benchmark comparison which only applies to market-linked assets.
    fi_invested = 0.0
    fi_current  = 0.0
    if segment is None:   # combined "All" view
        fi_rows    = compute_fi_holdings(db, today)
        from .fi_calc import rd_tenure_months as _rd_months
        fi_invested = sum(
            r.amount if r.fi_type != "RD"
            else r.amount * min(
                max(0, (today.year - r.start_date.year) * 12 +
                        (today.month - r.start_date.month) + 1),
                _rd_months(r.start_date, r.maturity_date)
            )
            for r in fi_rows
        )
        fi_current  = sum(r.current_value for r in fi_rows)
        invested      += fi_invested
        current_value += fi_current       # display card value includes FI
        total_pnl     += (fi_current - fi_invested)

    q = select(Transaction)
    if segment:
        q = q.where(Transaction.segment == segment)
    txns = db.execute(q).scalars().all()
    # XIRR terminal uses eq_mf_value (never current_value which may include FI)
    flows = portfolio_cashflows(db, txns, include_fi=False)
    if eq_mf_value > 0:
        flows.append((today, eq_mf_value))
    portfolio_xirr = xirr(flows)

    # Benchmark XIRR — replay portfolio cashflows into each index
    bench: dict[str, dict] = {}
    if txns:
        start_date = min(t.trade_date for t in txns)
        for ticker, name in BENCHMARKS.items():
            series = fetch_benchmark_series(db, ticker, start_date, today, cache_only=True)
            if not series:
                bench[ticker] = {"name": name, "xirr": None, "current_value": None}
                continue
            ff = fill_forward(series, start_date, today)

            # For transactions before the benchmark's first data date, use the
            # earliest available price so no cashflow is silently dropped.
            # Dropping early outflows while keeping the final value inflates XIRR wildly.
            sorted_bench_dates = sorted(ff.keys())
            first_bench_px = ff[sorted_bench_dates[0]] if sorted_bench_dates else None

            units = 0.0
            sim_flows: list[tuple[date, float]] = []
            for t in sorted(txns, key=lambda t: (t.trade_date, t.id)):
                px = ff.get(t.trade_date) or first_bench_px
                if px is None:
                    continue
                cash = t.quantity * t.price + t.fees
                if t.trade_type == "buy":
                    units += cash / px
                    sim_flows.append((t.trade_date, -cash))
                else:
                    # Sell: withdraw proportional index units and credit their
                    # actual index value — NOT the stock's sell price.
                    # Using the stock price here inflates the benchmark XIRR
                    # whenever the stock outperformed the index (e.g. IPO gains).
                    units_to_sell = min(units, cash / px)
                    index_proceeds = units_to_sell * px
                    units -= units_to_sell
                    if index_proceeds > 0:
                        sim_flows.append((t.trade_date, index_proceeds))
            today_px = ff.get(today)
            if today_px is None:
                avail = [d for d in series if d <= today]
                if avail:
                    today_px = series[max(avail)]
            sim_value = (units * today_px) if today_px else 0
            if sim_value > 0:
                sim_flows.append((today, sim_value))
            bench[ticker] = {
                "name": name,
                "xirr": xirr(sim_flows),
                "current_value": sim_value,
            }

    # Daily gain: sum of per-holding day changes (EQ+MF); % vs yesterday's value.
    day_change = sum(h.day_change or 0 for h in holdings)
    prev_value = eq_mf_value - day_change
    return {
        "invested": invested,
        "current_value": current_value,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "total_pnl": total_pnl,
        "pct_return": (total_pnl / invested) if invested else None,
        "portfolio_xirr": portfolio_xirr,
        "day_change": day_change,
        "day_change_pct": (day_change / prev_value) if prev_value else None,
        "benchmarks": bench,
        "as_of": today.isoformat(),
    }


# ---------- equity curve ----------

# Keywords sorted longest-first so longer matches take priority
# (e.g. "large and mid cap" is checked before "large cap" and "mid cap").
_MF_CAP_LABEL: list[tuple[str, str]] = sorted([
    # Equity — market cap mandated (longer patterns first to avoid wrong substring matches)
    ("large and mid cap",          "Large & Mid Cap"),
    ("large & mid cap",            "Large & Mid Cap"),   # ampersand variant
    ("large cap",                  "Large Cap"),
    ("nifty midcap",               "Mid Cap"),           # midcap index funds
    ("midcap 150",                 "Mid Cap"),
    ("midcap 100",                 "Mid Cap"),
    ("midcap 50",                  "Mid Cap"),
    ("mid cap",                    "Mid Cap"),
    ("midcap",                     "Mid Cap"),           # single-word variant
    ("small cap",                  "Small Cap"),
    ("flexi cap",                  "Flexi Cap"),
    ("multicap",                   "Multi Cap"),         # single-word variant
    ("multi cap",                  "Multi Cap"),
    # Equity — strategy / style
    ("elss",                       "ELSS (Tax Saver)"),
    ("tax saver",                  "ELSS (Tax Saver)"),
    ("focused",                    "Focused Fund"),
    ("dividend yield",             "Dividend Yield"),
    ("value",                      "Value / Contra"),
    ("contra",                     "Value / Contra"),
    ("sectoral",                   "Sectoral / Thematic"),
    ("thematic",                   "Sectoral / Thematic"),
    # Common sector fund names that don't say "sectoral" or "thematic"
    ("consumption",                "Sectoral / Thematic"),
    ("infrastructure",             "Sectoral / Thematic"),
    ("banking",                    "Sectoral / Thematic"),
    ("technology fund",            "Sectoral / Thematic"),
    ("pharma",                     "Sectoral / Thematic"),
    ("healthcare fund",            "Sectoral / Thematic"),
    ("manufacturing",              "Sectoral / Thematic"),
    ("business cycle",             "Sectoral / Thematic"),
    ("opportunities fund",         "Sectoral / Thematic"),
    ("innovation",                 "Sectoral / Thematic"),
    ("esg",                        "Sectoral / Thematic"),
    # Hybrid
    ("aggressive hybrid",          "Aggressive Hybrid"),
    ("conservative hybrid",        "Conservative Hybrid"),
    ("balanced advantage",         "Dynamic / Balanced Advantage"),
    ("dynamic asset allocation",   "Dynamic / Balanced Advantage"),
    ("multi asset allocation",     "Multi Asset"),
    ("multi asset",                "Multi Asset"),
    ("arbitrage",                  "Arbitrage"),
    ("hybrid",                     "Hybrid"),
    ("balanced",                   "Hybrid"),
    # Index / ETF
    ("index",                      "Index / ETF"),
    ("etf",                        "Index / ETF"),
    ("nifty",                      "Index / ETF"),
    ("sensex",                     "Index / ETF"),
    # International / FOF
    ("fund of funds",              "International / FOF"),
    ("international",              "International / FOF"),
    ("global",                     "International / FOF"),
    ("world",                      "International / FOF"),
    ("overseas",                   "International / FOF"),
    # Debt
    ("banking and psu",            "Debt (Banking & PSU)"),
    ("corporate bond",             "Debt (Corporate Bond)"),
    ("short duration",             "Debt (Short Duration)"),
    ("low duration",               "Debt (Low Duration)"),
    ("medium duration",            "Debt (Medium Duration)"),
    ("long duration",              "Debt (Long Duration)"),
    ("gilt",                       "Debt (Gilt)"),
    ("floater",                    "Debt (Floater)"),
    ("overnight",                  "Liquid / Debt"),
    ("liquid",                     "Liquid / Debt"),
    ("money market",               "Liquid / Debt"),
    ("debt",                       "Liquid / Debt"),
], key=lambda x: -len(x[0]))


def _mf_category_label(scheme_name: str | None) -> str:
    """Derive a SEBI category label from the scheme name.

    Keywords are matched longest-first so more-specific patterns (e.g.
    "large and mid cap") always win over shorter ones ("large cap", "mid cap").
    """
    n = (scheme_name or "").lower()
    # Skip raw ISINs — they start with INF/IN0 and contain no category info
    if n.startswith(("inf", "in0")) and len(n) < 14:
        return "Other / Unknown"
    for keyword, label in _MF_CAP_LABEL:
        if keyword in n:
            return label
    return "Other / Unknown"


def compute_allocation_breakdown(db: Session) -> dict:
    """Market cap + sector breakdown for direct EQ holdings; SEBI-category breakdown for MFs.

    Returns two top-level sections:
      equity  — direct stocks grouped by market_cap_category and sector
      mf      — MF holdings grouped by SEBI category (inferred from scheme name)

    Each group contains the constituent holdings sorted by current value descending,
    so the frontend can render both the summary bar and an expandable detail table.
    """
    today = date.today()

    # ---- direct equity ----
    eq_holdings = [
        h for h in compute_holdings(db, today, segment="EQ")
        if (h.quantity or 0) > 0 and h.current_value
    ]
    eq_total = sum(h.current_value for h in eq_holdings)

    # Pull market_cap_category + sector from instruments table in one query
    eq_syms = [h.symbol for h in eq_holdings]
    inst_map: dict[str, "Instrument"] = {}
    if eq_syms:
        for inst in db.execute(
            select(Instrument).where(Instrument.symbol.in_(eq_syms))
        ).scalars().all():
            inst_map[inst.symbol] = inst

    cap_groups: dict[str, list] = defaultdict(list)
    sector_groups: dict[str, list] = defaultdict(list)
    unclassified_count = 0

    for h in eq_holdings:
        inst = inst_map.get(h.symbol)
        cap_cat = (inst.market_cap_category if inst else None)
        sector  = (inst.sector if inst else None)

        # Runtime fallback: apply static overrides even if DB hasn't been refreshed yet
        if not sector or not cap_cat:
            ov = INSTRUMENT_META_OVERRIDES.get(h.symbol.upper(), {})
            if not sector:
                sector = ov.get("sector")
            if not cap_cat:
                cap_cat = ov.get("market_cap_category")

        sector = sector or "Unclassified"
        entry = {
            "symbol":       h.symbol,
            "display_name": h.display_name,
            "value":        round(h.current_value, 2),
            "pct":          round(h.current_value / eq_total * 100, 2) if eq_total else 0,
            "xirr":         h.xirr,
            "pct_return":   h.pct_return,
        }
        cap_groups[cap_cat or "unclassified"].append(entry)
        sector_groups[sector].append(entry)
        if not cap_cat:
            unclassified_count += 1

    _cap_order = {"large": 0, "mid": 1, "small": 2, "unclassified": 3}
    _cap_labels = {"large": "Large Cap", "mid": "Mid Cap",
                   "small": "Small Cap", "unclassified": "Unclassified"}
    by_market_cap = [
        {
            "category": cat,
            "label":    _cap_labels.get(cat, cat.title()),
            "value":    round(sum(e["value"] for e in items), 2),
            "pct":      round(sum(e["value"] for e in items) / eq_total * 100, 2) if eq_total else 0,
            "holdings": sorted(items, key=lambda x: x["value"], reverse=True),
        }
        for cat, items in sorted(cap_groups.items(), key=lambda x: _cap_order.get(x[0], 99))
    ]
    by_sector = sorted(
        [
            {
                "sector":   sect,
                "value":    round(sum(e["value"] for e in items), 2),
                "pct":      round(sum(e["value"] for e in items) / eq_total * 100, 2) if eq_total else 0,
                "holdings": sorted(items, key=lambda x: x["value"], reverse=True),
            }
            for sect, items in sector_groups.items()
        ],
        key=lambda x: x["value"],
        reverse=True,
    )

    # ---- mutual funds ----
    mf_holdings = [
        h for h in compute_holdings(db, today, segment="MF")
        if (h.quantity or 0) > 0 and h.current_value
    ]
    mf_total = sum(h.current_value for h in mf_holdings)

    mf_groups: dict[str, list] = defaultdict(list)
    for h in mf_holdings:
        label = _mf_category_label(h.display_name)
        mf_groups[label].append({
            "symbol":       h.symbol,
            "display_name": h.display_name,
            "value":        round(h.current_value, 2),
            "pct":          round(h.current_value / mf_total * 100, 2) if mf_total else 0,
            "xirr":         h.xirr,
            "pct_return":   h.pct_return,
        })
    by_mf_category = sorted(
        [
            {
                "category": cat,
                "value":    round(sum(e["value"] for e in items), 2),
                "pct":      round(sum(e["value"] for e in items) / mf_total * 100, 2) if mf_total else 0,
                "holdings": sorted(items, key=lambda x: x["value"], reverse=True),
            }
            for cat, items in mf_groups.items()
        ],
        key=lambda x: x["value"],
        reverse=True,
    )

    return {
        "equity": {
            "total_value":        round(eq_total, 2),
            "by_market_cap":      by_market_cap,
            "by_sector":          by_sector,
            "unclassified_count": unclassified_count,
        },
        "mf": {
            "total_value":    round(mf_total, 2),
            "by_category":    by_mf_category,
        },
    }


def compute_xirr_split(db: Session, segment: str | None = None) -> dict:
    """Split portfolio XIRR into active (currently held) vs closed (fully exited) positions.

    Active XIRR  — cashflows for instruments where qty > 0, plus today's market value as
                   terminal cashflow. Answers: "how is my current portfolio doing?"
    Closed XIRR  — cashflows for fully exited instruments only (no terminal value, position
                   is closed). Answers: "when I sold, how well did I do historically?"

    Both are computed on all-time cashflows, shown only on the all-time XIRR view.
    Note: closed XIRR has survivorship bias — it excludes instruments still held,
    which may include long-term positions yet to realise gains.
    """
    today = date.today()
    q = select(Transaction).order_by(Transaction.trade_date, Transaction.id)
    if segment:
        q = q.where(Transaction.segment == segment)
    all_txns = db.execute(q).scalars().all()
    if not all_txns:
        return {"active_xirr": None, "closed_xirr": None}

    bucket = _txns_by_isin(all_txns)
    active_flows: list[tuple[date, float]] = []
    closed_flows: list[tuple[date, float]] = []

    for _key, group in bucket.items():
        first = group[0]
        sym = first.symbol.upper()
        split_actions = get_split_actions(db, sym)
        qty, avg, _realized, frac_cash = adjusted_holding_state(group, split_actions)

        flows: list[tuple[date, float]] = []
        for t in group:
            gross = t.quantity * t.price
            flows.append(
                (t.trade_date, -(gross + t.fees) if t.trade_type == "buy" else gross - t.fees)
            )
        div_flows = dividend_cashflows(db, sym, group, split_actions)
        flows.extend(div_flows)

        if qty > 0:
            best_isin = next((t.isin for t in group if t.isin), None)
            inst = get_or_create_instrument(
                db, symbol=first.symbol, isin=best_isin,
                segment=first.segment, exchange=first.exchange,
            )
            cur_price = None
            if inst:
                try:
                    cur_price = latest_close(db, inst, today, cache_only=True)
                except Exception:
                    pass
            if cur_price is None:
                continue  # skip: no price means terminal value unknown — don't distort active XIRR
            terminal = qty * cur_price + (frac_cash or 0)
            flows.append((today, terminal))
            active_flows.extend(flows)
        else:
            # Only include if there were actual sell transactions (not just buys that hit 0)
            if any(t.trade_type == "sell" for t in group):
                closed_flows.extend(flows)

    return {
        "active_xirr": xirr(active_flows) if active_flows else None,
        "closed_xirr": xirr(closed_flows) if closed_flows else None,
    }


def _compute_equity_curve_impl(
    db: Session,
    benchmarks: list[str] | None = None,
    today: date | None = None,
    segment: str | None = None,
) -> dict:
    today = today or date.today()
    q = select(Transaction)
    if segment:
        q = q.where(Transaction.segment == segment)
    txns = db.execute(q).scalars().all()
    if not txns:
        return {"dates": [], "series": {}}

    start = min(t.trade_date for t in txns)

    # group by (segment, symbol) — same as holdings
    inst_txns: dict[str, list[Transaction]] = defaultdict(list)
    for t in sorted(txns, key=lambda t: (t.trade_date, t.id)):
        inst_txns[f"{t.segment}:{t.symbol.upper()}"].append(t)

    # build per-instrument daily qty series + closes
    qty_series: dict[str, dict[date, float]] = {}
    close_series: dict[str, dict[date, float]] = {}
    for k, group in inst_txns.items():
        first = group[0]
        best_isin = next((t.isin for t in group if t.isin), None)
        inst = get_or_create_instrument(
            db,
            symbol=first.symbol,
            isin=best_isin,
            segment=first.segment,
            exchange=first.exchange,
        )

        sym = first.symbol.upper()
        split_actions = get_split_actions(db, sym)

        # qty over time: single O(days + events) sweep instead of O(days × events).
        # Build a chronological event list (buys before sells on same day, splits last).
        qty_by_day: dict[date, float] = {}
        all_ev: list[tuple] = []
        for t in sorted(group, key=lambda t: (t.trade_date, t.id)):
            all_ev.append((t.trade_date, 0 if t.trade_type == "buy" else 1, t))
        for ex_d, act_type, ratio in split_actions:
            all_ev.append((ex_d, 2, (act_type, ratio)))
        all_ev.sort(key=lambda x: (x[0], x[1]))

        running = 0.0
        ev_idx = 0
        d = start
        while d <= today:
            while ev_idx < len(all_ev) and all_ev[ev_idx][0] <= d:
                _, kind, payload = all_ev[ev_idx]
                if kind == 0:           # buy
                    running += payload.quantity
                elif kind == 1:         # sell
                    running = max(0.0, running - payload.quantity)
                else:                   # split / demerger
                    act_type, ratio = payload
                    if act_type != "demerger":
                        running = float(int(running * ratio))
                    # demerger: qty unchanged (only cost basis changes)
                ev_idx += 1
            qty_by_day[d] = max(0.0, running)
            d += timedelta(days=1)
        qty_series[k] = qty_by_day

        if inst is None:
            close_series[k] = {}
            continue
        try:
            raw = get_close_series(db, inst, start, today, cache_only=True)
        except Exception:
            raw = {}
        close_series[k] = fill_forward(raw, start, today)

    # daily portfolio value
    days: list[date] = []
    portfolio_values: list[float] = []
    d = start
    while d <= today:
        days.append(d)
        v = 0.0
        for k in inst_txns:
            q = qty_series[k].get(d, 0.0)
            if q <= 0:
                continue
            px = close_series[k].get(d)
            if px is None:
                continue
            v += q * px
        portfolio_values.append(v)
        d += timedelta(days=1)

    # benchmark series
    bench_values: dict[str, list[float]] = {}
    bench_to_use = benchmarks or list(BENCHMARKS.keys())
    for ticker in bench_to_use:
        if ticker not in BENCHMARKS:
            continue
        raw = fetch_benchmark_series(db, ticker, start, today, cache_only=True)
        if not raw:
            continue
        ff = fill_forward(raw, start, today)
        sorted_bench_dates = sorted(ff.keys())
        first_bench_px = ff[sorted_bench_dates[0]] if sorted_bench_dates else None

        units = 0.0
        ti = 0
        sorted_txns = sorted(txns, key=lambda t: (t.trade_date, t.id))
        vals: list[float] = []
        for d in days:
            while ti < len(sorted_txns) and sorted_txns[ti].trade_date <= d:
                t = sorted_txns[ti]
                px = ff.get(t.trade_date) or first_bench_px
                if px:
                    cash = t.quantity * t.price + t.fees
                    if t.trade_type == "buy":
                        units += cash / px
                    else:
                        # Sell proportional index units (not full stock cash amount)
                        units_to_sell = min(units, cash / px)
                        units -= units_to_sell
                ti += 1
            # Also use first_bench_px for daily valuation before ETF existed.
            # This gives a flat line (no assumed return) for the pre-launch period,
            # then real performance from the launch date onward.
            day_px = ff.get(d) or first_bench_px
            vals.append(units * day_px if day_px else 0.0)
        bench_values[ticker] = vals

    # Rebase ALL series to 100 on the portfolio's first nonzero date.
    # Using the same base_idx for every series is what makes the comparison valid —
    # "if you invested the same rupees on the same dates into X instead, where would you be."
    base_idx = next((i for i, v in enumerate(portfolio_values) if v > 0), 0)
    base_p = portfolio_values[base_idx] or 1
    rebased_p = [
        (v / base_p * 100) if i >= base_idx else None
        for i, v in enumerate(portfolio_values)
    ]
    rebased_b: dict[str, list[float | None]] = {}
    for ticker, vals in bench_values.items():
        # Use portfolio's base_idx so all lines start at 100 on the same date.
        # With the first_bench_px backfill, benchmark vals[base_idx] is always > 0.
        bbase = vals[base_idx] if base_idx < len(vals) and vals[base_idx] > 0 else None
        if not bbase:
            # Fallback: benchmark ETF launched after portfolio start — find its own first value.
            # Lines before that date will be None (gap shown in chart).
            bidx = next((i for i, v in enumerate(vals) if v > 0), None)
            bbase = vals[bidx] if bidx is not None else None
        if not bbase:
            continue
        rebased_b[ticker] = [
            (v / bbase * 100) if v and v > 0 else None
            for v in vals
        ]

    return {
        "dates": [d.isoformat() for d in days],
        "base_date": days[base_idx].isoformat() if days else None,
        "series": {
            "Portfolio": rebased_p,
            **{BENCHMARKS[t]: rebased_b[t] for t in rebased_b},
        },
    }
