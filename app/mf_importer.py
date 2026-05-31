"""CAMS + KFintech Combined CAS PDF importer.

Parses the standard Consolidated Account Statement and inserts MF transactions
into the existing transactions table (segment='MF').

PDF format (each column appears on its own line after text extraction):
  DD-Mon-YYYY       ← trade date
  29,998.50         ← amount INR  (parentheses = negative = redemption/switch-out)
  401.7730          ← NAV
  74.665            ← units       (parentheses = negative)
  Purchase-BSE -    ← description (1 or more text lines)
  74.665            ← running unit balance

Stamp duty lines (same or next date, only one numeric line follows) → skipped.
Admin-only date lines (no numeric follows, or 0.00 amount) → skipped.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import Instrument, Transaction
from .prices import get_or_create_instrument, resolve_mf_scheme_code

log = logging.getLogger(__name__)

# ---- regex patterns ----
_DATE_PAT   = re.compile(r"^\d{2}-[A-Za-z]{3}-\d{4}$")
_POS_NUM    = re.compile(r"^[\d,]+\.\d+$")
_NEG_NUM    = re.compile(r"^\([\d,]+\.\d+\)$")
_ISIN_PAT   = re.compile(r"ISIN:\s*(IN[A-Z0-9]+)")
_FOLIO_PAT  = re.compile(r"Folio\s+No[:\s]+([A-Z0-9]+)")
_REG_PAT    = re.compile(r"Registrar\s*:\s*(CAMS|KFINTECH)", re.I)

_BUY_KEYWORDS  = [
    "purchase", "systematic investment", "sys. investment",
    "switch over in", "switch in",
    "nfo", "new fund offer", "reinvestment", "bonus units", "allotment",
]
_SELL_KEYWORDS = [
    "redemption", "switch over out", "switch out",
    "systematic withdrawal", "swp",
]


def _is_num(line: str) -> bool:
    return bool(_POS_NUM.match(line) or _NEG_NUM.match(line))


def _parse_num(line: str) -> float:
    if _NEG_NUM.match(line):
        return -float(line[1:-1].replace(",", ""))
    return float(line.replace(",", ""))


def _parse_date(s: str):
    return datetime.strptime(s, "%d-%b-%Y").date()


def _classify(description: str) -> str | None:
    d = description.lower()
    for kw in _SELL_KEYWORDS:
        if kw in d:
            return "sell"
    for kw in _BUY_KEYWORDS:
        if kw in d:
            return "buy"
    return None


def _clean_scheme_name(raw: str) -> str:
    """Extract readable scheme name from CAS header line.

    Header format: '{CODE}-{Scheme Name} (Non Demat/Demat) - ISIN: ...'
    We want everything between the first '-' and '(Non Demat' or '(Demat'.
    """
    # Remove code prefix (up to first '-')
    after_code = raw.split("-", 1)[-1].strip() if "-" in raw else raw
    # Remove trailing ' (Non Demat...) - ISIN:...'
    for suffix in [" (Non Demat", " (Demat", " - ISIN:"]:
        if suffix.lower() in after_code.lower():
            idx = after_code.lower().index(suffix.lower())
            after_code = after_code[:idx]
    return after_code.strip()


def import_cas_pdf(db: Session, pdf_path: str) -> dict:
    """Parse a CAS PDF and insert MF transactions.

    Returns {inserted, skipped_duplicates, errors, schemes_found}.
    """
    try:
        import fitz
    except ImportError:
        return {"inserted": 0, "skipped_duplicates": 0, "errors": ["PyMuPDF not installed. Run: pip install pymupdf"], "schemes_found": 0}

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        return {"inserted": 0, "skipped_duplicates": 0, "errors": [str(e)], "schemes_found": 0}

    # Flatten all page text into one list of stripped, non-empty lines
    all_lines: list[str] = []
    for page in doc:
        for line in page.get_text().split("\n"):
            stripped = line.strip()
            if stripped:
                all_lines.append(stripped)

    inserted = 0
    skipped  = 0
    errors: list[str] = []
    schemes_found = 0

    # Current scheme context (updated as we walk headers)
    cur_isin: str | None = None
    cur_scheme: str | None = None
    cur_folio: str | None = None
    cur_registrar: str | None = None

    n = len(all_lines)
    i = 0

    while i < n:
        line = all_lines[i]

        # ---- scheme header detection: line contains ISIN: ----
        isin_m = _ISIN_PAT.search(line)
        if isin_m:
            cur_isin = isin_m.group(1).strip()
            cur_scheme = _clean_scheme_name(line.split(" - ISIN:")[0])
            schemes_found += 1
            i += 1
            continue

        # ---- registrar (CAMS on same line; KFINTECH sometimes on next line) ----
        reg_m = _REG_PAT.search(line)
        if reg_m:
            cur_registrar = reg_m.group(1).upper()
            i += 1
            continue
        # KFintech split across two lines: "Registrar :" then "KFINTECH"
        if line.strip().lower().startswith("registrar") and ":" in line:
            next_line = all_lines[i + 1].strip().upper() if i + 1 < n else ""
            if next_line in ("KFINTECH", "CAMS"):
                cur_registrar = next_line
                i += 2
                continue
            i += 1
            continue

        # ---- folio ----
        folio_m = _FOLIO_PAT.search(line)
        if folio_m:
            cur_folio = folio_m.group(1).strip()
            i += 1
            continue

        # ---- date line ----
        if _DATE_PAT.match(line):
            # Look-ahead: is the next line a number?
            if i + 1 >= n or not _is_num(all_lines[i + 1]):
                # date-only admin event → skip
                i += 1
                continue

            amount_line = all_lines[i + 1]
            amount = _parse_num(amount_line)

            # Is this a stamp-duty entry? → no NAV follows
            if i + 2 >= n or not _is_num(all_lines[i + 2]):
                # stamp duty / fee → skip
                i += 2
                continue

            nav_line = all_lines[i + 2]

            # Need units on i+3
            if i + 3 >= n or not _is_num(all_lines[i + 3]):
                i += 2
                continue

            units_line = all_lines[i + 3]
            nav   = _parse_num(nav_line)
            units = _parse_num(units_line)

            # Zero-amount entries (e.g. admin with 0.00) → skip
            if abs(amount) < 0.01:
                i += 4
                continue

            # Collect description lines (i+4 onwards until we hit a running-balance number
            # or a date or a scheme-footer keyword)
            desc_parts: list[str] = []
            j = i + 4
            while j < n:
                l = all_lines[j]
                if _DATE_PAT.match(l):
                    break
                if l.startswith("Closing Unit Balance") or l.startswith("NAV on") or l.startswith("Opening Unit Balance"):
                    break
                if _is_num(l):
                    # This is the running balance — consume and stop
                    j += 1
                    break
                desc_parts.append(l)
                j += 1

            description = " ".join(desc_parts).strip()
            txn_type = _classify(description)

            if txn_type is None:
                # unrecognised transaction type — log and skip
                log.debug("Unclassified CAS txn: %s", description[:80])
                i = j
                continue

            if not cur_isin or not cur_folio:
                errors.append(f"Line {i}: transaction without scheme context — {line}")
                i = j
                continue

            trade_date = _parse_date(line)
            trade_id = f"CAS|{cur_folio}|{trade_date.isoformat()}|{nav:.4f}|{abs(units):.3f}"

            # Use ISIN as the symbol for MF transactions (unique per scheme variant).
            # The full scheme name is stored in instruments.name for display.
            symbol = cur_isin

            txn = Transaction(
                trade_date=trade_date,
                symbol=symbol,
                isin=cur_isin,
                exchange=cur_registrar,
                segment="MF",
                trade_type=txn_type,
                quantity=abs(units),
                price=nav,
                fees=0.0,
                notes=description[:200] if description else None,
                source="cas_import",
                trade_id=trade_id,
                folio=cur_folio,
            )

            try:
                with db.begin_nested():
                    db.add(txn)
                    db.flush()
                    # Lazily resolve instrument so AMFI scheme code is set
                    _ensure_instrument(db, cur_isin, cur_scheme, cur_registrar)
                inserted += 1
            except IntegrityError:
                skipped += 1

            i = j
            continue

        i += 1

    db.commit()
    log.info("CAS import: %d inserted, %d skipped, %d schemes", inserted, skipped, schemes_found)
    return {
        "inserted": inserted,
        "skipped_duplicates": skipped,
        "errors": errors,
        "schemes_found": schemes_found,
    }


def _ensure_instrument(db: Session, isin: str, scheme_name: str | None, registrar: str | None) -> None:
    """Create or update an Instrument record for an MF ISIN."""
    from datetime import datetime as dt
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from .db import Instrument

    amfi_code = resolve_mf_scheme_code(isin)
    stmt = (
        sqlite_insert(Instrument)
        .values(
            isin=isin,
            symbol=isin,          # ISIN used as symbol; name holds human-readable name
            name=scheme_name,
            segment="MF",
            amfi_scheme_code=amfi_code,
            last_resolved=dt.utcnow(),
        )
        .on_conflict_do_update(
            index_elements=["isin"],
            set_={
                "name": scheme_name,
                "amfi_scheme_code": amfi_code,
                "last_resolved": dt.utcnow(),
            },
        )
    )
    db.execute(stmt)
