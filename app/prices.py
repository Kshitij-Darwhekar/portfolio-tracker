"""Price fetching for stocks (yfinance) and mutual funds (AMFI + mfapi.in).

Single entry point: get_close_series(instrument, start, end) returns a dict
{date: close}. Daily closes are cached in price_cache to avoid hammering APIs.

Convention:
- For equities, cache key = yf_ticker (e.g. 'RELIANCE.NS').
- For MFs, cache key = ISIN (we resolve to AMFI scheme code internally).
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import requests
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import Instrument, PriceCache

log = logging.getLogger(__name__)

# Path for user-defined symbol aliases (persists across restarts)
_ALIASES_FILE = Path(__file__).resolve().parent.parent / "data" / "symbol_aliases.json"


def _load_aliases() -> dict[str, str]:
    """Load {old_symbol: new_symbol} from the aliases file."""
    if _ALIASES_FILE.exists():
        try:
            return json.loads(_ALIASES_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_aliases(aliases: dict[str, str]) -> None:
    _ALIASES_FILE.write_text(json.dumps(aliases, indent=2))

AMFI_NAV_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
MFAPI_URL = "https://api.mfapi.in/mf/{scheme_code}"

# in-process cache so we don't hit yfinance/AMFI repeatedly within a request burst
_amfi_isin_to_scheme: dict[str, str] = {}
_amfi_loaded_at: datetime | None = None


# ---------- AMFI / mutual fund helpers ----------

def _load_amfi_index(force: bool = False) -> dict[str, str]:
    """Build {ISIN -> scheme_code} from AMFI's daily NAV file. Cached for 12h."""
    global _amfi_loaded_at, _amfi_isin_to_scheme
    if (
        not force
        and _amfi_loaded_at
        and datetime.utcnow() - _amfi_loaded_at < timedelta(hours=12)
        and _amfi_isin_to_scheme
    ):
        return _amfi_isin_to_scheme

    try:
        r = requests.get(AMFI_NAV_URL, timeout=30)
        r.raise_for_status()
    except Exception as e:
        log.warning("AMFI fetch failed: %s", e)
        return _amfi_isin_to_scheme  # return whatever we had

    mapping: dict[str, str] = {}
    for line in r.text.splitlines():
        parts = line.split(";")
        if len(parts) < 6:
            continue
        scheme_code, isin_growth, isin_div, _name, _nav, _date = parts[:6]
        scheme_code = scheme_code.strip()
        if not scheme_code.isdigit():
            continue
        for isin in (isin_growth.strip(), isin_div.strip()):
            if isin and isin != "-":
                mapping[isin] = scheme_code
    _amfi_isin_to_scheme = mapping
    _amfi_loaded_at = datetime.utcnow()
    return mapping


def resolve_mf_scheme_code(isin: str) -> str | None:
    return _load_amfi_index().get(isin)


def fetch_mf_history(scheme_code: str) -> dict[date, float]:
    """Full NAV history for a scheme from mfapi.in."""
    try:
        r = requests.get(MFAPI_URL.format(scheme_code=scheme_code), timeout=30)
        r.raise_for_status()
        data = r.json().get("data", [])
    except Exception as e:
        log.warning("mfapi.in fetch failed for %s: %s", scheme_code, e)
        return {}

    out: dict[date, float] = {}
    for row in data:
        try:
            d = datetime.strptime(row["date"], "%d-%m-%Y").date()
            out[d] = float(row["nav"])
        except Exception:
            continue
    return out


# ---------- yfinance / equity helpers ----------

# Known NSE symbol renames. Merged at startup with user-defined aliases from
# data/symbol_aliases.json. Values are the CURRENT NSE symbol (no .NS suffix).
_BUILTIN_REMAP: dict[str, str] = {
    "ZOMATO": "ETERNAL",       # Zomato Limited → Eternal Limited (Mar 2025)
    "GOLDETFADD": "GOLDADD",   # DSP Gold ETF renamed to GOLDADD
}

# Runtime map — built-ins + user aliases; call reload_symbol_remap() to refresh
SYMBOL_REMAP: dict[str, str] = {**_BUILTIN_REMAP, **_load_aliases()}


def reload_symbol_remap() -> None:
    """Merge built-ins with the latest aliases file into SYMBOL_REMAP."""
    SYMBOL_REMAP.clear()
    SYMBOL_REMAP.update(_BUILTIN_REMAP)
    SYMBOL_REMAP.update(_load_aliases())


