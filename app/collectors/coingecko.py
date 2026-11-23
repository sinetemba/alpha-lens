import time

import requests
from loguru import logger


class CoinGeckoCollector:
    """Thin adapter around the CoinGecko free API (no key required).

    The public tier is rate-limited (~30 calls/min shared), so callers
    should cache responses rather than polling on every render.
    """

    BASE_URL = "https://api.coingecko.com/api/v3"

    def _call_with_retry(self, label: str, fn, max_retries: int = 2, base_delay: float = 0.5):
        """Retry transient failures only - non-429 client errors are final.

        429s get a longer wait honouring Retry-After when the API provides
        one; other 4xx (bad ids etc.) fail immediately.
        """
        for attempt in range(max_retries + 1):
            try:
                return fn()
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                if status and status < 500 and status != 429:
                    logger.error(f"{label} failed with HTTP {status} (not retrying)")
                    return None
                if attempt < max_retries:
                    time.sleep(self._retry_after(e) or base_delay * (2 ** attempt))
                else:
                    logger.error(f"{label} failed after {max_retries + 1} attempts: HTTP {status}")
                    return None
            except Exception as e:
                if attempt < max_retries:
                    time.sleep(base_delay * (2 ** attempt))
                else:
                    logger.error(f"{label} failed after {max_retries + 1} attempts: {e}")
                    return None

    @staticmethod
    def _retry_after(e: requests.HTTPError) -> float:
        """Server's Retry-After hint in seconds, 0 when absent/unparseable."""
        if e.response is None:
            return 0.0
        try:
            return max(float(e.response.headers.get("Retry-After") or 0), 0.0)
        except ValueError:
            return 0.0

    def get_markets(self, coin_ids: list) -> dict:
        """ZAR market data for the given CoinGecko coin ids.

        Returns {coin_id: market_row} where each row carries at least
        ``current_price`` (ZAR) and ``price_change_percentage_24h``.
        """
        ids = ",".join(sorted({c for c in coin_ids if c}))
        if not ids:
            return {}

        def _fetch():
            response = requests.get(
                f"{self.BASE_URL}/coins/markets",
                params={
                    "vs_currency": "zar",
                    "ids": ids,
                    "price_change_percentage": "24h",
                },
                timeout=15,
            )
            response.raise_for_status()
            return response.json()

        data = self._call_with_retry("CoinGecko markets", _fetch)
        return {coin["id"]: coin for coin in data} if isinstance(data, list) else {}

    def search(self, query: str) -> list:
        """Resolve a name/symbol to CoinGecko coin ids.

        Returns a list of {"id", "symbol", "name"} dicts (max 10).
        """
        query = (query or "").strip()
        if not query:
            return []

        def _fetch():
            response = requests.get(
                f"{self.BASE_URL}/search",
                params={"query": query},
                timeout=15,
            )
            response.raise_for_status()
            return response.json()

        data = self._call_with_retry("CoinGecko search", _fetch) or {}
        return [
            {"id": c.get("id"), "symbol": (c.get("symbol") or "").upper(), "name": c.get("name")}
            for c in (data.get("coins") or [])[:10]
            if c.get("id")
        ]
