from sqlalchemy import Column, Integer, String, DateTime, Text, Float, ForeignKey, Index
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from .base import Base


class NewsArticle(Base):
    """Represents a news article related to stocks."""
    
    __tablename__ = "news_articles"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(500), nullable=False)
    summary = Column(Text)
    url = Column(String(1000), unique=True, nullable=False)
    source = Column(String(100), nullable=False)
    published_at = Column(DateTime, nullable=False)
    
    # Sentiment analysis
    sentiment = Column(String(20))  # positive, neutral, negative
    sentiment_score = Column(Float)
    
    # Stock association (if article is about specific stock)
    stock_symbol = Column(String(10), index=True)
    
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Composite index for efficient queries
    __table_args__ = (
        Index('ix_news_articles_published_at', 'published_at'),
        Index('ix_news_articles_source_published', 'source', 'published_at'),
    )

    def __repr__(self):
        return f"<NewsArticle(title='{self.title[:50]}...', source='{self.source}')>"