def add_symbol_alias(old_symbol: str, new_symbol: str) -> None:
    aliases = _load_aliases()
    aliases[old_symbol.upper()] = new_symbol.upper()
    _save_aliases(aliases)
    reload_symbol_remap()


def remove_symbol_alias(old_symbol: str) -> bool:
    aliases = _load_aliases()
    if old_symbol.upper() in aliases:
        del aliases[old_symbol.upper()]
        _save_aliases(aliases)
        reload_symbol_remap()
        return True
    return False


def list_symbol_aliases() -> list[dict]:
    """All aliases: built-ins (read-only) + user-defined (editable)."""
    user = _load_aliases()
    out = []
    for old, new in _BUILTIN_REMAP.items():
        out.append({"old_symbol": old, "new_symbol": new, "source": "builtin"})
    for old, new in user.items():
        if old not in _BUILTIN_REMAP:
            out.append({"old_symbol": old, "new_symbol": new, "source": "user"})
    return out


def fetch_equity_history(yf_ticker: str, start: date, end: date) -> dict[date, float]:
    import yfinance as yf  # imported lazily to keep startup fast

    try:
        df = yf.Ticker(yf_ticker).history(
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            auto_adjust=True,
        )
    except Exception as e:
        log.warning("yfinance fetch failed for %s: %s", yf_ticker, e)
        return {}
    if df is None or df.empty:
        return {}
    out: dict[date, float] = {}
    for ts, row in df.iterrows():
        d = ts.date() if hasattr(ts, "date") else ts
        try:
            out[d] = float(row["Close"])
        except Exception:
            continue
    return out


# SEBI approximate market cap thresholds (INR, 2024-25).
# NSE defines large cap as the top 100 companies by full market cap (~>₹40,000 Cr),
# mid cap as 101-250 (~₹8,000-40,000 Cr), small cap as 251+ (~<₹8,000 Cr).
# These are approximate; NSE refreshes the official list every six months.
_LARGE_CAP_MIN_INR = 400_000_000_000   # ₹40,000 Cr
_MID_CAP_MIN_INR   =  80_000_000_000   # ₹8,000 Cr

# Static overrides for instruments yfinance cannot classify: ETFs, REITs, InvITs, SGBs.
# Keys are NSE symbols (upper-case). Used by both fetch_instrument_meta (persists to DB)
# and compute_allocation_breakdown (runtime fallback so X-Ray works before Refresh).
INSTRUMENT_META_OVERRIDES: dict[str, dict] = {
    # Gold ETFs / ETF Add-ons
    "GOLDBEES":    {"sector": "Gold",              "market_cap_category": None},
    "GOLDETFADD":  {"sector": "Gold",              "market_cap_category": None},
    "GOLDADD":     {"sector": "Gold",              "market_cap_category": None},
    "GOLDIETF":    {"sector": "Gold",              "market_cap_category": None},
    "AXISGOLD":    {"sector": "Gold",              "market_cap_category": None},
    "HDFCMFGETF":  {"sector": "Gold",              "market_cap_category": None},
    # Silver ETFs
    "SILVERBEES":  {"sector": "Silver",            "market_cap_category": None},
    "SILVRETF":    {"sector": "Silver",            "market_cap_category": None},
    # Liquid / Debt ETFs
    "LIQUIDCASE":  {"sector": "Liquid / Debt",     "market_cap_category": None},
    "LIQUIDBEES":  {"sector": "Liquid / Debt",     "market_cap_category": None},
    "LIQUIDIETF":  {"sector": "Liquid / Debt",     "market_cap_category": None},
    "ICICIB22":    {"sector": "Liquid / Debt",     "market_cap_category": None},
    "LICNETFGSEC": {"sector": "Liquid / Debt",     "market_cap_category": None},
    # Broad index ETFs
    "NIFTYBEES":   {"sector": "Index ETF",         "market_cap_category": None},
    "JUNIORBEES":  {"sector": "Index ETF",         "market_cap_category": None},
    "SETFNIF50":   {"sector": "Index ETF",         "market_cap_category": None},
    "ICICINIFTY":  {"sector": "Index ETF",         "market_cap_category": None},
    "MOM100":      {"sector": "Index ETF",         "market_cap_category": None},
    # Sector ETFs
    "BANKBEES":    {"sector": "Financial Services", "market_cap_category": None},
    "ITBEES":      {"sector": "Technology",         "market_cap_category": None},
    # REITs
    "EMBASSY":     {"sector": "Real Estate / REITs", "market_cap_category": "large"},
    "MINDSPACE":   {"sector": "Real Estate / REITs", "market_cap_category": "mid"},
    "BROOKFIELD":  {"sector": "Real Estate / REITs", "market_cap_category": "mid"},
    "NXTRA":       {"sector": "Real Estate / REITs", "market_cap_category": "mid"},
    # InvITs
    "POWERGRID":   {},  # regular stock — let yfinance handle
    "NEXUSINVIT":  {"sector": "Infrastructure InvIT", "market_cap_category": "mid"},
    "INDIGRID":    {"sector": "Infrastructure InvIT", "market_cap_category": "mid"},
    # SGBs
    "SGBDE31III":  {"sector": "Gold / SGB",        "market_cap_category": None},
    "SGBDEC31III": {"sector": "Gold / SGB",        "market_cap_category": None},
}


