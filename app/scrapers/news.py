from typing import List, Dict, Optional
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from app.models.news import NewsArticle
from app.models.stock import Stock
from app.scrapers.rss import RSSScraper
from loguru import logger
import re


class NewsScraper:
    """News scraper that aggregates and processes news articles."""
    
    def __init__(self, db: Session):
        self.db = db
        self.rss_scraper = RSSScraper()
    
    def scrape_all_sources(self) -> List[NewsArticle]:
        """Scrape news from all configured sources."""
        logger.info("Starting news scraping from all sources")
        
        all_articles_data = self.rss_scraper.fetch_all_feeds()
        saved_articles = []
        
        for article_data in all_articles_data:
            article = self._save_article(article_data)
            if article:
                saved_articles.append(article)
        
        logger.info(f"Scraped and saved {len(saved_articles)} new articles")
        return saved_articles
    
    def _save_article(self, article_data: Dict) -> Optional[NewsArticle]:
        """Save an article to the database if it doesn't already exist."""
        try:
            # Check if article already exists by URL
            existing = self.db.query(NewsArticle).filter(
                NewsArticle.url == article_data["url"]
            ).first()
            
            if existing:
                return None
            
            # Extract stock symbol from title if possible
            stock_symbol = self._extract_stock_symbol(article_data["title"])
            
            article = NewsArticle(
                title=article_data["title"],
                summary=article_data["summary"],
                url=article_data["url"],
                source=article_data["source"],
                published_at=article_data["published_at"] or datetime.now(timezone.utc),
                stock_symbol=stock_symbol,
            )
            
            self.db.add(article)
            self.db.commit()
            
            return article
        
        except Exception as e:
            logger.error(f"Error saving article: {e}")
            self.db.rollback()
            return None
    
    def _extract_stock_symbol(self, text: str) -> Optional[str]:
        """Extract JSE stock symbol from text."""
        # Common JSE symbols pattern (3-4 uppercase letters)
        # This is a simple extraction - could be enhanced with NLP
        jse_symbols = [
            "NPN", "CFR", "AGL", "SOL", "SBK", "MTN", "VOD", "ABG", "BID", "RCL",
            "FSR", "REM", "TSG", "GRT", "WHL", "TRU", "SHP", "MNP", "OML", "IMP",
            "AMS", "GLN", "BVT", "NED", "SBG", "DSY", "RMI", "VKE", "CPI", "TBS",
        ]
        
        text_upper = text.upper()
        
        for symbol in jse_symbols:
            # Look for pattern like "Naspers (NPN)" or "NPN.J"
            if symbol in text_upper:
                return symbol
        
        return None
    
    def get_recent_news(self, days: int = 7, symbol: Optional[str] = None) -> List[NewsArticle]:
        """Get recent news articles."""
        query = self.db.query(NewsArticle).filter(
            NewsArticle.published_at >= datetime.now(timezone.utc) - timedelta(days=days)
        )
        
        if symbol:
            query = query.filter(NewsArticle.stock_symbol == symbol)
        
        return query.order_by(NewsArticle.published_at.desc()).all()
    
    def get_news_by_source(self, source: str, limit: int = 50) -> List[NewsArticle]:
        """Get news articles by source."""
        return self.db.query(NewsArticle).filter(
            NewsArticle.source == source
        ).order_by(NewsArticle.published_at.desc()).limit(limit).all()
    
    def clean_old_articles(self, days: int = 90) -> int:
        """Delete articles older than specified days."""
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
        
        deleted = self.db.query(NewsArticle).filter(
            NewsArticle.published_at < cutoff_date
        ).delete()
        
        self.db.commit()
        
        logger.info(f"Deleted {deleted} old articles")
        return deleted
