from sqlalchemy import Column, Integer, String, Float, DateTime, Text, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from .base import Base


class WatchlistItem(Base):
    """Represents a stock in user's watchlist."""
    
    __tablename__ = "watchlist_items"

    id = Column(Integer, primary_key=True, index=True)
    stock_id = Column(Integer, ForeignKey("stocks.id"), nullable=False)
    symbol = Column(String(10), index=True, nullable=False)
    
    # User-defined fields
    purchase_price = Column(Float)
    target_price = Column(Float)
    stop_loss = Column(Float)
    notes = Column(Text)
    
    # Notification preferences
    notify_price_increase = Column(Boolean, default=False)
    notify_price_decrease = Column(Boolean, default=False)
    price_change_threshold = Column(Float, default=5.0)  # percentage
    notify_news = Column(Boolean, default=True)
    
    is_active = Column(Boolean, default=True)
    added_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    stock = relationship("Stock", back_populates="watchlist_items")

    def __repr__(self):
        return f"<WatchlistItem(symbol='{self.symbol}', target_price={self.target_price})>"