def fetch_instrument_meta(db: Session, inst: "Instrument") -> dict:
    """Fetch sector and market_cap_category for an EQ instrument.

    Checks the static override map first (ETFs, REITs, SGBs that yfinance cannot
    classify). Falls back to yfinance .info for regular equities.
    Writes the result to the instrument row and commits.
    """
    import yfinance as yf

    sym = inst.symbol.upper()

    # Static overrides take priority — yfinance returns None for ETFs/REITs/SGBs
    override = INSTRUMENT_META_OVERRIDES.get(sym, {})
    if override:  # non-empty dict means we have a definitive answer
        cap_cat = override.get("market_cap_category")
        sector  = override.get("sector")
        inst.market_cap_category = cap_cat
        inst.sector = sector
        db.commit()
        return {"sector": sector, "market_cap_category": cap_cat}

    # Cap category: prefer the official NSE/AMFI rank-based list (stable; doesn't
    # flip with the share price). yfinance market cap is only a fallback for the
    # rare holding outside the top-500 lists.
    from .cap_classification import get_cap_category
    cap_cat = get_cap_category(inst.isin, sym)

    ticker_str = inst.yf_ticker or resolve_yf_ticker(sym, "NSE")
    try:
        info = yf.Ticker(ticker_str).info
        sector = info.get("sector") or None
        if cap_cat is None:
            market_cap = info.get("marketCap") or 0
            if market_cap >= _LARGE_CAP_MIN_INR:
                cap_cat = "large"
            elif market_cap >= _MID_CAP_MIN_INR:
                cap_cat = "mid"
            elif market_cap > 0:
                cap_cat = "small"

        inst.market_cap_category = cap_cat
        inst.sector = sector
        db.commit()
        return {"sector": sector, "market_cap_category": cap_cat}
    except Exception as exc:
        log.warning("fetch_instrument_meta failed for %s: %s", ticker_str, exc)
        # yfinance failed, but we may still have a list-based cap — persist it.
        if cap_cat is not None:
            inst.market_cap_category = cap_cat
            db.commit()
            return {"sector": inst.sector, "market_cap_category": cap_cat}
        return {"sector": None, "market_cap_category": None}


def resolve_yf_ticker(symbol: str, exchange: str | None) -> str:
    """Map (symbol, exchange) → yfinance ticker."""
    sym = symbol.strip().upper()
    sym = SYMBOL_REMAP.get(sym, sym)
    if "." in sym:
        return sym
    if exchange and exchange.upper() == "BSE":
        return f"{sym}.BO"
    return f"{sym}.NS"


# ---------- instrument resolution ----------

def get_or_create_instrument(
    db: Session, *, symbol: str, isin: str | None, segment: str, exchange: str | None
) -> Instrument | None:
    """Idempotent under concurrent inserts (uses SQLite UPSERT)."""
    if not isin:
        return None

    inst = db.get(Instrument, isin)
    if inst is not None:
        # backfill missing resolution on existing row
        if segment == "EQ" and not inst.yf_ticker:
            inst.yf_ticker = resolve_yf_ticker(symbol, exchange)
        if segment == "MF" and not inst.amfi_scheme_code:
            inst.amfi_scheme_code = resolve_mf_scheme_code(isin)
        if not inst.last_resolved:
            inst.last_resolved = datetime.utcnow()
        return inst

    # Doesn't exist — INSERT OR IGNORE to dodge UNIQUE conflicts from concurrent
    # requests, then fetch the canonical row.
    values = {
        "isin": isin,
        "symbol": symbol,
        "segment": segment,
        "yf_ticker": resolve_yf_ticker(symbol, exchange) if segment == "EQ" else None,
        "amfi_scheme_code": resolve_mf_scheme_code(isin) if segment == "MF" else None,
        "last_resolved": datetime.utcnow(),
    }
    stmt = sqlite_insert(Instrument).values(**values).on_conflict_do_nothing(
        index_elements=["isin"]
    )
    try:
        db.execute(stmt)
        db.flush()
    except IntegrityError:
        # Other thread won the race — roll back this savepoint and re-fetch
        db.rollback()
    return db.get(Instrument, isin)


