import requests
from app.config.settings import settings
from loguru import logger


class GoldAPICollector:
    """Thin adapter around GoldAPI.io's precious metals prices."""

    def __init__(self):
        self.api_key = settings.gold_api_key
        self.base_url = settings.gold_api_base_url.rstrip("/")
        self.enabled = bool(self.api_key)

    def get_historical_price(self, metal: str, currency: str, date: str) -> dict | None:
        """Fetch the spot price for a specific date (YYYYMMDD) from GoldAPI."""
        if not self.enabled:
            return None

        url = f"{self.base_url}/api/{metal}/{currency}/{date}"
        headers = {"x-access-token": self.api_key}

        try:
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"GoldAPI historical request failed for {metal}/{currency}/{date}: {e}")
            return None

    def get_live_price(self, metal: str = "XAU", currency: str = "ZAR") -> dict | None:
        """Fetch the latest spot price for a metal/currency pair.

        Returns the raw GoldAPI JSON, e.g.:
            {
                "timestamp": 1716000000,
                "metal": "XAU",
                "currency": "ZAR",
                "price": 2391.72,
                "prev_close_price": 2380.12,
                "open_price": 2382.44,
                "low_price": 2374.91,
                "high_price": 2395.63,
                "ch": 11.6,
                "chp": 0.49,
            }
        """
        if not self.enabled:
            return None

        url = f"{self.base_url}/api/{metal}/{currency}"
        headers = {"x-access-token": self.api_key}

        try:
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"GoldAPI request failed for {metal}/{currency}: {e}")
            return None
