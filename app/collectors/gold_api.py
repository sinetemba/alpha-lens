import time

import requests
from loguru import logger

from app.config.settings import settings

# After a failure GoldAPI is skipped for a while instead of being retried on
# every Streamlit rerun: a 403 (bad key / 100-requests-a-month quota used up)
# made the Krugerrands page fire ~14 sequential requests per click (18s).
_AUTH_OR_QUOTA_COOLDOWN = 6 * 3600   # 401/403/429 won't fix themselves soon
_OTHER_COOLDOWN = 10 * 60


class GoldAPICollector:
    """Thin adapter around GoldAPI.io's precious metals prices.

    Optional since 2026-11-22 - live spot and year-end prices come from Yahoo
    (services/metal_prices.py); GoldAPI is only a fallback.
    """

    # Process-wide: every instance (page reruns create new ones) shares it.
    _blocked_until: float = 0.0
    last_error: str | None = None

    def __init__(self):
        self.api_key = settings.gold_api_key
        self.base_url = settings.gold_api_base_url.rstrip("/")
        self.enabled = bool(self.api_key)

    @classmethod
    def blocked(cls) -> bool:
        return time.monotonic() < cls._blocked_until

    @classmethod
    def _trip(cls, error: Exception) -> None:
        status = getattr(getattr(error, "response", None), "status_code", None)
        cooldown = _AUTH_OR_QUOTA_COOLDOWN if status in (401, 403, 429) else _OTHER_COOLDOWN
        cls._blocked_until = time.monotonic() + cooldown
        cls.last_error = (f"HTTP {status}" if status else error.__class__.__name__) + (
            " - invalid key or monthly quota used up" if status in (401, 403, 429) else "")
        logger.warning(f"GoldAPI failed ({cls.last_error}); skipping it for {cooldown // 60} min")

    def _get(self, path: str) -> dict | None:
        if not self.enabled or self.blocked():
            return None
        try:
            response = requests.get(f"{self.base_url}/api/{path}",
                                    headers={"x-access-token": self.api_key}, timeout=15)
            response.raise_for_status()
            self.__class__.last_error = None
            return response.json()
        except Exception as e:
            # Never log the response/URL beyond the path: the key is a header,
            # but keep it out of logs regardless.
            self._trip(e)
            return None

    def get_historical_price(self, metal: str, currency: str, date: str) -> dict | None:
        """Fetch the spot price for a specific date (YYYYMMDD) from GoldAPI."""
        return self._get(f"{metal}/{currency}/{date}")

    def get_live_price(self, metal: str = "XAU", currency: str = "ZAR") -> dict | None:
        """Fetch the latest spot price for a metal/currency pair.

        Returns the raw GoldAPI JSON, e.g. {"metal": "XAU", "currency": "ZAR",
        "price": 2391.72, "prev_close_price": 2380.12, ...}.
        """
        return self._get(f"{metal}/{currency}")