def _cache_key(inst: Instrument) -> str:
    if inst.segment == "EQ":
        return inst.yf_ticker or inst.symbol
    return inst.isin


def _cache_get(db: Session, key: str, dates: Iterable[date]) -> dict[date, float]:
    rows = db.execute(
        select(PriceCache.on_date, PriceCache.close).where(
            PriceCache.key == key, PriceCache.on_date.in_(list(dates))
        )
    ).all()
    return {r.on_date: r.close for r in rows}


def _cache_put(db: Session, key: str, series: dict[date, float]) -> None:
    if not series:
        return
    rows = [
        {"key": key, "on_date": d, "close": float(v), "fetched_at": datetime.utcnow()}
        for d, v in series.items()
    ]
    stmt = sqlite_insert(PriceCache).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["key", "on_date"],
        set_={"close": stmt.excluded.close, "fetched_at": stmt.excluded.fetched_at},
    )
    db.execute(stmt)


def get_close_series(
    db: Session, inst: Instrument, start: date, end: date, cache_only: bool = False
) -> dict[date, float]:
    """Return {date: close} between [start, end] inclusive, using cache when possible.

    cache_only=True: return only what is already in the DB; never call external APIs.
    This is used by analytics functions so they don't block on network calls.
    Live fetches only happen during explicit "Refresh prices" requests.
    """
    key = _cache_key(inst)
    if not key:
        return {}

    # Probe cache for end-date — if we have it AND start, assume the range is filled
    have_start = bool(_cache_get(db, key, [start]))
    have_end = bool(_cache_get(db, key, [end]))

    if have_start and have_end:
        # Pull the whole range from cache
        rows = db.execute(
            select(PriceCache.on_date, PriceCache.close).where(
                PriceCache.key == key,
                PriceCache.on_date >= start,
                PriceCache.on_date <= end,
            )
        ).all()
        return {r.on_date: r.close for r in rows}

    if cache_only:
        # Return whatever partial data we have — no API calls
        rows = db.execute(
            select(PriceCache.on_date, PriceCache.close).where(
                PriceCache.key == key,
                PriceCache.on_date >= start,
                PriceCache.on_date <= end,
            )
        ).all()
        return {r.on_date: r.close for r in rows}

    # Cache miss — fetch from source
    if inst.segment == "EQ":
        series = fetch_equity_history(key, start, end)

        # If empty and the underlying symbol has a known rename, try the new ticker
        if not series:
            base = (inst.symbol or "").upper()
            if base in SYMBOL_REMAP:
                remapped = f"{SYMBOL_REMAP[base]}.NS"
                log.info("No data for %s, trying remapped ticker %s", key, remapped)
                series = fetch_equity_history(remapped, start, end)
                if series:
                    inst.yf_ticker = remapped
                    key = remapped
                    db.flush()

        # Fallback to BSE if NSE still returned nothing
        if not series and key.endswith(".NS"):
            bo_ticker = key[:-3] + ".BO"
            log.info("No NSE data for %s, trying %s", key, bo_ticker)
            series = fetch_equity_history(bo_ticker, start, end)
            if series:
                inst.yf_ticker = bo_ticker
                key = bo_ticker
                db.flush()

        # Final fallback: ETFs are also MFs — try AMFI NAV using the ISIN
        if not series and inst.isin:
            log.info("yfinance failed for %s, trying AMFI NAV via ISIN %s", key, inst.isin)
            if not inst.amfi_scheme_code:
                inst.amfi_scheme_code = resolve_mf_scheme_code(inst.isin)
                db.flush()
            if inst.amfi_scheme_code:
                full = fetch_mf_history(inst.amfi_scheme_code)
                series = {d: v for d, v in full.items() if start <= d <= end}
                if series:
                    log.info("Got AMFI NAV data for %s via scheme %s", key, inst.amfi_scheme_code)
                    # cache under the original key so future lookups work
                    key = inst.isin
    else:  # MF
        if not inst.amfi_scheme_code:
            inst.amfi_scheme_code = resolve_mf_scheme_code(inst.isin)
            db.flush()
        if not inst.amfi_scheme_code:
            return {}
        full = fetch_mf_history(inst.amfi_scheme_code)
        series = {d: v for d, v in full.items() if start <= d <= end}

    if series:
        _cache_put(db, key, series)
        db.commit()
    return series


