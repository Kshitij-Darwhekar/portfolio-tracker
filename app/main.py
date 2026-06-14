from __future__ import annotations

import base64
import csv
import io
import os
import secrets
from datetime import date, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .analytics import (
    compute_allocation_breakdown,
    compute_equity_curve,
    compute_fi_holdings,
    compute_fi_summary,
    compute_holdings,
    compute_period_xirr,
    compute_realized_pnl_by_period,
    compute_summary,
    compute_xirr_split,
    cached_call,
    invalidate_analytics_cache,
    find_orphan_sells,
    portfolio_cashflows,
)
from .corporate_actions import auto_fetch_all
from .fi_rates import load_fi_rates, update_fi_rate
from .db import (BondDetail, CorporateAction, EPFEntry, FixedIncome,
                 GlobalEquityTransaction, Instrument, NWSnapshot, SIPSchedule, Transaction,
                 get_session, init_db)
from .importer import import_tradebook
from .bond_analytics import compute_bond_holdings
from .sip_processor import commit_sip_schedule, preview_sip_schedule
from .categorizer import auto_detect_category, ensure_all_categorised
from .global_equity_analytics import (
    compute_global_equity_curve,
    compute_global_tax,
    compute_global_xirr,
)
from .global_equity_importer import import_indmoney_global
from .epf_importer import import_epf_passbook
from .networth import compute_networth, invalidate_nw_cache
from .mf_importer import import_cas_pdf
from .prices import BENCHMARKS, add_symbol_alias, list_symbol_aliases, remove_symbol_alias
from .xirr import xirr

STATIC_DIR = Path(__file__).resolve().parent / "static"

# --- security config (all opt-in via environment) ---
# APP_PASSWORD unset/empty  → auth disabled (default; same as before).
# APP_PASSWORD set          → every request requires HTTP Basic Auth.
# Browsers prompt once and cache the credentials, so the SPA + API just work.
APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
# Debug endpoints are off unless explicitly enabled.
ENABLE_DEBUG_ENDPOINTS = os.environ.get("ENABLE_DEBUG_ENDPOINTS", "").strip().lower() in ("1", "true", "yes", "on")

