from typing import List, Dict, Optional
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from app.models.news import NewsArticle
from app.models.stock import Stock
from app.scrapers.rss import RSSScraper
from loguru import logger
import re


def _utcnow_naive() -> datetime:
    """Current UTC time as a naive datetime, matching the naive DateTime columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Frequently-mentioned JSE tickers, used to attribute articles to stocks even
# before the symbol exists in the DB. DB symbols (watchlist/portfolio) are
# merged in at runtime.
_COMMON_JSE_SYMBOLS = {
    "NPN", "CFR", "AGL", "SOL", "SBK", "MTN", "VOD", "ABG", "BID", "RCL",
    "FSR", "REM", "TSG", "GRT", "WHL", "TRU", "SHP", "MNP", "OML", "IMP",
    "AMS", "GLN", "BVT", "NED", "SBG", "DSY", "RMI", "VKE", "CPI", "TBS",
}


class NewsScraper:
    """News scraper that aggregates and processes news articles."""

    def __init__(self, db: Session):
        self.db = db
        self.rss_scraper = RSSScraper()
        self._known_symbols: Optional[set] = None

    def scrape_all_sources(self) -> List[NewsArticle]:
        """Scrape news from all configured sources."""
        logger.info("Starting news scraping from all sources")

        all_articles_data = self.rss_scraper.fetch_all_feeds()

        # One query for every URL in the batch replaces a SELECT per article.
        # `seen` also dedups inside the batch: the session runs with
        # autoflush off, so a repeated URL (same story on two feeds) would
        # otherwise pass the check and trip the unique index at commit.
        urls = {a["url"] for a in all_articles_data if a.get("url")}
        seen = (
            {row[0] for row in self.db.query(NewsArticle.url).filter(
                NewsArticle.url.in_(urls)
            ).all()}
            if urls else set()
        )

        articles = []
        for article_data in all_articles_data:
            article = self._build_article(article_data, seen)
            if article:
                seen.add(article_data["url"])
                articles.append(article)

        saved_articles = self._commit_articles(articles) if articles else []
        logger.info(f"Scraped and saved {len(saved_articles)} new articles")
        return saved_articles

    def _commit_articles(self, articles: List[NewsArticle]) -> List[NewsArticle]:
        """Commit the batch; on failure fall back to per-article commits.

        One bad row must not discard the batch: a rollback-after-queue used
        to drop every earlier article while they still counted as saved.
        """
        try:
            self.db.add_all(articles)
            self.db.commit()
            return articles
        except Exception as e:
            logger.error(f"Batch article commit failed ({e}); retrying per-article")
            self.db.rollback()

        saved = []
        for article in articles:
            try:
                self.db.add(article)
                self.db.commit()
                saved.append(article)
            except Exception as e:
                self.db.rollback()
                logger.warning(f"Could not save article {article.url!r}: {e}")
        return saved

    def _build_article(
        self, article_data: Dict, existing_urls: set
    ) -> Optional[NewsArticle]:
        """Construct a NewsArticle, or None when the URL is already known.

        Never touches the session - a construction error must not roll back
        work queued by this or earlier runs.
        """
        try:
            if article_data["url"] in existing_urls:
                return None

            # Extract stock symbol from title if possible
            stock_symbol = self._extract_stock_symbol(article_data["title"])

            # Normalise to naive UTC - the DateTime columns are naive, and on
            # SQLite an aware value stores the "+00:00" suffix, breaking
            # ordering/comparison against naive rows.
            published_at = article_data["published_at"] or _utcnow_naive()
            if published_at.tzinfo is not None:
                published_at = published_at.astimezone(timezone.utc).replace(tzinfo=None)

            return NewsArticle(
                title=article_data["title"],
                summary=article_data["summary"],
                url=article_data["url"],
                source=article_data["source"],
                published_at=published_at,
                stock_symbol=stock_symbol,
            )

        except Exception as e:
            logger.error(f"Error building article {article_data.get('url')!r}: {e}")
            return None

    def _extract_stock_symbol(self, text: str) -> Optional[str]:
        """Extract a JSE stock symbol from text (whole-word match only).

        Word-boundary matching is required: a plain substring check attributes
        "REM" inside "REMARK" or "BID" inside "BIDEN" to those stocks.
        """
        if self._known_symbols is None:
            db_symbols = {row[0] for row in self.db.query(Stock.symbol).all()}
            self._known_symbols = db_symbols | _COMMON_JSE_SYMBOLS

        text_upper = text.upper()

        for symbol in self._known_symbols:
            # Matches "Naspers (NPN)" and "NPN.JO" but not "SOLar" or "BIDEN".
            if re.search(rf"\b{re.escape(symbol)}\b", text_upper):
                return symbol

        return None

    def get_recent_news(self, days: int = 7, symbol: Optional[str] = None) -> List[NewsArticle]:
        """Get recent news articles."""
        query = self.db.query(NewsArticle).filter(
            NewsArticle.published_at >= _utcnow_naive() - timedelta(days=days)
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
        cutoff_date = _utcnow_naive() - timedelta(days=days)
        
        deleted = self.db.query(NewsArticle).filter(
            NewsArticle.published_at < cutoff_date
        ).delete()
        
        self.db.commit()
        
        logger.info(f"Deleted {deleted} old articles")
        return deleted
