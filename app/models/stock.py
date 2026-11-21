from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, Text, Index, ForeignKey
from sqlalchemy.orm import relationship
from .base import Base, utcnow_naive


class Stock(Base):
    """Represents a JSE listed stock/company."""
    
    __tablename__ = "stocks"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(10), unique=True, index=True, nullable=False)
    name = Column(String(200), nullable=False)
    sector = Column(String(100))
    industry = Column(String(100))
    market_cap = Column(Float)
    shares_outstanding = Column(Float)
    description = Column(Text)
    website = Column(String(500))
    is_active = Column(Boolean, default=True)
    # Set once a Yahoo get_info profile fetch has been attempted for this
    # stock. Symbols whose profile has no sector (indexes like ^J203.JO,
    # sector-less listings) would otherwise trigger a get_info call on every
    # price save forever.
    details_fetched_at = Column(DateTime)
    # Last time update_dividends fetched this symbol. Dividends change at
    # declaration cadence (weeks), so the daily job skips symbols fetched
    # recently instead of spending a Yahoo request on each, every run.
    last_dividend_fetch = Column(DateTime)
    created_at = Column(DateTime, default=utcnow_naive)
    updated_at = Column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)

    # Relationships
    prices = relationship("StockPrice", back_populates="stock", cascade="all, delete-orphan")
    dividends = relationship("Dividend", back_populates="stock", cascade="all, delete-orphan")
    watchlist_items = relationship("WatchlistItem", back_populates="stock", cascade="all, delete-orphan")
    portfolio_holdings = relationship("PortfolioHolding", back_populates="stock", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Stock(symbol='{self.symbol}', name='{self.name}')>"


class StockPrice(Base):
    """Historical stock price data."""
    
    __tablename__ = "stock_prices"

    id = Column(Integer, primary_key=True, index=True)
    stock_id = Column(Integer, ForeignKey('stocks.id'), nullable=False, index=True)
    symbol = Column(String(10), index=True, nullable=False)
    timestamp = Column(DateTime, nullable=False, index=True)
    open_price = Column(Float)
    high_price = Column(Float)
    low_price = Column(Float)
    close_price = Column(Float, nullable=False)
    adjusted_close = Column(Float)
    volume = Column(Integer)
    
    # Technical indicators
    sma_20 = Column(Float)
    sma_50 = Column(Float)
    sma_200 = Column(Float)
    ema_12 = Column(Float)
    ema_26 = Column(Float)
    rsi = Column(Float)
    macd = Column(Float)
    macd_signal = Column(Float)
    macd_hist = Column(Float)
    bollinger_upper = Column(Float)
    bollinger_middle = Column(Float)
    bollinger_lower = Column(Float)
    atr = Column(Float)
    obv = Column(Float)
    
    created_at = Column(DateTime, default=utcnow_naive)

    # When this row was last (re)fetched from a provider. Unlike ``timestamp``
    # (the bar's as-of time), this advances on every refresh, including when an
    # existing bar is re-fetched and deduplicated.
    fetched_at = Column(DateTime, default=utcnow_naive, index=True)

    # Relationships
    stock = relationship("Stock", back_populates="prices")

    # One bar per (symbol, timestamp): the dashboard scheduler and the headless
    # updater can run concurrently, and dedup is check-then-insert - this
    # constraint is the last line of defence against duplicate bars.
    __table_args__ = (
        Index('ix_stock_prices_symbol_timestamp', 'symbol', 'timestamp', unique=True),
    )

    def __repr__(self):
        return f"<StockPrice(symbol='{self.symbol}', timestamp='{self.timestamp}', close={self.close_price})>"