app = FastAPI(title="Portfolio Tracker", version="0.7.4")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Gate the whole app behind HTTP Basic Auth when APP_PASSWORD is set.
    No-op when unset, so local/dev use is unchanged. Uses constant-time
    comparison to avoid leaking credentials via timing."""
    if APP_PASSWORD:
        header = request.headers.get("Authorization", "")
        authorized = False
        if header.startswith("Basic "):
            try:
                user, _, pwd = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                authorized = (
                    secrets.compare_digest(user, APP_USERNAME)
                    and secrets.compare_digest(pwd, APP_PASSWORD)
                )
            except Exception:
                authorized = False
        if not authorized:
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Portfolio Tracker"'},
            )
    return await call_next(request)


@app.on_event("startup")
def _startup() -> None:
    init_db()


# --- root ---

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


# --- transactions ---

class TxnIn(BaseModel):
    trade_date: date
    symbol: str
    isin: str | None = None
    exchange: str | None = None
    segment: str = Field(pattern="^(EQ|MF)$")
    trade_type: str = Field(pattern="^(buy|sell)$")
    quantity: float = Field(gt=0)
    price: float = Field(ge=0)
    fees: float = 0
    notes: str | None = None


class TxnOut(TxnIn):
    id: int
    source: str
    trade_id: str | None
    created_at: datetime


def _txn_to_dict(t: Transaction) -> dict:
    return {
        "id": t.id,
        "trade_date": t.trade_date.isoformat(),
        "symbol": t.symbol,
        "isin": t.isin,
        "exchange": t.exchange,
        "segment": t.segment,
        "trade_type": t.trade_type,
        "quantity": t.quantity,
        "price": t.price,
        "fees": t.fees,
        "notes": t.notes,
        "source": t.source,
        "trade_id": t.trade_id,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


@app.get("/api/transactions")
def list_transactions(db: Session = Depends(get_session)):
    rows = db.execute(select(Transaction).order_by(Transaction.trade_date.desc(), Transaction.id.desc())).scalars().all()
    return [_txn_to_dict(t) for t in rows]


@app.post("/api/transactions")
def create_transaction(payload: TxnIn, db: Session = Depends(get_session)):
    txn = Transaction(
        trade_date=payload.trade_date,
        symbol=payload.symbol.upper().strip(),
        isin=payload.isin.strip() if payload.isin else None,
        exchange=payload.exchange,
        segment=payload.segment,
        trade_type=payload.trade_type,
        quantity=payload.quantity,
        price=payload.price,
        fees=payload.fees,
        notes=payload.notes,
        source="manual",
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)
    return _txn_to_dict(txn)


@app.patch("/api/transactions/{txn_id}")
def update_transaction(txn_id: int, payload: TxnIn, db: Session = Depends(get_session)):
    txn = db.get(Transaction, txn_id)
    if not txn:
        raise HTTPException(404, "Not found")
    for k, v in payload.model_dump().items():
        setattr(txn, k, v)
    db.commit()
    db.refresh(txn)
    return _txn_to_dict(txn)


@app.delete("/api/transactions/{txn_id}")
def delete_transaction(txn_id: int, db: Session = Depends(get_session)):
    txn = db.get(Transaction, txn_id)
    if not txn:
        raise HTTPException(404, "Not found")
    db.delete(txn)
    db.commit()
    return {"ok": True}


@app.get("/api/transactions.csv")
def export_csv(db: Session = Depends(get_session)):
    rows = db.execute(select(Transaction).order_by(Transaction.trade_date)).scalars().all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "trade_date", "symbol", "isin", "exchange", "segment",
        "trade_type", "quantity", "price", "fees", "notes", "source", "trade_id",
    ])
    for t in rows:
        w.writerow([
            t.trade_date.isoformat(), t.symbol, t.isin or "", t.exchange or "", t.segment,
            t.trade_type, t.quantity, t.price, t.fees, t.notes or "", t.source, t.trade_id or "",
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=transactions.csv"},
    )


# --- import ---

@app.post("/api/import")
async def import_file(file: UploadFile = File(...), db: Session = Depends(get_session)):
    content = await file.read()
    result = import_tradebook(db, file.filename or "upload.csv", content)
    return JSONResponse(result)


@app.post("/api/epf/debug-pdf")
async def debug_epf_pdf(file: UploadFile = File(...)):
    """Extract text from the EPF PDF and return it for debugging.
    PII patterns (UAN, Member ID, DOB, mobile) are redacted automatically.
    Use this when the normal import returns 0 entries to see what text PyMuPDF extracts.
    """
    if not ENABLE_DEBUG_ENDPOINTS:
        raise HTTPException(404, "Not found")
    import fitz, tempfile, os, re
    content = await file.read()
    pii_patterns = [
        re.compile(r'\b\d{12}\b'),              # 12-digit UAN
        re.compile(r'[A-Z]{2}\w{7,}'),          # Member ID-like
        re.compile(r'\b\d{10}\b'),               # mobile
        re.compile(r'\d{2}[/-]\d{2}[/-]\d{4}(?!\s*\d)'),  # keep dates used in transactions
    ]
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        doc = fitz.open(tmp_path)
        pages_text = []
        num_pages = len(doc)
        for i, page in enumerate(doc):
            raw = page.get_text()
            clean = re.sub(r'\b\d{12}\b', '[UAN-REDACTED]', raw)
            clean = re.sub(r'\b\d{10}\b', '[MOBILE-REDACTED]', clean)
            pages_text.append(f"=== PAGE {i+1} ===\n{clean[:2000]}")
        doc.close()   # must close before unlink on Windows
        return {"pages": num_pages, "text_preview": "\n".join(pages_text[:3])}
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass  # ignore if already deleted


@app.post("/api/import-epf")
async def import_epf(file: UploadFile = File(...), db: Session = Depends(get_session)):
    """Import an EPFO Member Passbook PDF.

    Only financial transaction data is extracted — UAN, Member ID, Name,
    DOB, mobile and establishment details are never stored.
    The PDF must be unlocked (save a password-free copy if needed).
    """
    content = await file.read()
    import tempfile, os
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        result = import_epf_passbook(db, tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
    return JSONResponse(result)


@app.get("/api/epf")
def list_epf(db: Session = Depends(get_session)):
    rows = db.execute(select(EPFEntry).order_by(EPFEntry.month)).scalars().all()
    return [{
        "id": r.id, "entry_type": r.entry_type,
        "month": r.month.isoformat(),
        "employee_share": r.employee_share, "employer_share": r.employer_share,
        "pension_contrib": r.pension_contrib,
        "employee_withdrawal": r.employee_withdrawal,
        "employer_withdrawal": r.employer_withdrawal,
    } for r in rows]


@app.delete("/api/epf/{epf_id}")
def delete_epf(epf_id: int, db: Session = Depends(get_session)):
    row = db.get(EPFEntry, epf_id)
    if not row:
        raise HTTPException(404, "Not found")
    db.delete(row)
    db.commit()
    return {"ok": True}


@app.get("/api/epf/summary")
def epf_summary(db: Session = Depends(get_session)):
    """Current EPF balance and year-wise breakdown."""
    rows = db.execute(select(EPFEntry).order_by(EPFEntry.month)).scalars().all()
    today = date.today()
    fy_year = today.year if today.month >= 4 else today.year - 1
    fy_start = date(fy_year, 4, 1)

    total_emp  = sum(r.employee_share - r.employee_withdrawal for r in rows)
    total_empr = sum(r.employer_share - r.employer_withdrawal for r in rows)
    total_int  = sum(r.employee_share + r.employer_share for r in rows
                     if r.entry_type == "interest")
    total_pension = sum(r.pension_contrib for r in rows)
    balance    = total_emp + total_empr  # interest already included in contribution rows

    fy_emp  = sum(r.employee_share for r in rows
                  if r.entry_type == "contribution" and r.month >= fy_start)
    fy_empr = sum(r.employer_share for r in rows
                  if r.entry_type == "contribution" and r.month >= fy_start)

    # Monthly timeline for chart
    monthly = [
        {"month": r.month.isoformat(), "entry_type": r.entry_type,
         "employee": r.employee_share, "employer": r.employer_share,
         "pension": r.pension_contrib}
        for r in rows if r.entry_type == "contribution"
    ]

    return {
        "total_employee_contributions": round(total_emp, 2),
        "total_employer_contributions": round(total_empr, 2),
        "total_interest_credited":      round(total_int, 2),
        "total_pension_contributions":  round(total_pension, 2),
        "estimated_balance":            round(balance, 2),
        "fy_employee_contribution":     round(fy_emp, 2),
        "fy_employer_contribution":     round(fy_empr, 2),
        "months_imported":              len([r for r in rows if r.entry_type == "contribution"]),
        "monthly_contributions":        monthly,
        "as_of": today.isoformat(),
    }


@app.post("/api/import-cas")
async def import_cas(file: UploadFile = File(...), db: Session = Depends(get_session)):
    """Import a CAMS+KFintech Combined CAS PDF (Consolidated Account Statement).

    The PDF must be unlocked/unencrypted. If your CAS is password-protected,
    open it in a PDF viewer and save a copy without a password first.
    """
    content = await file.read()
    # Write to a temp file since PyMuPDF needs a file path
    import tempfile, os
    suffix = ".pdf"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        result = import_cas_pdf(db, tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
    return JSONResponse(result)


# --- analytics ---

@app.get("/api/holdings")
def holdings(
    segment: str | None = Query(None, description="EQ | MF | None for all"),
    db: Session = Depends(get_session),
):
    rows = compute_holdings(db, segment=segment or None)
    return [r.__dict__ for r in rows]


@app.get("/api/summary")
def summary(
    segment: str | None = Query(None),
    db: Session = Depends(get_session),
):
    return cached_call(("summary", segment or "all"), db,
                       lambda: compute_summary(db, segment=segment or None))


@app.get("/api/equity-curve")
def equity_curve(
    benchmarks: str | None = Query(None, description="Comma-separated tickers"),
    segment: str | None = Query(None),
    db: Session = Depends(get_session),
):
    bench_list = [b.strip() for b in benchmarks.split(",") if b.strip()] if benchmarks else None
    return compute_equity_curve(db, bench_list, segment=segment or None)


@app.get("/api/benchmarks")
def list_benchmarks():
    return [{"ticker": t, "name": n} for t, n in BENCHMARKS.items()]


@app.post("/api/refresh-prices")
def refresh_prices(db: Session = Depends(get_session)):
    """Warm the price cache for all held instruments + benchmarks.

    Uses live API calls (cache_only=False) so subsequent analytics queries
    can run fully from cache and respond in milliseconds.
    """
    from datetime import date as date_cls, timedelta
    from .prices import (
        get_close_series, fetch_benchmark_series, BENCHMARKS,
        get_or_create_instrument, latest_close,
    )
    from sqlalchemy import select as sa_select

    today = date_cls.today()
    txns = db.execute(sa_select(Transaction)).scalars().all()
    if not txns:
        return {"ok": True, "as_of": today.isoformat(), "instruments_refreshed": 0}

    # Get all unique instruments
    from collections import defaultdict
    bucket = defaultdict(list)
    for t in txns:
        bucket[f"{t.segment}:{t.symbol.upper()}"].append(t)

    start = min(t.trade_date for t in txns)
    refreshed = 0
    for _key, group in bucket.items():
        first = group[0]
        best_isin = next((t.isin for t in group if t.isin), None)
        inst = get_or_create_instrument(
            db, symbol=first.symbol, isin=best_isin,
            segment=first.segment, exchange=first.exchange,
        )
        if inst:
            try:
                get_close_series(db, inst, start, today, cache_only=False)
                refreshed += 1
            except Exception:
                pass

    # Refresh benchmark series
    for ticker in BENCHMARKS:
        try:
            fetch_benchmark_series(db, ticker, start, today, cache_only=False)
        except Exception:
            pass

    db.commit()
    # Prices changed but transactions didn't, so the fingerprint-based caches
    # wouldn't auto-invalidate — clear them explicitly so values reflect new prices.
    invalidate_analytics_cache()
    invalidate_nw_cache()
    s = compute_summary(db)   # also re-warms the holdings cache
    # Pre-warm the heavy caches so the next page load (incl. the nightly cron's,
    # and the user's first load of the day) is fast instead of cold.
    try:
        compute_equity_curve(db)
        compute_networth(db)
    except Exception:
        pass
    return {"ok": True, "as_of": s["as_of"], "current_value": s["current_value"],
            "instruments_refreshed": refreshed}


@app.get("/api/xirr-analysis")
def xirr_analysis(
    period: str | None = Query(None),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    segment: str | None = Query(None),
    db: Session = Depends(get_session),
):
    """Portfolio + benchmark XIRR for a specific period.

    period shortcuts: all | 1y | 3y | 5y | current_fy | prev_fy | fy_YYYY
    Or pass explicit from_date / to_date.
    """
    today = date.today()
    if period == "1y":
        from_date = today.replace(year=today.year - 1)
    elif period == "3y":
        from_date = today.replace(year=today.year - 3)
    elif period == "5y":
        from_date = today.replace(year=today.year - 5)
    elif period == "current_fy":
        from_date = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
    elif period == "prev_fy":
        fy = today.year - 1 if today.month >= 4 else today.year - 2
        from_date, to_date = date(fy, 4, 1), date(fy + 1, 3, 31)
    elif period and period.startswith("fy_"):
        y = int(period[3:])
        from_date, to_date = date(y, 4, 1), date(y + 1, 3, 31)
    # period == "all" or None → from_date stays None
    def _compute():
        result = compute_period_xirr(db, from_date, to_date, segment=segment or None)
        # Active vs closed split is meaningful only on the all-time view; subperiod XIRRs
        # use opening/closing portfolio values so the split wouldn't map cleanly onto them.
        if from_date is None:
            result.update(compute_xirr_split(db, segment=segment or None))
        return result
    return cached_call(("xirr", period, str(from_date), str(to_date), segment or "all"), db, _compute)


@app.get("/api/allocation")
def get_allocation(db: Session = Depends(get_session)):
    """Market cap + sector breakdown for direct EQ holdings; SEBI-category breakdown for MFs."""
    return compute_allocation_breakdown(db)


@app.post("/api/refresh-market-meta")
def refresh_market_meta(db: Session = Depends(get_session)):
    """Refresh sector + market_cap_category for all EQ instruments.

    Cap category comes from NSE's official rank-based lists (NIFTY 100 / Midcap 150 /
    Smallcap 250), refreshed here once; sector still comes from yfinance .info.
    Run after imports or twice a year when NSE updates its lists.
    """
    from .prices import fetch_instrument_meta
    from .cap_classification import _load_cap_index

    n_listed = _load_cap_index(force=True)   # fetch the NSE constituent lists once
    instruments = db.execute(
        select(Instrument).where(Instrument.segment == "EQ")
    ).scalars().all()

    updated, failed, classified = 0, 0, 0
    for inst in instruments:
        result = fetch_instrument_meta(db, inst)
        if result.get("market_cap_category"):
            classified += 1
        if result.get("sector") or result.get("market_cap_category"):
            updated += 1
        else:
            failed += 1

    return {
        "updated": updated, "failed": failed, "total": len(instruments),
        "cap_classified": classified, "cap_list_size": n_listed,
    }


@app.get("/api/realized-pnl")
def realized_pnl(
    period: str | None = Query(None, description="current_fy | prev_fy | current_cy | all"),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    segment: str | None = Query(None),
    db: Session = Depends(get_session),
):
    """Realized P&L for sells within a period, with STCG/LTCG split.

    Indian FY = April 1 – March 31.
    STCG = held < 12 months (taxed at 20%).
    LTCG = held ≥ 12 months (taxed at 12.5% above ₹1.25L exemption).
    """
    today = date.today()
    if period == "current_fy":
        fy_start = date(today.year if today.month >= 4 else today.year - 1, 4, 1)
        from_date, to_date = fy_start, today
    elif period == "prev_fy":
        fy_year = today.year - 1 if today.month >= 4 else today.year - 2
        from_date = date(fy_year, 4, 1)
        to_date = date(fy_year + 1, 3, 31)
    elif period == "current_cy":
        from_date = date(today.year, 1, 1)
        to_date = today
    # if period == "all" or None, from_date/to_date stay as None → all-time
    return cached_call(("realized", str(from_date), str(to_date), segment or "all"), db,
                       lambda: compute_realized_pnl_by_period(db, from_date, to_date, segment=segment or None))


@app.get("/api/data-quality")
def data_quality(db: Session = Depends(get_session)):
    """Returns orphan sells (missing buy transactions) that are silently ignored.

    Common causes: IPO allotments, demerger receipts, off-market transfers.
    For each entry, add a manual buy transaction at the IPO/allotment price to fix.
    """
    orphans = cached_call(("orphans",), db, lambda: find_orphan_sells(db))
    total_proceeds = sum(o["proceeds"] for o in orphans)
    return {
        "orphan_count": len(orphans),
        "total_missing_proceeds": round(total_proceeds, 2),
        "message": (
            f"{len(orphans)} sell transaction(s) have no matching buy. "
            f"These are likely IPO allotments or corporate action receipts. "
            f"Add buy transactions at the allotment/issue price to capture "
            f"Rs.{total_proceeds:,.0f} in missing realized P&L."
        ) if orphans else "No data quality issues found.",
        "orphans": orphans,
    }


@app.get("/api/instrument-categories")
def get_instrument_categories(db: Session = Depends(get_session)):
    """List all instruments with their current and auto-detected category."""
    ensure_all_categorised(db)
    insts = db.execute(select(Instrument)).scalars().all()
    return [{
        "isin": i.isin, "symbol": i.symbol, "name": i.name or i.symbol,
        "segment": i.segment, "category": i.asset_category,
        "auto_detected": auto_detect_category(i.isin, i.symbol, i.name, i.segment),
    } for i in sorted(insts, key=lambda x: x.symbol or "")]


@app.patch("/api/instrument-categories/{isin}")
def update_instrument_category(isin: str, payload: dict,
                                db: Session = Depends(get_session)):
    inst = db.get(Instrument, isin)
    if not inst:
        raise HTTPException(404, "Instrument not found")
    valid = {"equity", "debt", "gold", "hybrid", "cash", "silver", "epf", "other"}
    cat = str(payload.get("category", "")).lower()
    if cat not in valid:
        raise HTTPException(400, f"category must be one of: {sorted(valid)}")
    inst.asset_category = cat
    db.commit()
    return {"isin": isin, "category": cat}


@app.post("/api/instrument-categories/auto-detect")
def auto_detect_all_categories(db: Session = Depends(get_session)):
    """Re-run auto-detection for all instruments (overwrites existing categories)."""
    insts = db.execute(select(Instrument)).scalars().all()
    for inst in insts:
        inst.asset_category = auto_detect_category(
            inst.isin, inst.symbol, inst.name, inst.segment)
    db.commit()
    return {"updated": len(insts)}


@app.post("/api/import-global")
async def import_global_equity(file: UploadFile = File(...),
                                db: Session = Depends(get_session)):
    """Import INDMoney Global Equity XLS/XLSX order book."""
    import tempfile, os
    content = await file.read()
    suffix = Path(file.filename or "upload.xls").suffix or ".xls"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        result = import_indmoney_global(db, tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
    return JSONResponse(result)


@app.get("/api/global-equity")
def list_global_equity(db: Session = Depends(get_session)):
    rows = db.execute(
        select(GlobalEquityTransaction).order_by(
            GlobalEquityTransaction.trade_date.desc(),
            GlobalEquityTransaction.id.desc()
        )
    ).scalars().all()
    return [{
        "id": r.id, "stock_name": r.stock_name, "symbol": r.symbol,
        "trade_date": r.trade_date.isoformat(),
        "trade_type": r.trade_type, "quantity": r.quantity,
        "price_usd": r.price_usd, "amount_usd": r.amount_usd,
        "fees_usd": r.fees_usd, "exchange_rate": r.exchange_rate,
        "amount_inr": round(r.amount_usd * r.exchange_rate, 2)
                      if r.exchange_rate else None,
        "source": r.source,
    } for r in rows]


class GlobalTxnIn(BaseModel):
    symbol:         str
    stock_name:     str | None = None
    trade_date:     date
    trade_type:     str = Field(pattern="^(buy|sell)$")
    quantity:       float = Field(gt=0)
    price_usd:      float = Field(gt=0)
    fees_usd:       float = 0.0
    exchange_rate:  float | None = None   # USD/INR; fetched automatically if None
    notes:          str | None = None


@app.post("/api/global-equity")
def create_global_txn(payload: GlobalTxnIn, db: Session = Depends(get_session)):
    rate = payload.exchange_rate
    if rate is None:
        from .global_equity_importer import _fetch_usdinr
        rates = _fetch_usdinr({payload.trade_date})
        rate = rates.get(payload.trade_date)
    txn = GlobalEquityTransaction(
        symbol=payload.symbol.upper().strip(),
        stock_name=payload.stock_name,
        trade_date=payload.trade_date,
        trade_type=payload.trade_type,
        quantity=payload.quantity,
        price_usd=payload.price_usd,
        amount_usd=payload.quantity * payload.price_usd,
        fees_usd=payload.fees_usd,
        exchange_rate=rate,
        notes=payload.notes,
        source="manual",
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)
    return {"id": txn.id, "symbol": txn.symbol, "exchange_rate": rate}


@app.delete("/api/global-equity/{txn_id}")
def delete_global_txn(txn_id: int, db: Session = Depends(get_session)):
    r = db.get(GlobalEquityTransaction, txn_id)
    if not r:
        raise HTTPException(404, "Not found")
    db.delete(r)
    db.commit()
    return {"ok": True}


@app.get("/api/global-equity/xirr")
def global_xirr(db: Session = Depends(get_session)):
    return compute_global_xirr(db)


@app.get("/api/global-equity/equity-curve")
def global_equity_curve(db: Session = Depends(get_session)):
    return compute_global_equity_curve(db)


@app.get("/api/global-equity/tax")
def global_tax(db: Session = Depends(get_session)):
    return compute_global_tax(db)


@app.get("/api/global-equity/summary")
def global_equity_summary(db: Session = Depends(get_session)):
    """Current holdings, P&L in USD and INR, live prices from yfinance."""
    import yfinance as yf
    from collections import defaultdict
    from datetime import date as date_cls

    today = date_cls.today()
    rows = db.execute(select(GlobalEquityTransaction).order_by(
        GlobalEquityTransaction.trade_date, GlobalEquityTransaction.id)
    ).scalars().all()

    if not rows:
        return {"holdings": [], "total_usd": 0, "total_inr": 0,
                "invested_usd": 0, "invested_inr": 0, "usdinr": None}

    # Compute holdings (weighted avg, fractional)
    qty_map:   dict[str, float] = defaultdict(float)
    cost_map:  dict[str, float] = defaultdict(float)   # total cost USD
    name_map:  dict[str, str]   = {}
    txns_map:  dict[str, list]  = defaultdict(list)

    for r in rows:
        sym = r.symbol
        name_map[sym] = r.stock_name or sym
        txns_map[sym].append(r)
        if r.trade_type == "buy":
            cost_map[sym]  += r.amount_usd + r.fees_usd
            qty_map[sym]   += r.quantity
        else:
            sell_qty        = min(r.quantity, qty_map[sym])
            if qty_map[sym] > 0:
                cost_map[sym] -= (sell_qty / qty_map[sym]) * cost_map[sym]
            qty_map[sym]   -= sell_qty
            if qty_map[sym] < 1e-9:
                qty_map[sym], cost_map[sym] = 0.0, 0.0

    # Fetch live USD/INR
    try:
        fx = yf.Ticker("USDINR=X").history(period="5d")
        usdinr = float(fx["Close"].iloc[-1]) if not fx.empty else 84.0
    except Exception:
        usdinr = 84.0

    # Fetch live prices for held symbols
    held = [s for s, q in qty_map.items() if q > 1e-6]
    prices: dict[str, float] = {}
    if held:
        try:
            tickers = yf.download(held, period="5d", auto_adjust=True,
                                   progress=False)["Close"]
            if hasattr(tickers, "iloc"):
                for sym in held:
                    col = tickers[sym] if sym in tickers.columns else tickers
                    prices[sym] = float(col.dropna().iloc[-1])
        except Exception:
            pass

    holdings = []
    total_val_usd = 0.0
    total_cost_usd = 0.0

    for sym in sorted(qty_map):
        qty = qty_map[sym]
        cost = cost_map[sym]
        if qty < 1e-6 and cost < 0.01:
            # Fully exited
            from .xirr import xirr as compute_xirr
            flows = []
            for t in txns_map[sym]:
                amt = (t.amount_usd + t.fees_usd) * (1 if t.trade_type == "buy" else -1)
                flows.append((t.trade_date, -amt if t.trade_type == "buy" else abs(amt) - t.fees_usd))
            realized_pnl = sum(a for _, a in flows)
            holdings.append({
                "symbol": sym, "name": name_map[sym],
                "quantity": 0, "avg_cost_usd": 0,
                "current_price_usd": prices.get(sym),
                "invested_usd": 0, "current_value_usd": 0,
                "realized_pnl_usd": round(realized_pnl, 4),
                "unrealized_pnl_usd": 0,
                "status": "closed",
            })
            continue

        avg_cost = cost / qty if qty > 0 else 0
        cur_price = prices.get(sym, 0)
        cur_value = qty * cur_price
        total_val_usd  += cur_value
        total_cost_usd += cost

        holdings.append({
            "symbol": sym, "name": name_map[sym],
            "quantity": round(qty, 8),
            "avg_cost_usd": round(avg_cost, 4),
            "current_price_usd": round(cur_price, 4) if cur_price else None,
            "invested_usd": round(cost, 4),
            "current_value_usd": round(cur_value, 4) if cur_price else None,
            "current_value_inr": round(cur_value * usdinr, 2) if cur_price else None,
            "invested_inr": round(cost * usdinr, 2),
            "unrealized_pnl_usd": round(cur_value - cost, 4) if cur_price else None,
            "unrealized_pnl_inr": round((cur_value - cost) * usdinr, 2) if cur_price else None,
            "pct_return": round((cur_value - cost) / cost * 100, 2) if cost > 0 and cur_price else None,
            "status": "active",
        })

    return {
        "holdings":      sorted(holdings, key=lambda h: h.get("current_value_usd") or 0, reverse=True),
        "total_usd":     round(total_val_usd, 2),
        "total_inr":     round(total_val_usd * usdinr, 2),
        "invested_usd":  round(total_cost_usd, 2),
        "invested_inr":  round(total_cost_usd * usdinr, 2),
        "usdinr":        round(usdinr, 2),
        "as_of":         today.isoformat(),
    }


@app.get("/api/bonds")
def list_bonds(db: Session = Depends(get_session)):
    return compute_bond_holdings(db)


@app.get("/api/bonds/details")
def list_bond_details(db: Session = Depends(get_session)):
    rows = db.execute(select(BondDetail).order_by(BondDetail.symbol)).scalars().all()
    return [{
        "id": b.id, "symbol": b.symbol, "isin": b.isin,
        "bond_type": b.bond_type, "full_name": b.full_name,
        "issue_price": b.issue_price, "issue_date": b.issue_date.isoformat(),
        "maturity_date": b.maturity_date.isoformat(),
        "coupon_rate": b.coupon_rate, "coupon_frequency": b.coupon_frequency,
        "capital_gains_exempt_at_maturity": b.capital_gains_exempt_at_maturity,
        "notes": b.notes,
    } for b in rows]


class BondDetailIn(BaseModel):
    symbol:           str
    isin:             str | None = None
    bond_type:        str = "SGB"
    full_name:        str | None = None
    issue_price:      float = Field(gt=0)
    issue_date:       date
    maturity_date:    date
    coupon_rate:      float = 0.0
    coupon_frequency: str = "semi-annual"
    capital_gains_exempt_at_maturity: bool = False
    quantity:         float = 0.0
    purchase_price:   float | None = None
    purchase_date:    date | None = None
    price_override:   float | None = None   # manual current price (overrides auto-fetch)
    notes:            str | None = None


@app.post("/api/bonds/details")
def create_bond_detail(payload: BondDetailIn, db: Session = Depends(get_session)):
    existing = db.execute(
        select(BondDetail).where(BondDetail.symbol == payload.symbol.upper())
    ).scalar_one_or_none()
    if existing:
        for k, v in payload.model_dump().items():
            if k == "symbol":
                continue
            setattr(existing, k, v)
        db.commit()
        return {"updated": True, "symbol": existing.symbol}
    bd = BondDetail(symbol=payload.symbol.upper().strip(), **{
        k: v for k, v in payload.model_dump().items() if k != "symbol"
    })
    db.add(bd)
    db.commit()
    return {"created": True, "symbol": bd.symbol}


@app.delete("/api/bonds/details/{bond_id}")
def delete_bond_detail(bond_id: int, db: Session = Depends(get_session)):
    b = db.get(BondDetail, bond_id)
    if not b:
        raise HTTPException(404, "Not found")
    db.delete(b)
    db.commit()
    return {"ok": True}


@app.get("/api/nw-snapshots")
def list_nw_snapshots(db: Session = Depends(get_session)):
    rows = db.execute(select(NWSnapshot).order_by(NWSnapshot.snap_date)).scalars().all()
    return [{
        "id": r.id, "snap_date": r.snap_date.isoformat(),
        "amount": r.amount, "label": r.label, "notes": r.notes,
    } for r in rows]


class NWSnapshotIn(BaseModel):
    snap_date: date
    amount:    float = Field(gt=0)
    label:     str | None = None
    notes:     str | None = None


@app.post("/api/nw-snapshots")
def create_nw_snapshot(payload: NWSnapshotIn, db: Session = Depends(get_session)):
    existing = db.execute(
        select(NWSnapshot).where(NWSnapshot.snap_date == payload.snap_date)
    ).scalar_one_or_none()
    if existing:
        existing.amount = payload.amount
        existing.label  = payload.label
        existing.notes  = payload.notes
        db.commit()
        return {"id": existing.id, "updated": True}
    row = NWSnapshot(**payload.model_dump())
    db.add(row); db.commit(); db.refresh(row)
    return {"id": row.id, "created": True}


@app.delete("/api/nw-snapshots/{snap_id}")
def delete_nw_snapshot(snap_id: int, db: Session = Depends(get_session)):
    r = db.get(NWSnapshot, snap_id)
    if not r: raise HTTPException(404, "Not found")
    db.delete(r); db.commit()
    return {"ok": True}


@app.get("/api/networth")
def networth(db: Session = Depends(get_session)):
    """Total net worth across Equity, MF, Fixed Income, and EPF.
    Includes historical monthly series, 36-month projection, and milestone dates.
    Uses cached prices only for historical data — no live API calls.
    """
    return compute_networth(db)


@app.get("/api/debug/cashflows")
def debug_cashflows(db: Session = Depends(get_session)):
    """Inspect the exact cashflow list driving portfolio XIRR.

    Useful to sanity-check the XIRR result. Final inflow is today's portfolio
    value; sum across all flows shows aggregate net P&L.
    """
    if not ENABLE_DEBUG_ENDPOINTS:
        raise HTTPException(404, "Not found")
    today = date.today()
    txns = db.execute(select(Transaction).order_by(Transaction.trade_date, Transaction.id)).scalars().all()
    flows = portfolio_cashflows(db, txns)
    holdings = compute_holdings(db, today)
    current_value = sum(h.current_value or 0 for h in holdings)
    if current_value > 0:
        flows.append((today, current_value))

    return {
        "as_of": today.isoformat(),
        "num_transactions": len(txns),
        "first_date": flows[0][0].isoformat() if flows else None,
        "last_date": flows[-1][0].isoformat() if flows else None,
        "total_outflows": sum(a for _, a in flows if a < 0),
        "total_inflows": sum(a for _, a in flows if a > 0),
        "net": sum(a for _, a in flows),
        "current_portfolio_value": current_value,
        "unpriced_holdings": [h.symbol for h in holdings if h.quantity > 0 and h.current_value is None],
        "computed_xirr": xirr(flows),
        "flows": [[d.isoformat(), round(a, 2)] for d, a in flows],
    }


# --- corporate actions ---

class CAIn(BaseModel):
    symbol: str
    isin: str | None = None
    action_type: str = Field(pattern="^(split|bonus|dividend|merger|demerger)$")
    ex_date: date
    ratio: float | None = None
    amount_per_share: float | None = None
    notes: str | None = None


def _ca_to_dict(ca: CorporateAction) -> dict:
    return {
        "id": ca.id,
        "symbol": ca.symbol,
        "isin": ca.isin,
        "action_type": ca.action_type,
        "ex_date": ca.ex_date.isoformat(),
        "ratio": ca.ratio,
        "amount_per_share": ca.amount_per_share,
        "notes": ca.notes,
        "source": ca.source,
        "created_at": ca.created_at.isoformat() if ca.created_at else None,
    }


@app.get("/api/corporate-actions")
def list_corporate_actions(symbol: str | None = None, db: Session = Depends(get_session)):
    q = select(CorporateAction).order_by(CorporateAction.ex_date.desc())
    if symbol:
        q = q.where(CorporateAction.symbol == symbol.upper())
    rows = db.execute(q).scalars().all()
    return [_ca_to_dict(r) for r in rows]


@app.post("/api/corporate-actions")
def create_corporate_action(payload: CAIn, db: Session = Depends(get_session)):
    ca = CorporateAction(
        symbol=payload.symbol.upper().strip(),
        isin=payload.isin,
        action_type=payload.action_type,
        ex_date=payload.ex_date,
        ratio=payload.ratio,
        amount_per_share=payload.amount_per_share,
        notes=payload.notes,
        source="manual",
    )
    db.add(ca)
    db.commit()
    db.refresh(ca)
    return _ca_to_dict(ca)


@app.patch("/api/corporate-actions/{ca_id}")
def update_corporate_action(ca_id: int, payload: CAIn, db: Session = Depends(get_session)):
    ca = db.get(CorporateAction, ca_id)
    if not ca:
        raise HTTPException(404, "Not found")
    for k, v in payload.model_dump().items():
        setattr(ca, k, v)
    ca.symbol = ca.symbol.upper().strip()
    db.commit()
    db.refresh(ca)
    return _ca_to_dict(ca)


@app.delete("/api/corporate-actions/{ca_id}")
def delete_corporate_action(ca_id: int, db: Session = Depends(get_session)):
    ca = db.get(CorporateAction, ca_id)
    if not ca:
        raise HTTPException(404, "Not found")
    db.delete(ca)
    db.commit()
    return {"ok": True}


@app.post("/api/corporate-actions/auto-fetch")
def auto_fetch_corporate_actions(db: Session = Depends(get_session)):
    result = auto_fetch_all(db)
    return result


# --- fixed income (FD / RD) ---

class FIIn(BaseModel):
    fi_type:          str = Field(pattern="^(FD_CUM|FD_NON_CUM|RD)$")
    bank:             str
    account_no:       str | None = None
    amount:           float = Field(gt=0)
    start_date:       date
    maturity_date:    date
    interest_rate:    float = Field(gt=0, lt=100)
    compounding:      str = "quarterly"
    payout_frequency: str | None = None
    initial_deposit:  float = 0.0    # RD only: lump-sum on start date
    is_tax_saver:     bool = False
    notes:            str | None = None


def _fi_to_dict(fi: FixedIncome) -> dict:
    from .fi_calc import (
        current_value as fi_cv, maturity_value as fi_mv,
        interest_earned_total, interest_this_fy,
    )
    today = date.today()
    cv  = fi_cv(fi)
    mv  = fi_mv(fi)
    ie  = interest_earned_total(fi, today)
    fy  = interest_this_fy(fi, today)
    dtm = (fi.maturity_date - today).days
    return {
        "id": fi.id, "fi_type": fi.fi_type, "bank": fi.bank,
        "account_no": fi.account_no, "amount": fi.amount,
        "start_date": fi.start_date.isoformat(),
        "maturity_date": fi.maturity_date.isoformat(),
        "interest_rate": fi.interest_rate, "compounding": fi.compounding,
        "payout_frequency": fi.payout_frequency,
        "initial_deposit": fi.initial_deposit or 0.0,
        "is_tax_saver": fi.is_tax_saver, "notes": fi.notes,
        "current_value": round(cv, 2), "maturity_value": round(mv, 2),
        "interest_earned": round(ie, 2), "interest_this_fy": round(fy, 2),
        "days_to_maturity": dtm, "status": "matured" if dtm < 0 else "active",
    }


@app.get("/api/fi")
def list_fi(db: Session = Depends(get_session)):
    records = db.execute(select(FixedIncome).order_by(FixedIncome.start_date)).scalars().all()
    return [_fi_to_dict(r) for r in records]


@app.post("/api/fi")
def create_fi(payload: FIIn, db: Session = Depends(get_session)):
    fi = FixedIncome(**payload.model_dump())
    db.add(fi)
    db.commit()
    db.refresh(fi)
    return _fi_to_dict(fi)


@app.patch("/api/fi/{fi_id}")
def update_fi(fi_id: int, payload: FIIn, db: Session = Depends(get_session)):
    fi = db.get(FixedIncome, fi_id)
    if not fi:
        raise HTTPException(404, "Not found")
    for k, v in payload.model_dump().items():
        setattr(fi, k, v)
    db.commit()
    db.refresh(fi)
    return _fi_to_dict(fi)


@app.delete("/api/fi/{fi_id}")
def delete_fi(fi_id: int, db: Session = Depends(get_session)):
    fi = db.get(FixedIncome, fi_id)
    if not fi:
        raise HTTPException(404, "Not found")
    db.delete(fi)
    db.commit()
    return {"ok": True}


@app.get("/api/sip-schedules")
def list_sip_schedules(db: Session = Depends(get_session)):
    rows = db.execute(select(SIPSchedule).order_by(SIPSchedule.created_at.desc())).scalars().all()
    return [{
        "id": s.id, "isin": s.isin, "scheme_name": s.scheme_name,
        "folio": s.folio, "amount": s.amount, "sip_day": s.sip_day,
        "start_date": s.start_date.isoformat(),
        "end_date": s.end_date.isoformat() if s.end_date else None,
        "is_active": s.is_active,
        "last_synced_date": s.last_synced_date.isoformat() if s.last_synced_date else None,
        "notes": s.notes,
    } for s in rows]


class SIPScheduleIn(BaseModel):
    isin:         str
    scheme_name:  str | None = None
    folio:        str | None = None
    amount:       float = Field(gt=0)
    sip_day:      int = Field(ge=1, le=28)
    start_date:   date | None = None   # optional — defaults to today
    end_date:     date | None = None
    is_active:    bool = True
    notes:        str | None = None


@app.post("/api/sip-schedules")
def create_sip_schedule(payload: SIPScheduleIn, db: Session = Depends(get_session)):
    today = date.today()
    sched = SIPSchedule(
        isin        = payload.isin.strip(),
        scheme_name = payload.scheme_name,
        folio       = payload.folio,
        amount      = payload.amount,
        sip_day     = payload.sip_day,
        start_date  = payload.start_date or today,
        end_date    = payload.end_date,
        is_active   = payload.is_active,
        notes       = payload.notes,
    )
    db.add(sched)
    db.commit()
    db.refresh(sched)
    # Try to resolve scheme name from AMFI if not provided
    if not sched.scheme_name:
        from .prices import resolve_mf_scheme_code, _load_amfi_index
        amfi = _load_amfi_index()
        # scheme_name resolution — best effort
    return {"id": sched.id, "start_date": sched.start_date.isoformat()}


@app.patch("/api/sip-schedules/{sched_id}")
def update_sip_schedule(sched_id: int, payload: SIPScheduleIn,
                         db: Session = Depends(get_session)):
    s = db.get(SIPSchedule, sched_id)
    if not s:
        raise HTTPException(404, "Not found")
    for k, v in payload.model_dump(exclude_none=True).items():
        setattr(s, k, v)
    db.commit()
    return {"ok": True}


@app.delete("/api/sip-schedules/{sched_id}")
def delete_sip_schedule(sched_id: int, db: Session = Depends(get_session)):
    s = db.get(SIPSchedule, sched_id)
    if not s:
        raise HTTPException(404, "Not found")
    db.delete(s)
    db.commit()
    return {"ok": True}


@app.get("/api/sip-schedules/{sched_id}/preview")
def preview_sip(sched_id: int, db: Session = Depends(get_session)):
    """Show exactly what transactions would be created — no DB changes.
    Review this before calling /confirm.
    """
    s = db.get(SIPSchedule, sched_id)
    if not s:
        raise HTTPException(404, "Not found")
    items = preview_sip_schedule(db, s)
    new_count     = sum(1 for i in items if i["status"] == "new")
    skipped_count = sum(1 for i in items if i["status"] == "skipped")
    no_nav_count  = sum(1 for i in items if i["status"] == "no_nav")
    return {
        "schedule_id":   sched_id,
        "scheme_name":   s.scheme_name or s.isin,
        "total_items":   len(items),
        "new":           new_count,
        "skipped":       skipped_count,
        "no_nav":        no_nav_count,
        "items":         items,
    }


@app.post("/api/sip-schedules/{sched_id}/confirm")
def confirm_sip(sched_id: int, db: Session = Depends(get_session)):
    """Commit the previewed transactions to the database.
    Only call this after reviewing /preview and confirming the values look correct.
    """
    s = db.get(SIPSchedule, sched_id)
    if not s:
        raise HTTPException(404, "Not found")
    result = commit_sip_schedule(db, s)
    return result


@app.post("/api/sip-schedules/sync-all")
def sync_all_sips(db: Session = Depends(get_session)):
    """Sync all active SIP schedules at once.
    Returns a combined summary — no preview, commits directly.
    Use /preview first to verify individual schedules before using this.
    """
    schedules = db.execute(
        select(SIPSchedule).where(SIPSchedule.is_active == True)
    ).scalars().all()
    total_inserted = 0
    total_skipped  = 0
    results = []
    for s in schedules:
        r = commit_sip_schedule(db, s)
        total_inserted += r["inserted"]
        total_skipped  += r["skipped"]
        results.append({"id": s.id, "scheme": s.scheme_name or s.isin, **r})
    return {
        "schedules_processed": len(schedules),
        "total_inserted": total_inserted,
        "total_skipped":  total_skipped,
        "detail": results,
    }


@app.get("/api/fi/rates")
def get_fi_rates():
    """Current benchmark rates used for FI growth curve comparison."""
    return load_fi_rates()


@app.patch("/api/fi/rates")
def update_fi_rates(payload: dict):
    """Update one or more benchmark rates.

    Valid keys: savings_rate, std_fd_rate, inflation_rate  (all annual %)
    Example: {"savings_rate": 3.25, "std_fd_rate": 6.8}
    """
    updated = load_fi_rates()
    for key, val in payload.items():
        try:
            updated = update_fi_rate(key, float(val))
        except ValueError as e:
            raise HTTPException(400, str(e))
    return updated


@app.get("/api/fi/summary")
def fi_summary(db: Session = Depends(get_session)):
    return compute_fi_summary(db)


@app.get("/api/fi/chart-data")
def fi_chart_data(db: Session = Depends(get_session)):
    """Three datasets for the Fixed Income charts:

    1. maturity_timeline  — one bar per instrument: {label, start, end, value, status}
    2. cashflow_forecast  — monthly inflow projections for next 24 months
    3. fy_interest        — interest earned per financial year (historical + current)
    """
    from .fi_calc import (
        fd_non_cum_payout, rd_tenure_months,
        maturity_value as fi_mv, interest_this_fy, value_on,
    )
    from dateutil.relativedelta import relativedelta
    import calendar

    today = date.today()
    records = db.execute(select(FixedIncome).order_by(FixedIncome.start_date)).scalars().all()

    # 1. Maturity timeline
    timeline = []
    for fi in records:
        mat_v = fi_mv(fi)
        dtm = (fi.maturity_date - today).days
        timeline.append({
            "id": fi.id,
            "label": f"{fi.bank} — {fi.fi_type.replace('_', ' ')} @ {fi.interest_rate}%",
            "start": fi.start_date.isoformat(),
            "end": fi.maturity_date.isoformat(),
            "maturity_value": round(mat_v, 2),
            "principal": fi.amount,
            "status": "matured" if dtm < 0 else "active",
            "days_to_maturity": dtm,
        })

    # 2. Monthly cashflow forecast (next 24 months)
    forecast: dict[str, float] = {}
    horizon = today + relativedelta(months=24)
    for fi in records:
        # Maturity payout
        if today <= fi.maturity_date <= horizon:
            key = fi.maturity_date.strftime("%Y-%m")
            forecast[key] = forecast.get(key, 0) + fi_mv(fi)

        # Non-cumulative periodic interest payouts
        if fi.fi_type == "FD_NON_CUM" and fi.payout_frequency:
            from .fi_calc import _PAYOUT_MONTHS, fd_non_cum_payout
            months = _PAYOUT_MONTHS.get(fi.payout_frequency, 3)
            payout = fd_non_cum_payout(fi.amount, fi.interest_rate, fi.payout_frequency)
            d = fi.start_date + relativedelta(months=months)
            while d <= min(fi.maturity_date, horizon):
                if d >= today:
                    key = d.strftime("%Y-%m")
                    forecast[key] = forecast.get(key, 0) + payout
                d += relativedelta(months=months)

    # Build complete month list
    cashflow_labels = []
    cashflow_values = []
    d = today.replace(day=1)
    while d <= horizon:
        k = d.strftime("%Y-%m")
        cashflow_labels.append(k)
        cashflow_values.append(round(forecast.get(k, 0), 2))
        d += relativedelta(months=1)

    # 3. FY interest income (last 5 FYs + current)
    fy_interest_data = []
    current_fy_year = today.year if today.month >= 4 else today.year - 1
    for fy in range(current_fy_year - 4, current_fy_year + 1):
        fy_end = date(fy + 1, 3, 31)
        fy_label = f"FY{fy}-{str(fy+1)[-2:]}"
        if fy_end > today:
            fy_end = today
        total_int = sum(interest_this_fy(fi, fy_end) for fi in records)
        fy_interest_data.append({"fy": fy_label, "interest": round(total_int, 2)})

    # 4. Growth curve: total FI value vs configurable benchmark rates
    from .fi_rates import load_fi_rates
    _rates = load_fi_rates()
    SAVINGS_RATE   = _rates["savings_rate"]
    STD_FD_RATE    = _rates["std_fd_rate"]
    INFLATION_RATE = _rates["inflation_rate"]

    if records:
        from .fi_calc import value_on as fi_value_on
        curve_start = min(fi.start_date for fi in records)
        curve_days = (today - curve_start).days + 1

        # Build monthly points (daily is too heavy)
        from .fi_calc import fd_cum_value_on
        curve_labels: list[str] = []
        curve_fi:     list[float] = []
        curve_sav:    list[float] = []
        curve_std_fd: list[float] = []
        curve_inf:    list[float] = []

        def _benchmark_value(rate: float, d: date) -> float:
            return sum(
                fd_cum_value_on(fi.amount, rate, fi.start_date, fi.maturity_date, d, "quarterly")
                for fi in records if d >= fi.start_date
            )

        d = curve_start.replace(day=1)
        while d <= today:
            total_fi = sum(fi_value_on(fi, d) for fi in records)
            curve_labels.append(d.isoformat())
            curve_fi.append(round(total_fi, 2))
            curve_sav.append(round(_benchmark_value(SAVINGS_RATE, d), 2))
            curve_std_fd.append(round(_benchmark_value(STD_FD_RATE, d), 2))
            curve_inf.append(round(_benchmark_value(INFLATION_RATE, d), 2))
            d += relativedelta(months=1)

        growth_curve = {
            "labels": curve_labels,
            "fi": curve_fi,
            "savings": curve_sav,
            "std_fd": curve_std_fd,
            "inflation": curve_inf,
            "savings_rate":   SAVINGS_RATE,
            "std_fd_rate":    STD_FD_RATE,
            "inflation_rate": INFLATION_RATE,
        }
    else:
        growth_curve = {"labels": [], "fi": [], "savings": [], "inflation": [],
                        "savings_rate": 4.0, "inflation_rate": 5.0}

    return {
        "maturity_timeline": timeline,
        "cashflow_forecast": {"labels": cashflow_labels, "values": cashflow_values},
        "fy_interest": fy_interest_data,
        "growth_curve": growth_curve,
    }


# --- symbol aliases ---

@app.get("/api/symbol-aliases")
def get_symbol_aliases():
    return list_symbol_aliases()


@app.post("/api/symbol-aliases")
def create_symbol_alias(payload: dict):
    old = str(payload.get("old_symbol", "")).strip().upper()
    new = str(payload.get("new_symbol", "")).strip().upper()
    if not old or not new:
        raise HTTPException(400, "old_symbol and new_symbol required")
    add_symbol_alias(old, new)
    return {"ok": True, "old_symbol": old, "new_symbol": new}


@app.delete("/api/symbol-aliases/{old_symbol}")
def delete_symbol_alias(old_symbol: str):
    removed = remove_symbol_alias(old_symbol.upper())
    if not removed:
        raise HTTPException(404, "Alias not found or is a built-in (cannot delete)")
    return {"ok": True}


# --- static assets ---

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
