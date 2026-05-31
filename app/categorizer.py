"""Auto-categorisation of instruments into asset classes for Net Worth breakdown.

Categories:
  equity   — Direct stocks, equity MFs, equity ETFs (NIFTYBEES, sector ETFs)
  debt     — Debt MFs, liquid/money-market ETFs, FDs/RDs
  gold     — Gold ETFs (GOLDBEES, GOLDADD), gold MFs, SGBs
  hybrid   — Balanced / hybrid / dynamic asset allocation MFs
  cash     — Liquid funds, overnight funds, ultra-short debt
  silver   — Silver ETFs (SILVERBEES)
  other    — Unclassified

The category is stored per instrument and can be overridden by the user.
Auto-detection runs on first encounter; manual overrides are permanent.
"""

from __future__ import annotations

import re
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Instrument

# ---- keyword rules applied to lowercased instrument name / symbol ----

_GOLD_KEYWORDS   = re.compile(
    r'\bgold\b|goldbees|goldadd|goldetfadd|sovereign gold bond|sgb', re.I)
_SILVER_KEYWORDS = re.compile(r'\bsilver\b|silverbees', re.I)
_CASH_KEYWORDS   = re.compile(
    r'liquid|overnight|money market|ultra short|floater|arbitrage', re.I)
_DEBT_KEYWORDS   = re.compile(
    r'\bdebt\b|gilt|credit risk|banking.*psu|'
    r'corporate bond|dynamic bond|'
    r'\bincome fund\b|\bbond fund\b', re.I)
_HYBRID_KEYWORDS = re.compile(
    r'hybrid|balanced|dynamic asset|multi asset|flexi cap.*balanced|'
    r'equity savings|aggressive hybrid|conservative hybrid', re.I)
_EQUITY_ETF_KEYWORDS = re.compile(
    r'niftybees|sensexbees|bankbees|juniorbees|infrabees|'
    r'psubees|hdfcsml250|itbees|mafang|hngsngbees|mos.*nifty|'
    r'mid.*bees|small.*bees|mosmidcap|mosmall|kotak.*nifty|'
    r'tata.*nifty|nippon.*nifty|axis.*nifty|hdfc.*nifty|'
    r'index fund|nifty.*etf', re.I)

# Known SGB ISINs prefix — all Sovereign Gold Bonds
_SGB_SYMBOL = re.compile(r'^SGB[A-Z0-9]+$', re.I)


def auto_detect_category(isin: str, symbol: str, name: str | None,
                         segment: str) -> str:
    """Return the most likely asset class category for an instrument.

    Uses the instrument name (most descriptive) and symbol as signals.
    MFs and ETFs are classified by fund type; direct stocks default to equity.
    """
    label = f"{name or ''} {symbol or ''}".lower()

    # Gold — check first (some gold funds also have "equity" in the path)
    if _GOLD_KEYWORDS.search(label) or _SGB_SYMBOL.match(symbol or ''):
        return "gold"

    # Silver
    if _SILVER_KEYWORDS.search(label):
        return "silver"

    # Cash / liquid (before debt — liquid is more specific)
    if _CASH_KEYWORDS.search(label):
        return "cash"

    # Hybrid
    if _HYBRID_KEYWORDS.search(label):
        return "hybrid"

    # Debt
    if _DEBT_KEYWORDS.search(label):
        return "debt"

    # Equity ETFs — exchange-traded equity indices
    if _EQUITY_ETF_KEYWORDS.search(label):
        return "equity"

    # For MFs: default to equity (covers large/mid/small cap funds)
    # For direct EQ segment: always equity
    return "equity"


def ensure_all_categorised(db: Session) -> int:
    """Assign auto-detected category to any instrument that has none.
    Returns count of instruments updated.
    """
    instruments = db.execute(
        select(Instrument).where(Instrument.asset_category.is_(None))
    ).scalars().all()
    updated = 0
    for inst in instruments:
        cat = auto_detect_category(inst.isin, inst.symbol, inst.name, inst.segment)
        inst.asset_category = cat
        updated += 1
    if updated:
        db.commit()
    return updated


def category_breakdown(db: Session, holdings_value: dict[str, float],
                       fi_value: float, epf_value: float) -> dict[str, float]:
    """Aggregate current values by asset category.

    holdings_value: {isin: current_value_rupees} from compute_holdings
    fi_value:  total FI (FDs/RDs) — always Debt
    epf_value: EPF balance — also Debt (fixed-rate government-backed, like a long bond)

    Rationale for EPF in Debt:
      EPF earns a government-declared fixed rate (8.25%), has no market risk, and
      behaves like a long-duration government bond. Including it in Debt gives an
      accurate picture of your true equity:debt allocation. Standard practice per
      Indian financial planning frameworks (Freefincal, Arthgyaan, etc.).

    Cash/Liquid funds (LIQUIDCASE etc.) are also merged into Debt since they are
    money-market instruments — the most liquid sub-category of debt.
    """
    ensure_all_categorised(db)

    # Merge cash into debt — liquid/overnight funds are still debt instruments
    _MERGE_INTO_DEBT = {"cash"}

    by_cat: dict[str, float] = {}

    for isin, value in holdings_value.items():
        if value <= 0:
            continue
        inst = db.get(Instrument, isin)
        raw_cat = inst.asset_category if inst and inst.asset_category else "equity"
        cat = "debt" if raw_cat in _MERGE_INTO_DEBT else raw_cat
        by_cat[cat] = by_cat.get(cat, 0) + value

    # FD/RD → Debt
    if fi_value > 0:
        by_cat["debt"] = by_cat.get("debt", 0) + fi_value

    # EPF → Debt (not a separate bucket)
    if epf_value > 0:
        by_cat["debt"] = by_cat.get("debt", 0) + epf_value

    return {k: round(v, 2) for k, v in sorted(by_cat.items(),
                                                key=lambda x: x[1], reverse=True)}
