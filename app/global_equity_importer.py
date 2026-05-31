"""INDMoney Global Equity XLS importer (Alpaca order book format).

Expected file format (INDMoney 'Global Equity Full Report.xls'):
  Row 0-8  : account details header (skipped)
  Row 9    : blank
  Row 10   : column headers
  Row 11+  : transaction data

Columns:
  Stock Name | Stock Symbol | Order Placed Time | Order Execution Time |
  Broker Reference Id | Transaction Type | Order Type |
  Quantity | Price ($) | Order Amount ($) | Brokerage ($)

Amounts are in USD. Exchange rates (USD/INR) are fetched from yfinance
for each unique trade date and stored with each transaction for accurate
INR P&L computation.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import GlobalEquityTransaction

log = logging.getLogger(__name__)

_DATE_FMTS = [
    "%d %b %Y, %I:%M %p",
    "%d %b %Y, %I:%M:%S %p",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
]


def _parse_dt(val) -> datetime | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, datetime):
        return val
    s = str(val).strip()
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _fetch_usdinr(dates: set) -> dict:
    """Fetch historical USD/INR close for a set of dates. Returns {date: rate}."""
    if not dates:
        return {}
    try:
        import yfinance as yf
        from datetime import timedelta
        min_d = min(dates) - timedelta(days=5)
        max_d = max(dates) + timedelta(days=2)
        df = yf.Ticker("USDINR=X").history(
            start=min_d.isoformat(), end=max_d.isoformat(), auto_adjust=True
        )
        if df is None or df.empty:
            return {}
        rates = {}
        for d in dates:
            # Find closest available rate
            avail = [ts.date() for ts in df.index if ts.date() <= d]
            if avail:
                ts = max(avail)
                rates[d] = float(df.loc[df.index.date == ts, "Close"].iloc[-1])
        return rates
    except Exception as e:
        log.warning("Could not fetch USD/INR rates: %s", e)
        return {}


def import_indmoney_global(db: Session, file_path: str) -> dict:
    """Parse INDMoney Global Equity XLS and insert transactions.

    Returns {inserted, skipped_duplicates, errors, total_found}.
    Broker Reference IDs and account numbers are NOT stored.
    """
    path = Path(file_path)
    suffix = path.suffix.lower()

    try:
        if suffix in (".xls", ".xlsx"):
            # Find the header row (contains 'Stock Symbol')
            raw = pd.read_excel(file_path, sheet_name=0, header=None)
            header_row = None
            for idx, row in raw.iterrows():
                if any("Stock Symbol" in str(v) for v in row.values):
                    header_row = idx
                    break
            if header_row is None:
                return {"inserted": 0, "skipped_duplicates": 0,
                        "errors": ["Could not find header row with 'Stock Symbol'"],
                        "total_found": 0}
            df = pd.read_excel(file_path, sheet_name=0, header=header_row)
        else:
            df = pd.read_csv(file_path)
    except Exception as e:
        return {"inserted": 0, "skipped_duplicates": 0,
                "errors": [str(e)], "total_found": 0}

    # Normalise column names
    df.columns = [str(c).strip() for c in df.columns]
    df = df.dropna(how="all")

    # Map column names flexibly
    COL_MAP = {
        "Stock Name":           ["Stock Name", "stock_name", "Name"],
        "Stock Symbol":         ["Stock Symbol", "Symbol", "Ticker"],
        "Order Execution Time": ["Order Execution Time", "Execution Time", "Trade Date"],
        "Transaction Type":     ["Transaction Type", "Type", "Side"],
        "Quantity":             ["Quantity", "Qty", "Shares"],
        "Price ($)":            ["Price ($)", "Price", "Price (USD)"],
        "Order Amount ($)":     ["Order Amount ($)", "Amount", "Amount ($)", "Order Amount"],
        "Brokerage ($)":        ["Brokerage ($)", "Brokerage", "Fee", "Commission"],
        "Broker Reference Id":  ["Broker Reference Id", "Reference Id", "Order Id"],
    }

    def find_col(choices):
        for c in choices:
            if c in df.columns:
                return c
        return None

    col_name    = find_col(COL_MAP["Stock Name"])
    col_sym     = find_col(COL_MAP["Stock Symbol"])
    col_exec    = find_col(COL_MAP["Order Execution Time"])
    col_type    = find_col(COL_MAP["Transaction Type"])
    col_qty     = find_col(COL_MAP["Quantity"])
    col_price   = find_col(COL_MAP["Price ($)"])
    col_amount  = find_col(COL_MAP["Order Amount ($)"])
    col_fee     = find_col(COL_MAP["Brokerage ($)"])
    col_ref     = find_col(COL_MAP["Broker Reference Id"])

    if not col_sym or not col_type or not col_qty:
        return {"inserted": 0, "skipped_duplicates": 0,
                "errors": [f"Missing required columns. Found: {list(df.columns)}"],
                "total_found": 0}

    # Collect unique trade dates for USD/INR batch fetch
    trade_dates = set()
    for _, row in df.iterrows():
        dt = _parse_dt(row.get(col_exec))
        if dt:
            trade_dates.add(dt.date())

    usdinr_rates = _fetch_usdinr(trade_dates)
    log.info("Fetched USD/INR for %d dates", len(usdinr_rates))

    inserted = skipped = 0
    errors: list[str] = []
    rows_found = 0

    for idx, row in df.iterrows():
        sym = str(row.get(col_sym, "")).strip().upper()
        if not sym or sym == "NAN" or sym == "STOCK SYMBOL":
            continue

        tt = str(row.get(col_type, "")).strip().upper()
        if tt not in ("BUY", "SELL"):
            continue

        rows_found += 1
        dt = _parse_dt(row.get(col_exec))
        if not dt:
            errors.append(f"Row {idx}: could not parse date '{row.get(col_exec)}'")
            continue

        try:
            qty     = float(row.get(col_qty, 0) or 0)
            price   = float(row.get(col_price, 0) or 0)
            amount  = float(row.get(col_amount, 0) or 0)
            fees    = float(row.get(col_fee, 0) or 0)
        except (TypeError, ValueError) as e:
            errors.append(f"Row {idx}: numeric parse error: {e}")
            continue

        if qty <= 0 or price <= 0:
            continue

        broker_ref = str(row.get(col_ref, "") or "").strip() or None
        # Only use broker_ref as dedup key if it looks like a UUID (don't store account IDs)
        if broker_ref and len(broker_ref) < 10:
            broker_ref = None

        trade_date = dt.date()
        rate = usdinr_rates.get(trade_date)

        txn = GlobalEquityTransaction(
            stock_name     = str(row.get(col_name, sym) or sym).strip()[:200],
            symbol         = sym,
            trade_date     = trade_date,
            execution_time = dt,
            trade_type     = tt.lower(),
            quantity       = qty,
            price_usd      = price,
            amount_usd     = amount if amount > 0 else qty * price,
            fees_usd       = fees,
            exchange_rate  = rate,
            broker_ref     = broker_ref,
            source         = "indmoney_import",
        )

        try:
            with db.begin_nested():
                db.add(txn)
                db.flush()
            inserted += 1
        except IntegrityError:
            skipped += 1
        except Exception as e:
            errors.append(f"Row {idx} {sym}: {e}")

    db.commit()
    return {
        "inserted":           inserted,
        "skipped_duplicates": skipped,
        "errors":             errors,
        "total_found":        rows_found,
    }
