from sqlalchemy import Column, Integer, String, Float, DateTime, Date, ForeignKey, Index
from sqlalchemy.orm import relationship
from .base import Base, utcnow_naive


class Dividend(Base):
    """Represents dividend payments for stocks."""
    
    __tablename__ = "dividends"

    id = Column(Integer, primary_key=True, index=True)
    stock_id = Column(Integer, ForeignKey("stocks.id"), nullable=False, index=True)
    symbol = Column(String(10), index=True, nullable=False)

    # Dividend details
    amount = Column(Float, nullable=False)
    currency = Column(String(3), default="ZAR")
    declaration_date = Column(DateTime)
    ex_dividend_date = Column(DateTime, index=True)
    record_date = Column(DateTime)
    payment_date = Column(DateTime)
    frequency = Column(String(20))  # monthly, quarterly, semi-annual, annual
    
    created_at = Column(DateTime, default=utcnow_naive)

    # Relationships
    stock = relationship("Stock", back_populates="dividends")

    # One dividend per (symbol, ex-date, amount): dedup across providers.
    # The amount is part of the key because a special dividend can share an
    # ex-date with an interim - a 2-column key silently dropped one of them.
    __table_args__ = (
        Index('ix_dividends_symbol_ex_date', 'symbol', 'ex_dividend_date', 'amount', unique=True),
    )

    def __repr__(self):
        return f"<Dividend(symbol='{self.symbol}', amount={self.amount}, payment_date='{self.payment_date}')>"


class MoneywebDividendWatch(Base):
    """Snapshot of Moneyweb's authenticated Dividend Watch calendar."""

    __tablename__ = "moneyweb_dividend_watch"

    id = Column(Integer, primary_key=True, index=True)
    instrument = Column(String(50), nullable=False, index=True)
    declared_date = Column(Date)
    last_day_to_trade = Column(Date)
    pay_date = Column(Date)
    dividend_type = Column(String(20))
    value = Column(String(50))
    fetched_at = Column(DateTime, default=utcnow_naive)
    created_at = Column(DateTime, default=utcnow_naive)

    def __repr__(self):
        return f"<MoneywebDividendWatch(instrument='{self.instrument}', pay_date='{self.pay_date}', value='{self.value}')>"
