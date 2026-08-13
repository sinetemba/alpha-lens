from typing import List, Dict, Optional
from datetime import datetime, timezone
import requests
from app.collectors.yfinance import YFinanceCollector
from app.scrapers.rss import RSSScraper
from app.config.settings import settings
from loguru import logger


NEWSDATA_URL = "https://newsdata.io/api/1/news"


class CompanyNewsService:
    """Fetch and aggregate on-demand company news from free sources."""

    def __init__(self, limit: int = 10):
        self.limit = limit
        self.yahoo = YFinanceCollector()
        self.rss = RSSScraper()

    def get_company_news(self, symbol: str, company_name: Optional[str] = None) -> List[Dict]:
        """Get the most recent news for a company."""
        if not company_name:
            company_name = self._get_company_name(symbol)

        search_terms = self._build_search_terms(symbol, company_name)

        articles = []
        articles.extend(self._fetch_yahoo(symbol, search_terms))
        articles.extend(self._fetch_rss(search_terms))

        if getattr(settings, "newsdata_api_key", None):
            articles.extend(self._fetch_newsdata(search_terms))

        articles = self._dedupe_and_sort(articles)
        return articles[: self.limit]

    def _get_company_name(self, symbol: str) -> str:
        try:
            info = self.yahoo.get_info(symbol)
            if info:
                return info.get("longName") or info.get("shortName") or symbol
        except Exception as e:
            logger.warning(f"Could not resolve company name for {symbol}: {e}")
        return symbol

    def _build_search_terms(self, symbol: str, company_name: str) -> List[str]:
        """Build an ordered list of lower-case search terms from symbol and company name."""
        terms = [company_name.lower(), symbol.lower()]

        short = company_name
        for suffix in [
            "Limited", "Ltd", "PLC", "Inc", "Incorporated",
            "Corporation", "Corp", "Holdings", "Holding", "Group", "N.V.", "AG", "SE",
        ]:
            short = short.replace(f" {suffix}", "").replace(f" {suffix.lower()}", "")

        short = short.strip()
        if short and short.lower() not in terms:
            terms.append(short.lower())
            first_word = short.split()[0]
            if first_word and first_word.lower() not in terms:
                terms.append(first_word.lower())

        return [t for t in terms if len(t) > 1]

    def _is_relevant(self, text: str, search_terms: List[str]) -> bool:
        text_lower = text.lower()
        return any(term in text_lower for term in search_terms)

    def _fetch_yahoo(self, symbol: str, search_terms: List[str]) -> List[Dict]:
        try:
            raw = self.yahoo.get_news(symbol)
            articles = []
            for article in raw:
                text = f"{article.get('title', '')} {article.get('summary', '')}"
                if not self._is_relevant(text, search_terms):
                    continue
                articles.append(article)
            return articles
        except Exception as e:
            logger.error(f"Error fetching Yahoo news for {symbol}: {e}")
            return []

    def _fetch_rss(self, search_terms: List[str]) -> List[Dict]:
        try:
            raw = self.rss.fetch_all_feeds()
            articles = []
            for article in raw:
                text = f"{article.get('title', '')} {article.get('summary', '')}"
                if not self._is_relevant(text, search_terms):
                    continue
                articles.append(article)
            return articles
        except Exception as e:
            logger.error(f"Error fetching RSS news: {e}")
            return []

    def _fetch_newsdata(self, search_terms: List[str]) -> List[Dict]:
        """Search NewsData.io using the most specific term (full company name)."""
        api_key = settings.newsdata_api_key
        if not api_key:
            return []

        query = search_terms[0] if search_terms else ""
        if not query:
            return []

        try:
            params = {
                "apikey": api_key,
                "q": query,
                "language": "en",
            }
            response = requests.get(NEWSDATA_URL, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            articles = []
            for result in data.get("results", []):
                title = result.get("title", "")
                link = result.get("link", "")
                if not title or not link:
                    continue

                text = f"{title} {result.get('description', '')}"
                if not self._is_relevant(text, search_terms):
                    continue

                published_at = None
                pub = result.get("pubDate") or result.get("pubDateTZ")
                if pub:
                    try:
                        normalized = pub.replace(" ", "T").replace("Z", "+00:00")
                        published_at = datetime.fromisoformat(normalized)
                    except (ValueError, TypeError):
                        pass

                articles.append({
                    "title": title,
                    "summary": result.get("description", ""),
                    "url": link,
                    "source": result.get("source_id", "NewsData.io"),
                    "published_at": published_at,
                })
            return articles
        except Exception as e:
            logger.error(f"Error fetching NewsData.io news: {e}")
            return []

    def _to_utc(self, dt: Optional[datetime]) -> datetime:
        if not dt:
            return datetime.min.replace(tzinfo=timezone.utc)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _dedupe_and_sort(self, articles: List[Dict]) -> List[Dict]:
        seen = set()
        unique = []
        for article in articles:
            url = article.get("url", "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            unique.append(article)

        unique.sort(
            key=lambda a: self._to_utc(a.get("published_at")),
            reverse=True,
        )
        return unique
