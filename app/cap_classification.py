"""Large/Mid/Small-cap classification from NSE's official index constituent lists.

SEBI/AMFI methodology ranks all listed companies by market cap:
  1-100   = Large Cap   (NIFTY 100)
  101-250 = Mid Cap     (NIFTY Midcap 150)
  251-500 = Small Cap   (NIFTY Smallcap 250)

We source NSE's published constituent CSVs (keyed by NSE Symbol + ISIN), so a
holding's category is fixed by its *rank* — not by a live, fluctuating market-cap
number that flips with the share price. The parsed map is cached in memory (TTL)
and persisted to data/cap_classification.json, so a fetch failure / offline run
falls back to the last good copy. Only the explicit "Refresh market data" action
hits the network (force=True); normal requests read memory/disk.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import requests

log = logging.getLogger(__name__)

_CACHE_FILE = Path(__file__).resolve().parent.parent / "data" / "cap_classification.json"
_TTL = timedelta(days=7)   # NSE updates semi-annually; the disk copy is the real store
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")  # niftyindices 403s without a UA

# index constituent CSV -> category
_SOURCES = {
    "large": "https://niftyindices.com/IndexConstituent/ind_nifty100list.csv",
    "mid":   "https://niftyindices.com/IndexConstituent/ind_niftymidcap150list.csv",
    "small": "https://niftyindices.com/IndexConstituent/ind_niftysmallcap250list.csv",
}

# in-memory cache
_by_isin: dict[str, str] = {}
_by_symbol: dict[str, str] = {}
_loaded_at: datetime | None = None


def _parse_csv(text: str, category: str, by_isin: dict, by_symbol: dict) -> None:
    """Parse one constituent CSV (cols: Company Name, Industry, Symbol, Series, ISIN Code)."""
    for row in csv.DictReader(io.StringIO(text)):
        sym = (row.get("Symbol") or "").strip().upper()
        isin = (row.get("ISIN Code") or row.get("ISIN") or "").strip().upper()
        if sym:
            by_symbol[sym] = category
        if isin:
            by_isin[isin] = category


def _fetch_lists() -> tuple[dict, dict] | None:
    """Fetch + parse all three NSE constituent CSVs. Returns (by_isin, by_symbol),
    or None if every fetch failed."""
    by_isin: dict[str, str] = {}
    by_symbol: dict[str, str] = {}
    ok = False
    for category, url in _SOURCES.items():
        try:
            r = requests.get(url, headers={"User-Agent": _UA}, timeout=30)
            r.raise_for_status()
            _parse_csv(r.text, category, by_isin, by_symbol)
            ok = True
        except Exception as e:
            log.warning("cap list fetch failed (%s): %s", category, e)
    return (by_isin, by_symbol) if ok else None


def _save_disk() -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps({
            "by_isin": _by_isin, "by_symbol": _by_symbol,
            "saved_at": datetime.utcnow().isoformat(),
        }))
    except Exception as e:
        log.warning("cap classification disk save failed: %s", e)


def _load_disk() -> bool:
    global _by_isin, _by_symbol
    if not _CACHE_FILE.exists():
        return False
    try:
        data = json.loads(_CACHE_FILE.read_text())
        _by_isin = data.get("by_isin", {}) or {}
        _by_symbol = data.get("by_symbol", {}) or {}
        return bool(_by_isin or _by_symbol)
    except Exception:
        return False


def _load_cap_index(force: bool = False) -> int:
    """Ensure the in-memory maps are populated. Only force=True hits the network
    (used by the Refresh-market-data action); otherwise loads from disk if memory
    is empty. Returns the number of symbols known."""
    global _by_isin, _by_symbol, _loaded_at
    if force:
        result = _fetch_lists()
        if result:
            _by_isin, _by_symbol = result
            _loaded_at = datetime.utcnow()
            _save_disk()
        elif not (_by_isin or _by_symbol):
            _load_disk()   # network failed and memory empty → last-good disk copy
    elif not (_by_isin or _by_symbol):
        _load_disk()
    return len(_by_symbol)


def lists_loaded() -> bool:
    """True if the official NSE constituent lists are available (memory or disk).
    Lets callers distinguish 'not in the top 500' (→ small by SEBI definition) from
    'lists unavailable' (→ fall back to a market-cap guess). No network fetch."""
    _load_cap_index()
    return bool(_by_isin or _by_symbol)


def get_cap_category(isin: str | None, symbol: str | None) -> str | None:
    """'large' | 'mid' | 'small', or None if not in the top-500 lists (caller may
    fall back). Matches by ISIN first (stable across ticker renames), then symbol.
    Never triggers a network fetch — call _load_cap_index(force=True) to refresh."""
    _load_cap_index()
    if isin:
        c = _by_isin.get(isin.strip().upper())
        if c:
            return c
    if symbol:
        c = _by_symbol.get(symbol.strip().upper())
        if c:
            return c
    return None
