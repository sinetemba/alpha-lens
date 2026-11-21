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
                if not feed.entries:
                    logger.warning(f"Error parsing RSS feed from {url}: {feed.bozo_exception}")
                    self._last_error = f"unparseable feed: {feed.bozo_exception}"[:300]
                    return None
                # Recoverable (e.g. IOL declares us-ascii but serves UTF-8):
                # the entries parsed fine, so this isn't worth a warning per run.
                logger.debug(f"RSS feed {url} parsed with a recoverable error: {feed.bozo_exception}")

            articles = []
            undated = []
            for entry in feed.entries:
                published = entry.get("published")
                published_at = self._parse_date(published)
                if published and published_at is None:
                    undated.append(published)
                articles.append({
                    "title": entry.get("title", ""),
                    "summary": entry.get("summary", ""),
                    "url": entry.get("link", ""),
                    "published_at": published_at,
                    "source": feed.feed.get("title", url),
                })
            if undated:
                # One line per feed per run, not one per article.
                logger.warning(
                    f"{len(undated)}/{len(articles)} dates unparseable in {url} "
                    f"(e.g. {undated[0]!r}) - those articles are dated at fetch time"
                )
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
    
    # Non-RFC-822 formats seen in configured feeds. All are UTC: News24's
    # capi24 feed ('Friday Nov 20 2026 17:24:41') matches the article pages'
    # datePublished (+00:00) to the second - checked 2026-11-21.
    _FALLBACK_FORMATS = ("%A %b %d %Y %H:%M:%S",)

    def _parse_date(self, date_str: Optional[str]) -> Optional[datetime]:
        """Parse an RSS date to aware UTC; None if no known format matches
        (the caller reports unparseable dates once per feed)."""
        if not date_str:
            return None
        date_str = date_str.strip()
        try:
            parsed = parsedate_to_datetime(date_str)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError, IndexError):
            pass
        for fmt in self._FALLBACK_FORMATS:
            try:
                return datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
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
