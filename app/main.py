from __future__ import annotations

import csv
import io
from datetime import date, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .analytics import (
    compute_equity_curve,
    compute_holdings,
    compute_period_xirr,
    compute_realized_pnl_by_period,
    compute_summary,
    find_orphan_sells,
    portfolio_cashflows,
)
from .corporate_actions import auto_fetch_all
from .db import CorporateAction, Transaction, get_session, init_db
from .importer import import_tradebook
from .mf_importer import import_cas_pdf
from .prices import BENCHMARKS, add_symbol_alias, list_symbol_aliases, remove_symbol_alias
from .xirr import xirr

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Portfolio Tracker", version="0.1.0")


@app.on_event("startup")
def _startup() -> None:
    init_db()


# --- root ---

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


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
        os.unlink(tmp_path)
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
    return compute_summary(db, segment=segment or None)


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
    """Force a recompute by recomputing summary (which lazily refetches today)."""
    s = compute_summary(db)
    return {"ok": True, "as_of": s["as_of"], "current_value": s["current_value"]}


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
    return compute_period_xirr(db, from_date, to_date, segment=segment or None)


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
    return compute_realized_pnl_by_period(db, from_date, to_date, segment=segment or None)


@app.get("/api/data-quality")
def data_quality(db: Session = Depends(get_session)):
    """Returns orphan sells (missing buy transactions) that are silently ignored.

    Common causes: IPO allotments, demerger receipts, off-market transfers.
    For each entry, add a manual buy transaction at the IPO/allotment price to fix.
    """
    orphans = find_orphan_sells(db)
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


@app.get("/api/debug/cashflows")
def debug_cashflows(db: Session = Depends(get_session)):
    """Inspect the exact cashflow list driving portfolio XIRR.

    Useful to sanity-check the XIRR result. Final inflow is today's portfolio
    value; sum across all flows shows aggregate net P&L.
    """
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
