"""Bond analytics: interest income, tax treatment, coupon schedule.

Handles SGBs (Sovereign Gold Bonds) and other bonds stored in bond_details.

Indian tax treatment:
  SGB (held to maturity):
    - Capital gains on RBI redemption: EXEMPT (Section 47(viic) IT Act)
    - Capital gains on exchange sale before maturity: STCG/LTCG (24-month rule)
    - Interest income: Taxable at slab rate as "Income from Other Sources"

  Corporate bonds / G-Secs:
    - Capital gains: STCG (< 36 months) at slab / LTCG (≥ 36 months) at 20% with indexation
      [Note: 36-month threshold for debt instruments, changed in Budget 2023 — verify for current rules]
    - Interest income: Taxable at slab rate

SGB coupon schedule: paid semi-annually (typically Jan/Jul or Feb/Aug depending on issue series).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from dateutil.relativedelta import relativedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import BondDetail, Transaction

_MONTHS_PER_FREQ = {
    "semi-annual": 6,
    "annual":      12,
    "quarterly":   3,
}


def _coupon_dates(issue_date: date, maturity_date: date, frequency: str) -> list[date]:
    """Generate all coupon payment dates from issue to maturity (inclusive)."""
    months = _MONTHS_PER_FREQ.get(frequency, 6)
    dates: list[date] = []
    d = issue_date + relativedelta(months=months)
    while d <= maturity_date:
        dates.append(d)
        d += relativedelta(months=months)
    return dates


def coupon_per_payment(issue_price: float, coupon_rate: float, frequency: str) -> float:
    """Interest amount per coupon payment per unit."""
    periods = 12 / _MONTHS_PER_FREQ.get(frequency, 6)
    return issue_price * (coupon_rate / 100.0) / periods


def interest_earned_to_date(bond: BondDetail, units: float,
                             on_date: date | None = None) -> dict:
    """Total interest received to date and breakdown by financial year.

    Returns:
        total_interest    — cumulative interest received so far
        fy_interest       — interest received in current FY
        next_coupon_date  — next upcoming coupon date
        next_coupon_amount — amount per coupon payment
        coupon_schedule   — list of {date, amount, received}
    """
    on_date = on_date or date.today()
    per_payment = coupon_per_payment(bond.issue_price, bond.coupon_rate,
                                      bond.coupon_frequency)
    per_payment_total = per_payment * units

    all_dates = _coupon_dates(bond.issue_date, bond.maturity_date, bond.coupon_frequency)
    paid = [d for d in all_dates if d <= on_date]
    upcoming = [d for d in all_dates if d > on_date]

    total_interest = len(paid) * per_payment_total

    fy_year = on_date.year if on_date.month >= 4 else on_date.year - 1
    fy_start = date(fy_year, 4, 1)
    fy_paid = [d for d in paid if d >= fy_start]
    fy_interest = len(fy_paid) * per_payment_total

    schedule = [
        {"date": d.isoformat(), "amount": round(per_payment_total, 2),
         "received": d <= on_date}
        for d in all_dates
    ]

    return {
        "total_interest":     round(total_interest, 2),
        "fy_interest":        round(fy_interest, 2),
        "next_coupon_date":   upcoming[0].isoformat() if upcoming else None,
        "next_coupon_amount": round(per_payment_total, 2),
        "coupon_schedule":    schedule,
    }


def _gold_price_per_gram(db: Session) -> float | None:
    """Get current gold price in INR per gram using GOLDBEES as a proxy.

    After Nippon's 2019 10:1 split: 1 GOLDBEES unit = 0.01 gram of 24k gold.
    Therefore: gold_per_gram = GOLDBEES_price × 100.

    Strategy:
    1. Try today's cached price (already fresh from price refresh)
    2. If stale (> 2 days), fetch live from yfinance — bonds section should
       always show current gold value, not last-refresh value
    3. Fall back to older cache entry
    """
    import yfinance as yf
    from .db import PriceCache
    from sqlalchemy import desc as sa_desc
    from datetime import timedelta, datetime

    today_d = date.today()

    # Always fetch live for bond pricing — gold moves daily and
    # a stale 6-hour-old cache price would give significantly wrong SGB value
    for ticker, multiplier in [("GOLDBEES.NS", 100), ("GOLDADD.NS", 100)]:
        try:
            df = yf.Ticker(ticker).history(period="5d", auto_adjust=True)
            if df is not None and not df.empty:
                price = float(df["Close"].iloc[-1])
                if price > 5:
                    # Cache it for subsequent calls
                    from sqlalchemy.dialects.sqlite import insert as si
                    for ts, row in df.iterrows():
                        d = ts.date() if hasattr(ts, "date") else ts
                        stmt = si(PriceCache).values(
                            key=ticker, on_date=d, close=float(row["Close"]),
                            fetched_at=datetime.utcnow()
                        ).on_conflict_do_update(
                            index_elements=["key", "on_date"],
                            set_={"close": si(PriceCache).excluded.close,
                                  "fetched_at": si(PriceCache).excluded.fetched_at},
                        )
                        db.execute(stmt)
                    db.commit()
                    return round(price * multiplier, 2)
        except Exception:
            continue

    # Last resort: any cached entry within 30 days
    for gold_key in ("GOLDBEES.NS", "GOLDADD.NS"):
        row = db.execute(
            select(PriceCache.close)
            .where(PriceCache.key == gold_key,
                   PriceCache.on_date >= today_d - timedelta(days=30))
            .order_by(sa_desc(PriceCache.on_date)).limit(1)
        ).scalar()
        if row and float(row) > 5:
            return round(float(row) * 100, 2)
    return None


def compute_bond_holdings(db: Session, today: date | None = None) -> list[dict]:
    """Compute current bond holdings using quantity stored in bond_details.

    Pricing strategy per bond type:
      SGB     : current gold price per gram (via GOLDBEES × 100 proxy)
      corporate/g-sec : from price_cache if instrument exists, else None
      other   : None (user sees cost basis only)

    Bonds are self-contained — no equity tradebook entry needed.
    """
    today = today or date.today()
    all_bonds = db.execute(select(BondDetail)).scalars().all()
    if not all_bonds:
        return []

    # Get gold price once (used for all SGBs)
    gold_price = _gold_price_per_gram(db)

    rows: list[dict] = []
    for bd in sorted(all_bonds, key=lambda b: b.symbol):
        qty        = bd.quantity or 0.0
        buy_price  = bd.purchase_price or bd.issue_price
        cost_basis = qty * buy_price

        # Determine current price by bond type
        cur_price: float | None = None
        price_source = None

        # Manual price override takes priority over all auto-fetching
        if bd.price_override and bd.price_override > 0:
            cur_price    = bd.price_override
            price_source = "Manual override"
        elif bd.bond_type == "SGB":
            # Try direct NSE ticker first (most accurate — includes accrued interest premium)
            from .db import PriceCache, Instrument
            from sqlalchemy import desc as sa_desc
            direct_key = f"{bd.symbol}.NS"
            from datetime import timedelta as _td
            direct_row = db.execute(
                select(PriceCache.close)
                .where(PriceCache.key == direct_key,
                       PriceCache.on_date >= today - _td(days=14))
                .order_by(sa_desc(PriceCache.on_date)).limit(1)
            ).scalar()
            if direct_row and float(direct_row) > 1000:  # sanity: SGB > ₹1,000
                cur_price    = float(direct_row)
                price_source = f"{direct_key} (NSE)"
            else:
                # Fall back to GOLDBEES gold proxy
                cur_price    = gold_price
                price_source = "GOLDBEES × 100 (gold proxy)" if gold_price else None
        else:
            # Try price_cache via instrument record
            from .db import PriceCache, Instrument
            from sqlalchemy import desc as sa_desc
            inst = db.execute(
                select(Instrument).where(Instrument.symbol == bd.symbol)
            ).scalars().first()
            if inst:
                cache_key = inst.yf_ticker or inst.isin or bd.symbol
                row = db.execute(
                    select(PriceCache.close)
                    .where(PriceCache.key == cache_key, PriceCache.on_date <= today)
                    .order_by(sa_desc(PriceCache.on_date)).limit(1)
                ).scalar()
                if row:
                    cur_price    = float(row)
                    price_source = "yfinance"

        cur_value     = round(qty * cur_price, 2)  if cur_price and qty > 0 else None
        unrealized    = round(cur_value - cost_basis, 2) if cur_value is not None else None
        pct_return    = round((cur_value - cost_basis) / cost_basis * 100, 2) \
                        if cur_value and cost_basis > 0 else None

        # Interest schedule
        interest_info: dict = {}
        if bd.coupon_rate > 0 and qty > 0:
            interest_info = interest_earned_to_date(bd, qty, today)

        dtm = (bd.maturity_date - today).days

        rows.append({
            "id":              bd.id,
            "symbol":          bd.symbol,
            "full_name":       bd.full_name or bd.symbol,
            "bond_type":       bd.bond_type,
            "isin":            bd.isin,
            "units":           qty,
            "purchase_price":  buy_price,
            "issue_price":     bd.issue_price,
            "issue_date":      bd.issue_date.isoformat(),
            "purchase_date":   bd.purchase_date.isoformat() if bd.purchase_date else None,
            "maturity_date":   bd.maturity_date.isoformat(),
            "days_to_maturity": dtm,
            "coupon_rate":     bd.coupon_rate,
            "coupon_frequency": bd.coupon_frequency,
            "current_price":   round(cur_price, 2) if cur_price else None,
            "price_source":    price_source,
            "current_value":   cur_value,
            "cost_basis":      round(cost_basis, 2),
            "unrealized_pnl":  unrealized,
            "pct_return":      pct_return,
            # Interest income
            "total_interest_earned": interest_info.get("total_interest", 0),
            "fy_interest":           interest_info.get("fy_interest", 0),
            "next_coupon_date":      interest_info.get("next_coupon_date"),
            "next_coupon_amount":    interest_info.get("next_coupon_amount", 0),
            "coupon_schedule":       interest_info.get("coupon_schedule", []),
            # Tax
            "capital_gains_exempt_at_maturity": bd.capital_gains_exempt_at_maturity,
            "tax_note": (
                "Capital gains on RBI redemption at maturity are TAX EXEMPT "
                "(Section 47(viic) IT Act). Interest is taxable at your slab rate."
                if bd.capital_gains_exempt_at_maturity else
                "Capital gains: STCG/LTCG as per holding period. "
                "Interest taxable at slab rate."
            ),
        })

    return rows