def latest_close(
    db: Session, inst: Instrument, on_or_before: date, cache_only: bool = False
) -> float | None:
    """Most recent close ≤ given date.

    Fast path: single SQL query for the most recent cached row within 14 days.
    This avoids the have_start/have_end two-point probe in get_close_series,
    which triggers a full history download when even one boundary date is missing
    (e.g. start date was a weekend with no market data).

    Slow path (cache miss): fetches only the last 30 days, not full history.
    """
    from sqlalchemy import desc as sa_desc

    key = _cache_key(inst)
    if not key:
        return None

    # Direct single-row query — no boundary probing, no range scan
    row = db.execute(
        select(PriceCache.close).where(
            PriceCache.key == key,
            PriceCache.on_date >= on_or_before - timedelta(days=14),
            PriceCache.on_date <= on_or_before,
        ).order_by(sa_desc(PriceCache.on_date)).limit(1)
    ).scalar()
    if row is not None:
        return float(row)

    if cache_only:
        return None

    # Cache miss — fetch only the last 30 days (not full history)
    series = get_close_series(
        db, inst, on_or_before - timedelta(days=30), on_or_before, cache_only=False
    )
    if not series:
        return None
    eligible = [d for d in series if d <= on_or_before]
    if not eligible:
        return None
    return series[max(eligible)]


def fill_forward(series: dict[date, float], start: date, end: date) -> dict[date, float]:
    """Forward-fill a daily-close series so every calendar date has a value.

    Used so portfolio-vs-benchmark math doesn't hiccup on weekends/holidays.
    """
    if not series:
        return {}
    out: dict[date, float] = {}
    last = None
    d = start
    while d <= end:
        if d in series:
            last = series[d]
        if last is not None:
            out[d] = last
        d += timedelta(days=1)
    return out


# Benchmark tickers — yfinance coverage of Indian indices is patchy, so each
# benchmark has a fallback chain. We fetch the first one that returns data.
BENCHMARKS: dict[str, str] = {
    "^NSEI": "NIFTY 50",
    "^CRSLDX": "NIFTY 500",
    "MID150BEES.NS": "NIFTY Midcap 150",
    "MOSMALL250.NS": "NIFTY Smallcap 250",
}

# Fallback chain per requested benchmark. We try these in order until one
# returns data. Indian-index coverage on yfinance is patchy and changes —
# logging shows which ticker won for each benchmark.
BENCHMARK_FALLBACKS: dict[str, list[str]] = {
    "^CRSLDX": ["NIFTY500.NS", "NIF500IETF.NS", "NIFTYBEES.NS"],
    "MID150BEES.NS": ["MIDCAPIETF.NS", "MIDCAPETF.NS", "MIDCAP.NS", "MID150NETF.NS"],
    "MOSMALL250.NS": ["SMALLCAP.NS", "SMALLCAPETF.NS", "NIFTYSMLCAP250.NS", "NETFSML.NS"],
}


def fetch_benchmark_series(
    db: Session, ticker: str, start: date, end: date, cache_only: bool = False
) -> dict[date, float]:
    """Cache + fetch a benchmark equity index from yfinance, with fallbacks."""
    have = _cache_get(db, ticker, [start, end])
    if len(have) == 2:
        rows = db.execute(
            select(PriceCache.on_date, PriceCache.close).where(
                PriceCache.key == ticker,
                PriceCache.on_date >= start,
                PriceCache.on_date <= end,
            )
        ).all()
        return {r.on_date: r.close for r in rows}

    if cache_only:
        rows = db.execute(
            select(PriceCache.on_date, PriceCache.close).where(
                PriceCache.key == ticker,
                PriceCache.on_date >= start,
                PriceCache.on_date <= end,
            )
        ).all()
        return {r.on_date: r.close for r in rows}

    candidates = [ticker] + BENCHMARK_FALLBACKS.get(ticker, [])
    for cand in candidates:
        series = fetch_equity_history(cand, start, end)
        if series:
            if cand != ticker:
                log.info("Benchmark %s: using fallback ticker %s", ticker, cand)
            _cache_put(db, ticker, series)
            db.commit()
            return series
    return {}
