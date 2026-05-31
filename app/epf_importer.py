"""EPFO passbook PDF importer — handles the actual EPFO text extraction format.

From real passbooks, PyMuPDF extracts each cell on its own line:
  Cont. For Due-Month      ← label line (with trailing space)
  012024                   ← month on next line
  1287                     ← employee deposit (separate line)
  1287                     ← employer deposit (separate line)
  0                        ← emp withdrawal
  0                        ← employer withdrawal
  0                        ← pension

  Int. Updated upto        ← label line
  31/03/2024               ← date on next line
  31                       ← employee interest
  31                       ← employer interest
  0  0  0                  ← remaining cols

Page breaks can split entries: the month code may appear at the start of the next
page when the label is at the bottom of the current page. Member ID lines
(e.g. PYKRP00192140001148253) and "Page N of M" lines appear between pages.

PII never stored: UAN, Member ID, Name, establishment details are skipped.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import EPFEntry

log = logging.getLogger(__name__)

# ---- patterns ----
_CONT_LABEL = re.compile(r"Cont\.?\s+For\s+Due[-\s]?Month", re.I)
_INT_LABEL  = re.compile(r"Int\.?\s+Updated\s+upto", re.I)
_OB_LABEL   = re.compile(r"\bOB\b", re.I)
_MMYYYY     = re.compile(r"^(\d{2})(\d{4})$")
_DATE_DMY   = re.compile(r"^(\d{2})[/\-](\d{2})[/\-](\d{4})$")
_SINGLE_NUM = re.compile(r"^\d[\d,]*$")
_FIVE_INLINE= re.compile(r"(\d[\d,]*)\s+(\d[\d,]*)\s+(\d[\d,]*)\s+(\d[\d,]*)\s+(\d[\d,]*)")
_PAGE_ART   = re.compile(r"Page\s+\d+\s+of\s+\d+", re.I)
_MEMBER_ID  = re.compile(r"^[A-Z]{2}\w{4,}\d{6,}")  # PYKRP00192140001148253

# PII lines to skip entirely (never logged or stored)
_PII = [
    re.compile(r"\b\d{12}\b"),
    re.compile(r"Member\s+(Id|ID)\s*/\s*Name", re.I),
    re.compile(r"Establishment\s+(Id|ID)", re.I),
    re.compile(r"Date\s+of\s+Birth", re.I),
    re.compile(r"\b\d{10}\b"),           # mobile
]


def _is_pii(line: str) -> bool:
    return any(p.search(line) for p in _PII)


def _is_artifact(line: str) -> bool:
    """Page header/footer, member ID, or other non-data lines to skip."""
    return bool(_PAGE_ART.search(line) or _MEMBER_ID.match(line))


def _parse_num(s: str) -> float:
    return float(s.replace(",", ""))


def _find_month(lines: list[str], from_idx: int, max_skip: int = 8
                ) -> tuple[int | None, str | None, str | None]:
    """Scan forward for a MMYYYY line, skipping page artifacts."""
    for j in range(from_idx, min(from_idx + max_skip, len(lines))):
        line = lines[j].strip()
        m = _MMYYYY.match(line)
        if m:
            return j, m.group(1), m.group(2)
        # Keep scanning through artifacts; stop at new labels
        if _is_artifact(line) or not line:
            continue
        if _CONT_LABEL.search(line) or _INT_LABEL.search(line):
            break
    return None, None, None


def _find_date(lines: list[str], from_idx: int, max_skip: int = 8
               ) -> tuple[int | None, date | None]:
    """Scan forward for a DD/MM/YYYY date line."""
    for j in range(from_idx, min(from_idx + max_skip, len(lines))):
        line = lines[j].strip()
        m = _DATE_DMY.match(line)
        if m:
            return j, date(int(m.group(3)), int(m.group(2)), 1)
        if _is_artifact(line) or not line:
            continue
        if _CONT_LABEL.search(line) or _INT_LABEL.search(line):
            break
    return None, None


def _collect_five(lines: list[str], from_idx: int, max_scan: int = 15
                  ) -> tuple[tuple | None, int]:
    """Collect exactly 5 numbers (each on its own line or all on one line).
    Returns (tuple_of_5_floats, next_idx) or (None, from_idx).
    """
    # Try inline: all 5 on one line
    if from_idx < len(lines):
        m5 = _FIVE_INLINE.search(lines[from_idx])
        if m5:
            return (tuple(_parse_num(m5.group(i)) for i in range(1, 6)),
                    from_idx + 1)

    # Collect individual number lines, skipping artifacts
    nums: list[float] = []
    j = from_idx
    while j < len(lines) and len(nums) < 5:
        line = lines[j].strip()
        if _SINGLE_NUM.match(line):
            nums.append(_parse_num(line))
            j += 1
        elif _is_artifact(line) or not line:
            j += 1
        elif _FIVE_INLINE.search(line) and not nums:
            m5 = _FIVE_INLINE.search(line)
            return (tuple(_parse_num(m5.group(i)) for i in range(1, 6)), j + 1)
        else:
            break

    if len(nums) == 5:
        return tuple(nums), j
    return None, from_idx


# ---------- main import ----------

def import_epf_passbook(db: Session, pdf_path: str) -> dict:
    """Parse EPFO passbook PDF and insert EPFEntry rows.

    No PII (UAN, Member ID, name, establishment, mobile) is stored.
    Returns {inserted, skipped_duplicates, errors, entries_found}.
    """
    try:
        import fitz
    except ImportError:
        return {"inserted": 0, "skipped_duplicates": 0,
                "errors": ["PyMuPDF not installed — run: pip install pymupdf"],
                "entries_found": 0}

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        return {"inserted": 0, "skipped_duplicates": 0,
                "errors": [str(e)], "entries_found": 0}

    # Flatten all pages: strip, remove empty, skip PII
    all_lines: list[str] = []
    for page in doc:
        for raw in page.get_text().split("\n"):
            s = raw.strip()
            if not s or _is_pii(s):
                continue
            all_lines.append(s)
    doc.close()

    entries: list[dict] = []
    inserted = skipped = 0
    errors: list[str] = []

    i = 0
    n = len(all_lines)

    while i < n:
        line = all_lines[i]

        # ---- Contribution row ----
        if _CONT_LABEL.search(line):
            remainder = line[_CONT_LABEL.search(line).end():].strip()
            mm_m = _MMYYYY.match(remainder)

            if mm_m:
                # Month embedded in the label line itself
                mm, yyyy = mm_m.group(1), mm_m.group(2)
                nums, next_i = _collect_five(all_lines, i + 1)

            else:
                # Find the first non-artifact, non-empty line after the label
                probe = i + 1
                while probe < n and (_is_artifact(all_lines[probe]) or
                                     not all_lines[probe].strip()):
                    probe += 1

                if probe < n and _MMYYYY.match(all_lines[probe].strip()):
                    # Normal order: month then numbers
                    m = _MMYYYY.match(all_lines[probe].strip())
                    mm, yyyy = m.group(1), m.group(2)
                    nums, next_i = _collect_five(all_lines, probe + 1)

                elif probe < n and _SINGLE_NUM.match(all_lines[probe].strip()):
                    # Page-break reverse order: numbers then month (month on next page)
                    # e.g. page ends with amounts, page starts with MMYYYY
                    nums, next_i = _collect_five(all_lines, probe)
                    if nums:
                        month_idx, mm, yyyy = _find_month(all_lines, next_i, max_skip=10)
                        if month_idx is not None:
                            next_i = month_idx + 1  # consume the month line too
                    else:
                        mm = yyyy = None

                else:
                    # Fall back: scan for month anywhere nearby
                    month_idx, mm, yyyy = _find_month(all_lines, i + 1)
                    nums, next_i = (
                        _collect_five(all_lines, month_idx + 1)
                        if month_idx is not None else (None, i + 1)
                    )

            if nums and mm and yyyy:
                emp, empr, emp_wd, empr_wd, pension = nums
                entries.append({
                    "entry_type":          "contribution",
                    "month":               date(int(yyyy), int(mm), 1),
                    "employee_share":      emp,
                    "employer_share":      empr,
                    "pension_contrib":     pension,
                    "employee_withdrawal": emp_wd,
                    "employer_withdrawal": empr_wd,
                })
            i = next_i if nums else i + 1
            continue

        # ---- Interest row ----
        if _INT_LABEL.search(line) and not _OB_LABEL.search(line):
            remainder = line[_INT_LABEL.search(line).end():].strip()
            dm = _DATE_DMY.match(remainder)
            if dm:
                month = date(int(dm.group(3)), int(dm.group(2)), 1)
                nums, next_i = _collect_five(all_lines, i + 1)
            else:
                date_idx, month = _find_date(all_lines, i + 1)
                if date_idx is None:
                    i += 1
                    continue
                nums, next_i = _collect_five(all_lines, date_idx + 1)

            if nums and month:
                emp_int, empr_int = nums[0], nums[1]
                entries.append({
                    "entry_type":          "interest",
                    "month":               month,
                    "employee_share":      emp_int,
                    "employer_share":      empr_int,
                    "pension_contrib":     0.0,
                    "employee_withdrawal": 0.0,
                    "employer_withdrawal": 0.0,
                })
            i = next_i if nums else i + 1
            continue

        i += 1

    # Insert with deduplication
    for entry in entries:
        row = EPFEntry(**entry, source="passbook_import",
                       created_at=datetime.utcnow())
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
            inserted += 1
        except IntegrityError:
            skipped += 1
        except Exception as e:
            errors.append(f"{entry['month']} {entry['entry_type']}: {e}")

    db.commit()
    log.info("EPF import: %d inserted, %d skipped, %d entries parsed",
             inserted, skipped, len(entries))
    return {
        "inserted":           inserted,
        "skipped_duplicates": skipped,
        "errors":             errors,
        "entries_found":      len(entries),
    }
