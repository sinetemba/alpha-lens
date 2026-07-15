import requests
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from app.config.settings import settings
from loguru import logger


JSE_MIC_CODE = "XJSE"


class TwelveDataCollector:
    """Collector for Twelve Data API."""

    def __init__(self):
        self.api_key = settings.twelve_data_api_key
        self.base_url = settings.twelve_data_base_url
        
        if not self.api_key:
            logger.warning("Twelve Data API key not configured. Collector will not function.")
    
    def _make_request(self, endpoint: str, params: Dict) -> Optional[Dict]:
        """Make a request to the Twelve Data API."""
        if not self.api_key:
            logger.error("Twelve Data API key not configured")
            return None
        
        params["apikey"] = self.api_key
        
        try:
            response = requests.get(f"{self.base_url}/{endpoint}", params=params, timeout=30)
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
