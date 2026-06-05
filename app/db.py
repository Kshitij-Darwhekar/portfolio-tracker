from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "portfolio.db"

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(Date, nullable=False, index=True)
    symbol = Column(String, nullable=False, index=True)
    isin = Column(String, nullable=True, index=True)
    exchange = Column(String, nullable=True)
    segment = Column(String, nullable=False)  # 'EQ' or 'MF'
    trade_type = Column(String, nullable=False)  # 'buy' or 'sell'
    quantity = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    fees = Column(Float, nullable=False, default=0.0)
    notes = Column(String, nullable=True)
    source = Column(String, nullable=False, default="manual")  # 'import' | 'manual'
    trade_id = Column(String, nullable=True)
    folio = Column(String, nullable=True)   # MF folio number (from CAS import)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("trade_id", name="uq_trade_id"),)


class Instrument(Base):
    __tablename__ = "instruments"

    isin = Column(String, primary_key=True)
    symbol = Column(String, nullable=False)
    name = Column(String, nullable=True)
    segment = Column(String, nullable=False)  # 'EQ' or 'MF'
    yf_ticker = Column(String, nullable=True)  # e.g. RELIANCE.NS
    amfi_scheme_code = Column(String, nullable=True)
    last_resolved = Column(DateTime, nullable=True)
    # Asset class category for Net Worth breakdown
    # equity | debt | gold | hybrid | cash | silver | other
    asset_category = Column(String, nullable=True)
    # Market cap classification (from yfinance info + SEBI thresholds)
    # large | mid | small  (None = not yet fetched)
    market_cap_category = Column(String, nullable=True)
    # Sector from yfinance info (e.g. "Technology", "Financial Services")
    sector = Column(String, nullable=True)


class PriceCache(Base):
    __tablename__ = "price_cache"

    key = Column(String, primary_key=True)  # ISIN for MF, yf_ticker for EQ
    on_date = Column(Date, primary_key=True)
    close = Column(Float, nullable=False)
    fetched_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class CorporateAction(Base):
    __tablename__ = "corporate_actions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String, nullable=False, index=True)
    isin = Column(String, nullable=True)
    action_type = Column(String, nullable=False)  # split|bonus|dividend|merger|demerger
    ex_date = Column(Date, nullable=False, index=True)
    # split/bonus/merger/demerger: new_shares per old_share (e.g. 1:1 bonus = 2.0)
    ratio = Column(Float, nullable=True)
    amount_per_share = Column(Float, nullable=True)  # dividends only
    notes = Column(String, nullable=True)
    source = Column(String, nullable=False, default="manual")  # manual|yfinance
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("symbol", "ex_date", "action_type", name="uq_ca_symbol_date_type"),
    )


class FixedIncome(Base):
    """Fixed Deposits (cumulative & non-cumulative) and Recurring Deposits."""
    __tablename__ = "fixed_income"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    fi_type          = Column(String, nullable=False)   # FD_CUM | FD_NON_CUM | RD
    bank             = Column(String, nullable=False)
    account_no       = Column(String, nullable=True)    # optional reference number
    amount           = Column(Float, nullable=False)    # FD: principal; RD: monthly instalment
    start_date       = Column(Date, nullable=False)
    maturity_date    = Column(Date, nullable=False)
    interest_rate    = Column(Float, nullable=False)    # annual %, e.g. 7.5
    # quarterly | monthly | half_yearly | annual | simple
    compounding      = Column(String, nullable=False, default="quarterly")
    # Non-cumulative only: monthly | quarterly | annual
    payout_frequency = Column(String, nullable=True)
    # RD only: optional lump-sum deposited on start_date in addition to monthly instalments
    initial_deposit  = Column(Float, nullable=True, default=0.0)
    is_tax_saver     = Column(Boolean, nullable=False, default=False)  # 80C 5-year FD
    notes            = Column(String, nullable=True)
    created_at       = Column(DateTime, nullable=False, default=datetime.utcnow)


