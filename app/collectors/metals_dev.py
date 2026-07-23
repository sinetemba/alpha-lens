import requests
from typing import Dict, Optional
from app.config.settings import settings
from loguru import logger


class MetalsDevCollector:
    def __init__(self):
        self.api_key = settings.metals_dev_api_key
        self.base_url = settings.metals_dev_base_url
        self.currency = settings.metals_dev_currency
        self.unit = settings.metals_dev_unit
        self.enabled = bool(self.api_key and self.base_url)

        if not self.enabled:
            logger.warning("Metals.dev API key not configured. Set METALS_DEV_API_KEY to enable.")

    def get_latest_rates(self) -> Optional[Dict[str, float]]:
        if not self.enabled:
            logger.error("Metals.dev is not configured")
            return None

        try:
            response = requests.get(
                self.base_url,
                params={
                    "api_key": self.api_key,
                    "currency": self.currency,
                    "unit": self.unit,
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.exceptions.RequestException, ValueError) as e:
            logger.error(f"Error fetching latest Metals.dev prices: {e}")
            return None

        if data.get("status") != "success" or not isinstance(data.get("metals"), dict):
            logger.error("Metals.dev returned an invalid latest-prices response")
            return None

        return {
            symbol: float(price)
            for symbol, price in data["metals"].items()
            if isinstance(price, (int, float))
        }
