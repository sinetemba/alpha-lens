import time
from typing import Optional

import requests
from loguru import logger


class CoinGeckoCollector:
    """Thin adapter around the CoinGecko free API (no key required).

    The public tier is rate-limited (~30 calls/min shared), so callers
    should cache responses rather than polling on every render.
    """

    BASE_URL = "https://api.coingecko.com/api/v3"

    def _call_with_retry(self, label: str, fn, max_retries: int = 2, base_delay: float = 0.5):
        for attempt in range(max_retries + 1):
            try:
                return fn()
            except Exception as e:
                if attempt < max_retries:
                    time.sleep(base_delay * (2 ** attempt))
                else:
                    logger.error(f"{label} failed after {max_retries + 1} attempts: {e}")
                    return None

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