class EPFEntry(Base):
    """One row from the EPFO passbook — contribution, interest, or withdrawal."""
    __tablename__ = "epf_entries"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    entry_type       = Column(String, nullable=False)   # contribution|interest|withdrawal|opening
    month            = Column(Date, nullable=False, index=True)  # first day of the month
    employee_share   = Column(Float, nullable=False, default=0.0)  # EPF employee contribution
    employer_share   = Column(Float, nullable=False, default=0.0)  # EPF employer contribution
    pension_contrib  = Column(Float, nullable=False, default=0.0)  # EPS pension contribution
    # Withdrawals are negative — stored as positive numbers here, sign handled in analytics
    employee_withdrawal = Column(Float, nullable=False, default=0.0)
    employer_withdrawal = Column(Float, nullable=False, default=0.0)
    source           = Column(String, nullable=False, default="passbook_import")
    created_at       = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("entry_type", "month", name="uq_epf_entry_type_month"),
    )


class SIPSchedule(Base):
    """Recurring SIP schedule for automatic transaction generation.

    The processor generates one MF Transaction per due SIP date using the
    official AMFI NAV from mfapi.in. If the SIP date is a market holiday,
    the next available trading day's NAV is used automatically.

    start_date is optional (defaults to today) so you can register future
    SIPs without affecting existing CAS-imported history.
    """
    __tablename__ = "sip_schedules"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    isin             = Column(String, nullable=False, index=True)
    scheme_name      = Column(String, nullable=True)
    folio            = Column(String, nullable=True)
    amount           = Column(Float, nullable=False)        # ₹ per instalment
    sip_day          = Column(Integer, nullable=False)      # day of month 1-28
    start_date       = Column(Date, nullable=False)         # first SIP date
    end_date         = Column(Date, nullable=True)          # None = ongoing
    is_active        = Column(Boolean, nullable=False, default=True)
    last_synced_date = Column(Date, nullable=True)          # last processed date
    notes            = Column(String, nullable=True)
    created_at       = Column(DateTime, nullable=False, default=datetime.utcnow)


class GlobalEquityTransaction(Base):
    """US / international stock transaction in USD (via INDMoney / Alpaca)."""
    __tablename__ = "global_equity_transactions"

    id                  = Column(Integer, primary_key=True, autoincrement=True)
    stock_name          = Column(String, nullable=True)    # full company name
    symbol              = Column(String, nullable=False, index=True)  # US ticker e.g. MSFT
    trade_date          = Column(Date, nullable=False, index=True)
    execution_time      = Column(DateTime, nullable=True)
    trade_type          = Column(String, nullable=False)    # 'buy' | 'sell'
    quantity            = Column(Float, nullable=False)     # fractional shares
    price_usd           = Column(Float, nullable=False)     # price per share in USD
    amount_usd          = Column(Float, nullable=False)     # total USD
    fees_usd            = Column(Float, nullable=False, default=0.0)
    exchange_rate       = Column(Float, nullable=True)      # USD/INR at trade date
    broker_ref          = Column(String, nullable=True)     # deduplication key
    notes               = Column(String, nullable=True)
    source              = Column(String, nullable=False, default="manual")
    created_at          = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("broker_ref", name="uq_global_broker_ref"),
    )


class BondDetail(Base):
    """Metadata for bonds held in the equity tradebook (SGBs, corporate bonds etc.).

    The actual buy/sell transactions are in the transactions table (segment='EQ').
    This table stores the bond-specific details needed for interest/tax calculations.
    """
    __tablename__ = "bond_details"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    symbol           = Column(String, nullable=False, unique=True, index=True)
    isin             = Column(String, nullable=True)
    bond_type        = Column(String, nullable=False, default="SGB")  # SGB|corporate|g-sec|other
    full_name        = Column(String, nullable=True)
    issue_price      = Column(Float, nullable=False)   # ₹ per unit at issue
    issue_date       = Column(Date, nullable=False)
    maturity_date    = Column(Date, nullable=False)
    coupon_rate      = Column(Float, nullable=False, default=0.0)   # annual % on issue price
    coupon_frequency = Column(String, nullable=False, default="semi-annual")  # semi-annual|annual|quarterly
    # Position — stored here so bonds don't need an equity tradebook entry
    quantity         = Column(Float, nullable=False, default=0.0)   # units held
    purchase_price   = Column(Float, nullable=True)  # actual price paid (may differ from issue_price)
    purchase_date    = Column(Date, nullable=True)
    # Tax flags
    capital_gains_exempt_at_maturity = Column(Boolean, nullable=False, default=False)
    # True for SGBs held to maturity (Section 47(viic)); False for corporate bonds
    # Optional manual price override — set this to the NSE/RBI price if auto-fetch is wrong
    price_override   = Column(Float, nullable=True)
    notes            = Column(String, nullable=True)
    created_at       = Column(DateTime, nullable=False, default=datetime.utcnow)


