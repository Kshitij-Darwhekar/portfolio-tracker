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


def init_db() -> None:
    Base.metadata.create_all(engine)
    # One-time migration: add folio column if not yet present
    insp = inspect(engine)
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


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
