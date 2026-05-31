"""Corporate actions: splits, bonuses, dividends, mergers, demergers.

Ratio convention:
  split / bonus / merger:
    ratio = new_shares / old_shares  (quantity multiplier, same as yfinance .splits)
    1:1 bonus → 2.0, 2:1 split → 2.0, reverse 1:2 → 0.5

  demerger (parent company side):
    ratio = fraction of original cost RETAINED in the parent after the spin-off.
    Quantity is unchanged — only cost basis is reduced.
    Example: ITC Hotels demerger → ITC ratio = 0.9072
    (9.28% of your ITC cost was transferred to ITC Hotels shares you received)

  The demerged child shares should be entered as a separate buy transaction
  at ₹0 (or at the allocated cost), so they appear correctly in holdings.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .db import CorporateAction, Instrument, Transaction

log = logging.getLogger(__name__)

# Actions that affect quantity (new_shares/old_shares multiplier)
QTY_SPLIT_TYPES = {"split", "bonus", "merger"}
# Actions that affect cost basis only (fraction of cost retained in parent)
COST_ONLY_TYPES = {"demerger"}
ALL_ADJ_TYPES = QTY_SPLIT_TYPES | COST_ONLY_TYPES


# ---------- query helpers ----------

def get_split_actions(db: Session, symbol: str) -> list[tuple[date, str, float]]:
    """Return (ex_date, action_type, ratio) for all adjusting actions, asc.

    Two behaviours depending on action_type:
      split/bonus/merger → qty multiplier (ratio = new_shares/old_shares)
      demerger           → cost-basis multiplier only (ratio = fraction retained,
                           quantity is NOT changed)
    """
    rows = db.execute(
        select(CorporateAction)
        .where(
            CorporateAction.symbol == symbol.upper(),
            CorporateAction.action_type.in_(ALL_ADJ_TYPES),
            CorporateAction.ratio.is_not(None),
        )
        .order_by(CorporateAction.ex_date)
    ).scalars().all()
    return [(r.ex_date, r.action_type, r.ratio) for r in rows]


def get_dividend_actions(db: Session, symbol: str) -> list[CorporateAction]:
    return db.execute(
        select(CorporateAction)
        .where(
            CorporateAction.symbol == symbol.upper(),
            CorporateAction.action_type == "dividend",
            CorporateAction.amount_per_share.is_not(None),
        )
        .order_by(CorporateAction.ex_date)
    ).scalars().all()


# ---------- factor math ----------

def cumulative_factor(split_actions: list[tuple[date, str, float]], trade_date: date) -> float:
    """Qty multiplier from all split/bonus/merger actions AFTER trade_date.
    Demerger actions are intentionally excluded — they only affect cost basis."""
    factor = 1.0
    for ex_date, action_type, ratio in split_actions:
        if ex_date > trade_date and action_type in QTY_SPLIT_TYPES:
            factor *= ratio
    return factor


# ---------- adjusted holding state ----------

def adjusted_holding_state(
    txns: Sequence[Transaction],
    split_actions: list[tuple[date, float]],
) -> tuple[float, float, float, float]:
    """(adj_qty, adj_avg_cost, realized_pnl, fractional_cash).

    Uses a pure chronological replay — buys use ORIGINAL qty/price, and the
    split ratio is applied exactly once when the corporate action ex-date fires.
    This avoids the double-counting that occurs when mixing cumulative_factor
    (forward-looking) with an event loop.

    Indian market has no fractional shares: when a bonus/split produces a
    fractional result (e.g. 3 × 2.5 = 7.5), the floor (7) is kept and the
    fraction (0.5) is paid out as cash at the prevailing avg_cost.
    """
    qty = 0.0
    avg = 0.0
    realized = 0.0
    fractional_cash = 0.0

    # Sort order within a date:
    #   0 = buy  (must come before sells so same-day intraday trades net to zero)
    #   1 = sell
    #   2 = corporate action event (after all transactions on that date)
    all_events: list[tuple[date, int, object]] = []
    for t in sorted(txns, key=lambda t: (t.trade_date, t.id)):
        kind = 0 if t.trade_type == "buy" else 1
        all_events.append((t.trade_date, kind, t))
    for ex_date, action_type, ratio in split_actions:
        all_events.append((ex_date, 2, (action_type, ratio)))

    all_events.sort(key=lambda x: (x[0], x[1]))

    for _date, kind_order, payload in all_events:
        if kind_order == 0:                               # buy
            t = payload
            cost = t.quantity * t.price + t.fees
            new_qty = qty + t.quantity
            avg = (qty * avg + cost) / new_qty
            qty = new_qty
        elif kind_order == 1:                             # sell — already post-split
            t = payload
            sell_qty = min(t.quantity, qty)
            realized += sell_qty * (t.price - avg) - t.fees
            qty -= sell_qty
            if qty <= 1e-9:
                qty, avg = 0.0, 0.0
        else:                                             # corporate action (kind_order == 2)
            action_type, ratio = payload
            if action_type == "demerger":
                # Demerger of the parent company: quantity stays the same, only
                # cost basis is reduced because part of the original cost is now
                # attributed to the spun-off child company's shares.
                # ratio = fraction of cost retained (e.g. 0.9072 for ITC after Hotels spin-off)
                avg = avg * ratio
            else:
                # split / bonus / merger: quantity multiplied, avg cost adjusted inversely
                new_raw = qty * ratio
                new_floored = float(int(new_raw))
                fraction = new_raw - new_floored
                if fraction > 1e-9 and avg > 0:
                    fractional_cash += fraction * avg
                    realized += fraction * avg
                qty = new_floored
                if qty > 0 and ratio > 0:
                    avg = avg / ratio

    return qty, avg, realized, fractional_cash


def qty_held_on(
    txns: Sequence[Transaction],
    split_actions: list[tuple[date, float]],
    on_date: date,
) -> float:
    """Adjusted integer qty held at end of a given date (same replay as adjusted_holding_state)."""
    all_events: list[tuple[date, int, object]] = []
    for t in sorted(txns, key=lambda t: (t.trade_date, t.id)):
        if t.trade_date <= on_date:
            kind = 0 if t.trade_type == "buy" else 1
            all_events.append((t.trade_date, kind, t))
    for ex_date, action_type, ratio in split_actions:
        if ex_date <= on_date:
            all_events.append((ex_date, 2, (action_type, ratio)))
    all_events.sort(key=lambda x: (x[0], x[1]))

    qty = 0.0
    for _date, kind_order, payload in all_events:
        if kind_order == 0:       # buy
            qty += payload.quantity
        elif kind_order == 1:     # sell
            qty = max(0.0, qty - payload.quantity)
        else:                     # corporate action
            action_type, ratio = payload
            if action_type != "demerger":  # demerger doesn't change qty
                qty = float(int(qty * ratio))
    return qty


def dividend_cashflows(
    db: Session,
    symbol: str,
    txns: Sequence[Transaction],
    split_actions: list[tuple[date, float]],
) -> list[tuple[date, float]]:
    """Positive cashflows for dividends received, sized by adjusted qty on ex_date."""
    actions = get_dividend_actions(db, symbol)
    flows: list[tuple[date, float]] = []
    for ca in actions:
        q = qty_held_on(txns, split_actions, ca.ex_date)
        if q > 0 and ca.amount_per_share:
            flows.append((ca.ex_date, q * ca.amount_per_share))
    return flows


# ---------- yfinance auto-fetch ----------

def fetch_yfinance_corporate_actions(
    db: Session, symbol: str, yf_ticker: str
) -> dict[str, int]:
    """Fetch splits and dividends from yfinance and upsert into corporate_actions.

    Returns {splits_upserted, dividends_upserted}.
    """
    import yfinance as yf

    sym = symbol.upper()
    splits_n = 0
    divs_n = 0

    try:
        ticker = yf.Ticker(yf_ticker)

        # --- splits ---
        splits = ticker.splits
        if splits is not None and not splits.empty:
            for ts, ratio in splits.items():
                if ratio <= 0:
                    continue
                ex_d = ts.date() if hasattr(ts, "date") else ts
                stmt = (
                    sqlite_insert(CorporateAction)
                    .values(
                        symbol=sym,
                        action_type="split",
                        ex_date=ex_d,
                        ratio=float(ratio),
                        source="yfinance",
                        created_at=datetime.utcnow(),
                    )
                    .on_conflict_do_update(
                        index_elements=["symbol", "ex_date", "action_type"],
                        set_={"ratio": float(ratio), "source": "yfinance"},
                    )
                )
                db.execute(stmt)
                splits_n += 1

        # --- dividends ---
        divs = ticker.dividends
        if divs is not None and not divs.empty:
            for ts, amount in divs.items():
                if amount <= 0:
                    continue
                ex_d = ts.date() if hasattr(ts, "date") else ts
                stmt = (
                    sqlite_insert(CorporateAction)
                    .values(
                        symbol=sym,
                        action_type="dividend",
                        ex_date=ex_d,
                        amount_per_share=float(amount),
                        source="yfinance",
                        created_at=datetime.utcnow(),
                    )
                    .on_conflict_do_update(
                        index_elements=["symbol", "ex_date", "action_type"],
                        set_={"amount_per_share": float(amount), "source": "yfinance"},
                    )
                )
                db.execute(stmt)
                divs_n += 1

        db.commit()
        log.info(
            "%s: fetched %d splits, %d dividends from yfinance",
            yf_ticker, splits_n, divs_n,
        )
    except Exception as e:
        db.rollback()
        log.warning("Corporate actions fetch failed for %s: %s", yf_ticker, e)

    return {"splits": splits_n, "dividends": divs_n}


def auto_fetch_all(db: Session) -> dict:
    """Fetch corporate actions from yfinance for every EQ symbol in transactions.

    Drives off the transactions table (not instruments) so demerger-received shares
    that have a transaction but no instrument record are still included.
    """
    from .db import Instrument, Transaction
    from .prices import resolve_yf_ticker

    # Collect unique (symbol, exchange) pairs for EQ segment
    rows = db.execute(
        select(Transaction.symbol, Transaction.exchange, Transaction.isin)
        .where(Transaction.segment == "EQ")
        .distinct()
    ).all()

    seen: set[str] = set()
    results = {}
    for symbol, exchange, isin in rows:
        sym = symbol.upper()
        if sym in seen:
            continue
        seen.add(sym)

        # Resolve yf_ticker: prefer instruments table, fall back to resolve_yf_ticker
        inst = None
        if isin:
            inst = db.get(Instrument, isin)
        yf_ticker = (inst.yf_ticker if inst and inst.yf_ticker else None) or resolve_yf_ticker(sym, exchange)

        r = fetch_yfinance_corporate_actions(db, sym, yf_ticker)
        results[sym] = r

    return {"symbols_processed": len(seen), "by_symbol": results}
