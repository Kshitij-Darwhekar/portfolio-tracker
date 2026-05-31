"""Zerodha tradebook importer.

Expected columns:
  symbol, isin, trade_date, exchange, segment, series, trade_type,
  auction, quantity, price, trade_id, order_id, order_execution_time

`trade_id` is the dedupe key — re-importing the same file is a no-op.
"""

from __future__ import annotations

import io
from datetime import datetime
from typing import IO

import pandas as pd
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import Transaction

REQUIRED_COLS = {
    "symbol",
    "trade_date",
    "segment",
    "trade_type",
    "quantity",
    "price",
}


def _read_dataframe(filename: str, content: bytes) -> pd.DataFrame:
    name = filename.lower()
    if name.endswith(".xlsx") or name.endswith(".xls"):
        return pd.read_excel(io.BytesIO(content))
    # auto-detect separator (comma / tab / semicolon / pipe) and skip BOM
    return pd.read_csv(io.BytesIO(content), sep=None, engine="python", encoding="utf-8-sig")


def _parse_date(v) -> datetime.date | None:
    if v is None or pd.isna(v):
        return None
    if isinstance(v, datetime):
        return v.date()
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return pd.to_datetime(s).date()
    except Exception:
        return None


def _opt_str(v) -> str | None:
    """Coerce a pandas cell to an optional string, treating NaN/empty as None."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    return s


def import_tradebook(db: Session, filename: str, content: bytes) -> dict:
    df = _read_dataframe(filename, content)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    missing = REQUIRED_COLS - set(df.columns)
    if missing:
        return {
            "inserted": 0,
            "skipped_duplicates": 0,
            "errors": [
                f"Missing required columns: {sorted(missing)}",
                f"Columns found in file: {list(df.columns)}",
            ],
        }

    inserted = 0
    skipped = 0
    errors: list[str] = []

    for idx, row in df.iterrows():
        try:
            trade_date = _parse_date(row.get("trade_date"))
            if not trade_date:
                errors.append(f"Row {idx}: invalid trade_date")
                continue

            segment = str(row.get("segment", "")).strip().upper() or "EQ"
            if segment not in ("EQ", "MF"):
                # Zerodha sometimes uses 'EQUITY' / 'MUTUALFUND'
                if segment.startswith("EQU"):
                    segment = "EQ"
                elif segment.startswith("MUT"):
                    segment = "MF"
                else:
                    segment = "EQ"

            trade_type = str(row.get("trade_type", "")).strip().lower()
            if trade_type not in ("buy", "sell"):
                errors.append(f"Row {idx}: invalid trade_type '{trade_type}'")
                continue

            qty = float(row.get("quantity") or 0)
            price = float(row.get("price") or 0)
            if qty <= 0 or price < 0:
                errors.append(f"Row {idx}: invalid qty/price")
                continue

            symbol = _opt_str(row.get("symbol"))
            if not symbol:
                errors.append(f"Row {idx}: missing symbol")
                continue

            fees_raw = row.get("fees")
            try:
                fees = float(fees_raw) if fees_raw is not None and not pd.isna(fees_raw) else 0.0
            except (TypeError, ValueError):
                fees = 0.0

            txn = Transaction(
                trade_date=trade_date,
                symbol=symbol.upper(),
                isin=_opt_str(row.get("isin")),
                exchange=_opt_str(row.get("exchange")),
                segment=segment,
                trade_type=trade_type,
                quantity=qty,
                price=price,
                fees=fees,
                notes=None,
                source="import",
                trade_id=_opt_str(row.get("trade_id")),
            )

            try:
                with db.begin_nested():  # SAVEPOINT — isolates this row's INSERT
                    db.add(txn)
                    db.flush()
                inserted += 1
            except IntegrityError:
                skipped += 1
        except Exception as e:
            errors.append(f"Row {idx}: {e}")

    db.commit()
    return {"inserted": inserted, "skipped_duplicates": skipped, "errors": errors}
