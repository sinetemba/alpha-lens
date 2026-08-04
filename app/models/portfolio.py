from sqlalchemy import Column, Integer, String, Float, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from .base import Base


class Portfolio(Base):
    """Represents a user's investment portfolio."""
    
    __tablename__ = "portfolios"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), default="My Portfolio")
    description = Column(Text)
    initial_investment = Column(Float, default=0.0)
    
    # Calculated fields
    current_value = Column(Float, default=0.0)
    total_gain_loss = Column(Float, default=0.0)
    total_gain_loss_percentage = Column(Float, default=0.0)
    
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Set only on a successful EasyEquities sync (not bumped by unrelated
    # writes like updated_at), so sync staleness can be tracked persistently
    # across app restarts/sessions instead of relying on session state.
    last_easyequities_sync = Column(DateTime)

    # Relationships
    holdings = relationship("PortfolioHolding", back_populates="portfolio", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Portfolio(name='{self.name}', current_value={self.current_value})>"


class PortfolioHolding(Base):
    """Represents a stock holding in a portfolio."""
    
    __tablename__ = "portfolio_holdings"

    id = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"), nullable=False)
    stock_id = Column(Integer, ForeignKey("stocks.id"), nullable=False)
    symbol = Column(String(10), index=True, nullable=False)
    account_type = Column(String(50), default="ZAR")
    
    # Purchase information
    quantity = Column(Float, nullable=False)
    purchase_price = Column(Float, nullable=False)
    purchase_date = Column(DateTime, nullable=False)
    
    # Current values (calculated)
    current_price = Column(Float)
    current_value = Column(Float)
    gain_loss = Column(Float)
    gain_loss_percentage = Column(Float)
    
    # Dividend tracking
    total_dividends_received = Column(Float, default=0.0)
    
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    portfolio = relationship("Portfolio", back_populates="holdings")
    stock = relationship("Stock", back_populates="portfolio_holdings")

    def __repr__(self):
        return f"<PortfolioHolding(symbol='{self.symbol}', quantity={self.quantity})>"
