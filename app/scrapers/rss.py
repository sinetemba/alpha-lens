import feedparser
from typing import List, Dict, Optional
from datetime import datetime
from app.config.settings import settings
from loguru import logger


class RSSScraper:
    """Scraper for RSS news feeds."""
    
    def __init__(self):
        self.feeds = settings.rss_feeds
    
    def fetch_feed(self, url: str) -> Optional[List[Dict]]:
        """Fetch and parse an RSS feed."""
        try:
            feed = feedparser.parse(url)
            
            if feed.bozo:
                logger.warning(f"Error parsing RSS feed from {url}: {feed.bozo_exception}")
                return None
            
            articles = []
            
            for entry in feed.entries:
                article = {
                    "title": entry.get("title", ""),
                    "summary": entry.get("summary", ""),
                    "url": entry.get("link", ""),
                    "published_at": self._parse_date(entry.get("published")),
                    "source": feed.feed.get("title", url),
                }
                articles.append(article)
            
            return articles
        
        except Exception as e:
            logger.error(f"Error fetching RSS feed from {url}: {e}")
            return None
    
    def fetch_all_feeds(self) -> List[Dict]:
        """Fetch all configured RSS feeds."""
        all_articles = []
        
        for feed_url in self.feeds:
            logger.info(f"Fetching RSS feed: {feed_url}")
            articles = self.fetch_feed(feed_url)
            if articles:
                all_articles.extend(articles)
                logger.info(f"Fetched {len(articles)} articles from {feed_url}")
        
        return all_articles
    
    def _parse_date(self, date_str: Optional[str]) -> Optional[datetime]:
        """Parse various date formats from RSS feeds."""
        if not date_str:
            return None
        
        try:
            # feedparser usually parses dates automatically
            # but we can try to parse as string if needed
            parsed = feedparser._parse_date(date_str)
            if parsed:
                return datetime(*parsed[:6])
        except Exception as e:
            logger.warning(f"Error parsing date '{date_str}': {e}")
        
        return None
    
    def fetch_moneyweb(self) -> List[Dict]:
        """Fetch Moneyweb RSS feed."""
        return self.fetch_feed(settings.rss_moneyweb) or []
    
    def fetch_businesstech(self) -> List[Dict]:
        """Fetch BusinessTech RSS feed."""
        return self.fetch_feed(settings.rss_businesstech) or []
    
    def fetch_news24_business(self) -> List[Dict]:
        """Fetch News24 Business RSS feed."""
        return self.fetch_feed(settings.rss_news24_business) or []
    
    def fetch_daily_investor(self) -> List[Dict]:
        """Fetch Daily Investor RSS feed."""
        return self.fetch_feed(settings.rss_daily_investor) or []
