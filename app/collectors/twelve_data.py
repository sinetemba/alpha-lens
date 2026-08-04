import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Deque, List, Dict, Optional

import requests
from app.config.settings import settings
from loguru import logger


JSE_MIC_CODE = "XJSE"

# Twelve Data's "basic" (free) plan caps requests both per-minute and per-day.
# These are self-imposed defaults matching that plan; the per-minute figure is
# refined at runtime from the API's own 429 message in case the account differs.
DEFAULT_REQUESTS_PER_MINUTE = 8
DEFAULT_REQUESTS_PER_DAY = 800

_MINUTE_LIMIT_RE = re.compile(r"current limit being (\d+)")


class TwelveDataCollector:
    """Collector for Twelve Data API."""

    # Shared across every instance (and the scheduler's background thread), since
    # they all draw against the same API key's quota - state must be class-level,
    # not per-instance, or callers spun up per-request (like DataService) would
    # each start throttling from zero.
    _lock = threading.Lock()
    _request_times: Deque[float] = deque()
    _minute_limit = DEFAULT_REQUESTS_PER_MINUTE
    _daily_limit = DEFAULT_REQUESTS_PER_DAY
    _daily_count = 0
    _daily_count_date = None
    _quota_exhausted_logged = False

    def __init__(self):
        self.api_key = settings.twelve_data_api_key
        self.base_url = settings.twelve_data_base_url

        if not self.api_key:
            logger.warning("Twelve Data API key not configured. Collector will not function.")

    @classmethod
    def _reset_daily_count_if_new_day(cls) -> None:
        today = datetime.now(timezone.utc).date()
        if cls._daily_count_date != today:
            cls._daily_count_date = today
            cls._daily_count = 0
            cls._quota_exhausted_logged = False

    @classmethod
    def quota_exceeded_message(cls) -> Optional[str]:
        """User-facing message if today's Twelve Data quota is exhausted, else None."""
        with cls._lock:
            cls._reset_daily_count_if_new_day()
            if cls._daily_count >= cls._daily_limit:
                return (
                    f"Twelve Data daily limit reached ({cls._daily_count}/{cls._daily_limit} requests) - "
                    "using Yahoo Finance only until it resets tomorrow."
                )
        return None

    @classmethod
    def _throttle(cls) -> bool:
        """Pace requests to stay under the per-minute cap, sleeping if needed.

        Returns False without sleeping or making a request if today's daily cap
        is already exhausted - the caller should skip the request entirely.
        """
        with cls._lock:
            cls._reset_daily_count_if_new_day()
            if cls._daily_count >= cls._daily_limit:
                return False

            now = time.monotonic()
            while cls._request_times and now - cls._request_times[0] >= 60:
                cls._request_times.popleft()

            if len(cls._request_times) >= cls._minute_limit:
                wait = 60 - (now - cls._request_times[0]) + 0.1
                if wait > 0:
                    logger.info(f"Twelve Data per-minute limit reached, waiting {wait:.1f}s")
                    time.sleep(wait)
                now = time.monotonic()
                while cls._request_times and now - cls._request_times[0] >= 60:
                    cls._request_times.popleft()

            cls._request_times.append(time.monotonic())
            cls._daily_count += 1
            return True

    @classmethod
    def _handle_rate_limited(cls, response: requests.Response) -> None:
        """Called on an HTTP 429 despite our own throttling - this means the shared
        key's quota was already consumed elsewhere (a prior process run today, or
        concurrent scheduler/dashboard activity). Adjusts tracked limits from the
        API's own message so subsequent throttling matches reality.
        """
        try:
            message = response.json().get("message", "")
        except ValueError:
            message = ""

        message_lower = message.lower()
        with cls._lock:
            if "minute" in message_lower:
                match = _MINUTE_LIMIT_RE.search(message)
                if match:
                    cls._minute_limit = int(match.group(1))
            elif "day" in message_lower:
                cls._daily_count = cls._daily_limit

        logger.warning(f"Twelve Data rate limit hit: {message or 'HTTP 429'}")

    def _make_request(self, endpoint: str, params: Dict) -> Optional[Dict]:
        """Make a request to the Twelve Data API."""
        if not self.api_key:
            logger.error("Twelve Data API key not configured")
            return None

        if not self._throttle():
            if not self.__class__._quota_exhausted_logged:
                logger.warning(
                    f"Twelve Data daily quota ({self._daily_limit} requests) reached - "
                    "skipping remaining Twelve Data calls until tomorrow."
                )
                self.__class__._quota_exhausted_logged = True
            return None

        params["apikey"] = self.api_key

        try:
            response = requests.get(f"{self.base_url}/{endpoint}", params=params, timeout=30)

            # Twelve Data returns plan-gated symbols as a plain HTTP 404, identical to an
            # unknown/delisted symbol, unless the body is inspected before raise_for_status()
            # discards it - so this is checked separately rather than treated as "not found".
            if response.status_code == 404:
                try:
                    error_data = response.json()
                except ValueError:
                    error_data = {}
                message = error_data.get("message", "")
                if "plan" in message.lower():
                    logger.warning(
                        f"Twelve Data symbol '{params.get('symbol')}' requires a higher-tier plan: {message}"
                    )
                    return None

            if response.status_code == 429:
                self._handle_rate_limited(response)
                return None

            response.raise_for_status()
            data = response.json()

            # Check for API errors
            if "status" in data and data["status"] == "error":
                logger.error(f"Twelve Data API error: {data.get('message', 'Unknown error')}")
                return None

            return data

        except requests.exceptions.RequestException as e:
            logger.error(f"Error making request to Twelve Data API: {e}")
            return None
    
    def get_price(self, symbol: str) -> Optional[Dict]:
        """Get current price for a symbol."""
        params = {
            "symbol": symbol,
            "outputsize": "1"
        }
        data = self._make_request("price", params)
        return data
    
    def get_quote(self, symbol: str) -> Optional[Dict]:
        """Get detailed quote for a symbol."""
        params = {
            "symbol": symbol
        }
        data = self._make_request("quote", params)

        if data and data.get("mic_code") != JSE_MIC_CODE:
            # Twelve Data's free plan doesn't support exchange-scoped queries, so a
            # bare symbol (e.g. "RNG") can silently resolve to an unrelated company
            # on another exchange (e.g. RingCentral on NYSE) instead of the JSE stock.
            logger.warning(
                f"Twelve Data quote for '{symbol}' resolved to {data.get('exchange')} "
                f"({data.get('mic_code')}), not JSE - discarding."
            )
            return None

        return data
    
    def get_exchange_rate(self, from_currency: str, to_currency: str) -> Optional[Dict]:
        """Get exchange rate between two currencies."""
        params = {
            "symbol": f"{from_currency}/{to_currency}"
        }
        data = self._make_request("exchange_rate", params)
        return data
    
    def get_historical_data(
        self, 
        symbol: str, 
        interval: str = "1day",
        outputsize: int = 100,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> Optional[List[Dict]]:
        """Get historical price data for a symbol.
        
        Args:
            symbol: Stock symbol
            interval: Time interval (1min, 5min, 15min, 30min, 1h, 4h, 1day, 1week, 1month)
            outputsize: Number of data points to return
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
        """
        params = {
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize
        }
        
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        
        data = self._make_request("time_series", params)

        if data and "values" in data:
            mic_code = data.get("meta", {}).get("mic_code")
            if mic_code != JSE_MIC_CODE:
                logger.warning(
                    f"Twelve Data historical data for '{symbol}' resolved to "
                    f"{data.get('meta', {}).get('exchange')} ({mic_code}), not JSE - discarding."
                )
                return None
            return data["values"]

        return None
    
    def get_technical_indicators(
        self, 
        symbol: str, 
        indicator: str, 
        interval: str = "1day"
    ) -> Optional[Dict]:
        """Get technical indicators for a symbol.
        
        Supported indicators: sma, ema, rsi, macd, bbands, adx, cci, stoch, etc.
        """
        params = {
            "symbol": symbol,
            "indicator": indicator,
            "interval": interval
        }
        data = self._make_request("technical_indicators", params)
        
        if data and "values" in data:
            return data["values"]
        
        return None
    
    def get_company_info(self, symbol: str) -> Optional[Dict]:
        """Get company information for a symbol."""
        params = {
            "symbol": symbol
        }
        data = self._make_request("profile", params)
        return data
    
    def get_symbols(self, country: str = "South Africa") -> Optional[List[Dict]]:
        """Get list of available symbols for a country."""
        params = {
            "country": country
        }
        data = self._make_request("symbol_search", params)
        
        if data and "data" in data:
            return data["data"]
        
        return None
    
    def get_dividends(self, symbol: str) -> Optional[List[Dict]]:
        """Get dividend history (including payment dates) for a symbol.

        Unlike Yahoo Finance, Twelve Data's /dividends endpoint includes
        ex_date, payment_date, and record_date fields where available.
        """
        params = {
            "symbol": symbol,
        }
        data = self._make_request("dividends", params)

        if data and "dividends" in data:
            return data["dividends"]

        return None

    def get_multiple_quotes(self, symbols: List[str]) -> Optional[Dict]:
        """Get quotes for multiple symbols at once."""
        if len(symbols) > 10:
            logger.warning("Twelve Data API supports maximum 10 symbols per request")
            symbols = symbols[:10]
        
        params = {
            "symbol": ",".join(symbols)
        }
        data = self._make_request("quote", params)
        return data
