from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Index
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from .base import Base


class Dividend(Base):
    """Represents dividend payments for stocks."""
    
    __tablename__ = "dividends"

    id = Column(Integer, primary_key=True, index=True)
    stock_id = Column(Integer, ForeignKey("stocks.id"), nullable=False)
    symbol = Column(String(10), index=True, nullable=False)
    
    # Dividend details
    amount = Column(Float, nullable=False)
    currency = Column(String(3), default="ZAR")
    declaration_date = Column(DateTime)
    ex_dividend_date = Column(DateTime, index=True)
    record_date = Column(DateTime)
    payment_date = Column(DateTime)
    frequency = Column(String(20))  # monthly, quarterly, semi-annual, annual
    
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Relationships
    stock = relationship("Stock", back_populates="dividends")

    # Composite index for efficient queries
    __table_args__ = (
        Index('ix_dividends_symbol_ex_date', 'symbol', 'ex_dividend_date'),
    )

    def __repr__(self):
        return f"<Dividend(symbol='{self.symbol}', amount={self.amount}, payment_date='{self.payment_date}')>"
