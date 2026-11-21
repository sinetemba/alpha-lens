from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean, Index
from .base import Base, utcnow_naive


class Notification(Base):
    """Represents a notification sent to the user."""

    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    type = Column(String(50), nullable=False, index=True)  # price, news, dividend, technical, portfolio
    title = Column(String(500), nullable=False)
    message = Column(Text, nullable=False)
    
    # Associated stock (if applicable)
    stock_symbol = Column(String(10), index=True)
    
    # Metadata
    meta_data = Column(Text)  # JSON string with additional data
    
    # Delivery status
    is_sent = Column(Boolean, default=False)
    sent_at = Column(DateTime)
    delivery_channel = Column(String(50))  # email, ntfy, discord, telegram, desktop
    # Failed delivery attempts - send_pending_notifications skips rows past the cap.
    attempts = Column(Integer, default=0)
    
    # User interaction
    is_read = Column(Boolean, default=False)
    read_at = Column(DateTime)
    
    created_at = Column(DateTime, default=utcnow_naive, index=True)

    # Composite indexes serve the two hot filters that also order/limit by
    # recency: the unread badge/page (is_read) and the pending-send scan
    # (is_sent). _ensure_plain_indexes creates them on existing databases.
    __table_args__ = (
        Index("ix_notifications_is_read_created", "is_read", "created_at"),
        Index("ix_notifications_is_sent_created", "is_sent", "created_at"),
    )

    def __repr__(self):
        return f"<Notification(type='{self.type}', title='{self.title[:50]}...', is_sent={self.is_sent})>"
