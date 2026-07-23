import requests
from typing import Dict, List, Optional
from datetime import datetime
from app.config.settings import settings
from loguru import logger


class CommodityPriceAPICollector:
    """Collector for CommodityPriceAPI (commoditypriceapi.com)."""

    def __init__(self):
        self.api_key = settings.commodity_price_api_key
        self.base_url = settings.commodity_price_api_base_url
        self.enabled = bool(self.api_key and self.base_url)

        if not self.enabled:
            logger.warning(
                "CommodityPriceAPI key not configured. Set COMMODITY_PRICE_API_KEY to enable."
            )

    def _make_request(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """Make a request to the CommodityPriceAPI."""
        if not self.enabled:
            logger.error("CommodityPriceAPI is not configured")
            return None

        params = params or {}
        params["apiKey"] = self.api_key

        try:
            response = requests.get(
                f"{self.base_url}/{endpoint}",
                params=params,
                timeout=30
            )
            response.raise_for_status()
            data = response.json()

            if not data.get("success"):
                logger.error(
                    f"CommodityPriceAPI error for {endpoint}: {data.get('message', 'Unknown error')}"
                )
                return None

            return data

        except requests.exceptions.RequestException as e:
            logger.error(f"Error making request to CommodityPriceAPI: {e}")
            return None

    def get_latest_rates(self, symbols: List[str]) -> Optional[Dict[str, float]]:
        """Get latest rates for one or more commodity symbols.

        Returns a mapping of symbol -> rate.
        """
        if not symbols:
            return {}

        data = self._make_request("rates/latest", {"symbols": ",".join(symbols)})
        if not data or "rates" not in data:
            return None

        return data["rates"]

    def get_historical_rate(
        self, symbol: str, date: datetime
    ) -> Optional[Dict]:
        """Get historical OHLC for a single commodity on a given date."""
        date_str = date.strftime("%Y-%m-%d")
        data = self._make_request(
            "rates/historical",
            {"symbols": symbol, "date": date_str}
        )

        if not data or "rates" not in data:
            return None

        return data["rates"].get(symbol)

    def get_symbol_info(self) -> Optional[List[Dict]]:
        """Fetch the list of supported symbols and their metadata."""
        data = self._make_request("symbols")
        if not data:
            return None
        return data.get("symbols")
