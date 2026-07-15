import yfinance as yf
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from app.config.settings import settings
from loguru import logger


# Yahoo Finance reports JSE-listed prices in South African cents (ZAc), not Rand,
# so OHLC values for ".JO"-suffixed tickers must be divided by 100.
JSE_PRICE_SCALE = 100
_OHLC_COLUMNS = ["Open", "High", "Low", "Close", "Adj Close"]


class YFinanceCollector:
    """Collector for Yahoo Finance data using yfinance library."""

    def __init__(self):
        self.enabled = settings.yfinance_enabled

    def get_ticker(self, symbol: str) -> Optional[yf.Ticker]:
        """Get a Ticker object for a symbol."""
        if not self.enabled:
            logger.warning("Yahoo Finance collector is disabled")
            return None

        try:
            # JSE-listed stocks are stored without an exchange suffix, but
            # Yahoo Finance requires the ".JO" suffix to resolve them.
            yf_symbol = symbol if "." in symbol else f"{symbol}.JO"
            ticker = yf.Ticker(yf_symbol)
            return ticker
        except Exception as e:
            logger.error(f"Error creating ticker for {symbol}: {e}")
            return None

    @staticmethod
    def _to_rand(hist, symbol: str):
        """Convert JSE OHLC prices from cents to Rand in-place."""
        if hist is None or hist.empty or "." in symbol:
            return hist

        for column in _OHLC_COLUMNS:
            if column in hist.columns:
                hist[column] = hist[column] / JSE_PRICE_SCALE

        return hist
    
    def get_info(self, symbol: str) -> Optional[Dict]:
        """Get company information for a symbol."""
        ticker = self.get_ticker(symbol)
        if not ticker:
            return None
        
        try:
            info = ticker.info
            return info
        except Exception as e:
            logger.error(f"Error getting info for {symbol}: {e}")
            return None
    
    def get_history(
        self, 
        symbol: str, 
        period: str = "1y",
        interval: str = "1d"
    ) -> Optional[Dict]:
        """Get historical price data for a symbol.
        
        Args:
            symbol: Stock symbol
            period: Time period (1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max)
            interval: Data interval (1m, 2m, 5m, 15m, 30m, 60m, 90m, 1h, 1d, 5d, 1wk, 1mo, 3mo)
        """
        ticker = self.get_ticker(symbol)
        if not ticker:
            return None
        
        try:
            hist = ticker.history(period=period, interval=interval)
            return self._to_rand(hist, symbol)
        except Exception as e:
            logger.error(f"Error getting history for {symbol}: {e}")
            return None

    def get_history_date_range(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        interval: str = "1d"
    ) -> Optional[Dict]:
        """Get historical data for a specific date range.
        
        Args:
            symbol: Stock symbol
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
            interval: Data interval
        """
        ticker = self.get_ticker(symbol)
        if not ticker:
            return None
        
        try:
            hist = ticker.history(start=start_date, end=end_date, interval=interval)
            return self._to_rand(hist, symbol)
        except Exception as e:
            logger.error(f"Error getting history for {symbol}: {e}")
            return None
    
    def get_dividends(self, symbol: str) -> Optional[List]:
        """Get dividend history for a symbol."""
        ticker = self.get_ticker(symbol)
        if not ticker:
            return None
        
        try:
            dividends = ticker.dividends
            return dividends
        except Exception as e:
            logger.error(f"Error getting dividends for {symbol}: {e}")
            return None
    
    def get_splits(self, symbol: str) -> Optional[List]:
        """Get stock split history for a symbol."""
        ticker = self.get_ticker(symbol)
        if not ticker:
            return None
        
        try:
            splits = ticker.splits
            return splits
        except Exception as e:
            logger.error(f"Error getting splits for {symbol}: {e}")
            return None
    
    def get_actions(self, symbol: str) -> Optional[List]:
        """Get all corporate actions (dividends, splits) for a symbol."""
        ticker = self.get_ticker(symbol)
        if not ticker:
            return None
        
        try:
            actions = ticker.actions
            return actions
        except Exception as e:
            logger.error(f"Error getting actions for {symbol}: {e}")
            return None
    
    def get_financials(self, symbol: str) -> Optional[Dict]:
        """Get financial statements for a symbol."""
        ticker = self.get_ticker(symbol)
        if not ticker:
            return None
        
        try:
            financials = {
                "income_statement": ticker.income_stmt,
                "balance_sheet": ticker.balance_sheet,
                "cash_flow": ticker.cashflow,
            }
            return financials
        except Exception as e:
            logger.error(f"Error getting financials for {symbol}: {e}")
            return None
    
    def get_major_holders(self, symbol: str) -> Optional[Dict]:
        """Get major holders information for a symbol."""
        ticker = self.get_ticker(symbol)
        if not ticker:
            return None
        
        try:
            major_holders = ticker.major_holders
            institutional_holders = ticker.institutional_holders
            return {
                "major_holders": major_holders,
                "institutional_holders": institutional_holders,
            }
        except Exception as e:
            logger.error(f"Error getting holders for {symbol}: {e}")
            return None
    
    def get_recommendations(self, symbol: str) -> Optional[Dict]:
        """Get analyst recommendations for a symbol."""
        ticker = self.get_ticker(symbol)
        if not ticker:
            return None
        
        try:
            recommendations = ticker.recommendations
            return recommendations
        except Exception as e:
            logger.error(f"Error getting recommendations for {symbol}: {e}")
            return None
    
    def get_calendar(self, symbol: str) -> Optional[Dict]:
        """Get calendar events for a symbol."""
        ticker = self.get_ticker(symbol)
        if not ticker:
            return None
        
        try:
            calendar = ticker.calendar
            return calendar
        except Exception as e:
            logger.error(f"Error getting calendar for {symbol}: {e}")
            return None
    
    def get_earnings(self, symbol: str) -> Optional[Dict]:
        """Get earnings information for a symbol."""
        ticker = self.get_ticker(symbol)
        if not ticker:
            return None
        
        try:
            earnings = ticker.earnings
            quarterly_earnings = ticker.quarterly_earnings
            return {
                "annual": earnings,
                "quarterly": quarterly_earnings,
            }
        except Exception as e:
            logger.error(f"Error getting earnings for {symbol}: {e}")
            return None
    
    def search_symbols(self, query: str) -> Optional[List[Dict]]:
        """Search for symbols matching a query."""
        try:
            tickers = yf.Tickers(query)
            # This is a simplified search - yfinance doesn't have a direct search API
            return None
        except Exception as e:
            logger.error(f"Error searching symbols: {e}")
            return None
