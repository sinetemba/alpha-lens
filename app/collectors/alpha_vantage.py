from datetime import datetime
from time import monotonic, sleep
from typing import Dict, List, Optional

import requests
from loguru import logger

from app.config.settings import settings


class AlphaVantageCollector:
    def __init__(self):
        self.api_key = settings.alpha_vantage_api_key
        self.base_url = settings.alpha_vantage_base_url
        self.enabled = bool(self.api_key and self.base_url)
        self._last_request_at = 0.0

        if not self.enabled:
            logger.warning("Alpha Vantage API key not configured. Set ALPHA_VANTAGE_API_KEY to enable.")

    def get_commodity_history(self, commodity: str) -> Optional[List[Dict]]:
        if not self.enabled:
            logger.error("Alpha Vantage is not configured")
            return None

        if commodity in {"gold", "silver"}:
            params = {
                "function": "GOLD_SILVER_HISTORY",
                "symbol": commodity.upper(),
                "interval": "daily",
                "apikey": self.api_key,
            }
        elif commodity == "copper":
            params = {
                "function": "COPPER",
                "interval": "daily",
                "apikey": self.api_key,
            }
        else:
            logger.warning(f"Alpha Vantage does not provide configured history for {commodity}")
            return None

        elapsed = monotonic() - self._last_request_at
        if elapsed < 1.1:
            sleep(1.1 - elapsed)

        try:
            response = requests.get(self.base_url, params=params, timeout=30)
            self._last_request_at = monotonic()
            response.raise_for_status()
            data = response.json()
        except (requests.exceptions.RequestException, ValueError) as e:
            logger.error(f"Error fetching Alpha Vantage history for {commodity}: {e}")
            return None

        if "Information" in data or "Note" in data or "Error Message" in data:
            logger.error(f"Alpha Vantage error for {commodity}: {data.get('Information') or data.get('Note') or data.get('Error Message')}")
            return None

        points = data.get("data")
        if not isinstance(points, list):
            logger.error(f"Alpha Vantage returned an invalid history response for {commodity}")
            return None

        parsed_points = []
        for point in points:
            try:
                parsed_points.append({
                    "timestamp": datetime.strptime(point["date"], "%Y-%m-%d"),
                    "close": float(point.get("price", point.get("value"))),
                })
            except (KeyError, TypeError, ValueError):
                continue

        return parsed_points
