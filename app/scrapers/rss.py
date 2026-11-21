import feedparser
import requests
from email.utils import parsedate_to_datetime
from typing import List, Dict, Optional
from datetime import datetime, timezone
from app.config.settings import settings
from loguru import logger


class RSSScraper:
    """Scraper for RSS news feeds."""
    
    def __init__(self):
        self.feeds = settings.rss_feeds
        # {url: error message or None} for the last fetch_all_feeds() run -
        # NewsScraper records them as source-health heartbeats.
        self.feed_results: Dict[str, Optional[str]] = {}
        self._last_error: Optional[str] = None
    
    def fetch_feed(self, url: str) -> Optional[List[Dict]]:
        """Fetch and parse an RSS feed."""
        self._last_error = None
        try:
            # feedparser.parse(url) uses urllib with no timeout - a hung feed
            # would block the whole update job, so fetch the bytes ourselves.
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            
            if feed.bozo:
                logger.warning(f"Error parsing RSS feed from {url}: {feed.bozo_exception}")
                if not feed.entries:
                    self._last_error = f"unparseable feed: {feed.bozo_exception}"[:300]
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
            self._last_error = f"{e.__class__.__name__}: {e}"[:300]
            return None
    
    def fetch_all_feeds(self) -> List[Dict]:
        """Fetch all configured RSS feeds."""
        all_articles = []
        self.feed_results = {}

        for feed_url in self.feeds:
            logger.info(f"Fetching RSS feed: {feed_url}")
            articles = self.fetch_feed(feed_url)
            self.feed_results[feed_url] = None if articles is not None else (self._last_error or "fetch failed")
            if articles:
                all_articles.extend(articles)
                logger.info(f"Fetched {len(articles)} articles from {feed_url}")
        
        return all_articles
    
    def _parse_date(self, date_str: Optional[str]) -> Optional[datetime]:
        """Parse various date formats from RSS feeds."""
        if not date_str:
            return None
        
        try:
            parsed = parsedate_to_datetime(date_str)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
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
