from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
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


def init_db() -> None:
    Base.metadata.create_all(engine)


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