class NWSnapshot(Base):
    """User-recorded net worth on a specific date (from personal tracking).

    Used to overlay actual data points on the NW projection chart, letting
    the user see where the computed historical line matches their real records.
    """
    __tablename__ = "nw_snapshots"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    snap_date  = Column(Date, nullable=False, unique=True, index=True)
    amount     = Column(Float, nullable=False)   # actual NW in ₹
    label      = Column(String, nullable=True)   # e.g. "₹1L milestone"
    notes      = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


def init_db() -> None:
    Base.metadata.create_all(engine)
    # One-time migration: add folio column if not yet present
    insp = inspect(engine)
    # Migrate instruments.asset_category if not yet present
    if "instruments" in insp.get_table_names():
        inst_cols = {c["name"] for c in insp.get_columns("instruments")}
        if "asset_category" not in inst_cols:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE instruments ADD COLUMN asset_category TEXT"))
                conn.commit()

    # Migrate bond_details new columns if table already exists
    if "bond_details" in insp.get_table_names():
        bd_cols = {c["name"] for c in insp.get_columns("bond_details")}
        for col_sql in [
            "ALTER TABLE bond_details ADD COLUMN quantity REAL DEFAULT 0",
            "ALTER TABLE bond_details ADD COLUMN purchase_price REAL",
            "ALTER TABLE bond_details ADD COLUMN purchase_date TEXT",
            "ALTER TABLE bond_details ADD COLUMN price_override REAL",
        ]:
            col_name = col_sql.split("ADD COLUMN ")[1].split()[0]
            if col_name not in bd_cols:
                with engine.connect() as conn:
                    conn.execute(text(col_sql))
                    conn.commit()

    # Seed known SGB details if bond_details table is newly created
    if "bond_details" in insp.get_table_names():
        with SessionLocal() as _db:
            _sa_select = __import__('sqlalchemy', fromlist=['select']).select
            existing = _db.execute(
                _sa_select(BondDetail).where(BondDetail.symbol.in_(['SGBDE31III', 'SGBDEC31III']))
            ).scalar_one_or_none()
            if not existing:
                from datetime import date as _date
                _db.add(BondDetail(
                    symbol        = 'SGBDE31III',    # Correct NSE ticker
                    isin          = 'IN0020230168',  # Confirmed from NSE page
                    bond_type     = 'SGB',
                    full_name     = 'Sovereign Gold Bond 2023-24 Series III (Dec 2031)',
                    issue_price   = 6149.0,
                    issue_date    = _date(2023, 12, 28),
                    maturity_date = _date(2031, 12, 28),
                    coupon_rate   = 2.50,
                    coupon_frequency = 'semi-annual',
                    capital_gains_exempt_at_maturity = True,
                    notes         = 'NSE ticker: SGBDE31III. Capital gains at maturity: EXEMPT. '
                                    'Interest 2.5% p.a. on issue price: taxable at slab rate.',
                ))
                _db.commit()

    # Migrate fixed_income.initial_deposit if table already exists
    if "fixed_income" in insp.get_table_names():
        fi_cols = {c["name"] for c in insp.get_columns("fixed_income")}
        if "initial_deposit" not in fi_cols:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE fixed_income ADD COLUMN initial_deposit REAL DEFAULT 0"))
                conn.commit()

    existing_cols = {c["name"] for c in insp.get_columns("transactions")}
    if "folio" not in existing_cols:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE transactions ADD COLUMN folio TEXT"))
            conn.commit()

    if "instruments" in insp.get_table_names():
        inst_cols = {c["name"] for c in insp.get_columns("instruments")}
        for col_name in ("market_cap_category", "sector"):
            if col_name not in inst_cols:
                with engine.connect() as conn:
                    conn.execute(text(f"ALTER TABLE instruments ADD COLUMN {col_name} TEXT"))
                    conn.commit()


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
