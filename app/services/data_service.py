import pandas as pd
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from app.models.stock import Stock, StockPrice
from app.models.news import NewsArticle
from app.collectors.twelve_data import TwelveDataCollector
from app.collectors.yfinance import YFinanceCollector
from app.scrapers.news import NewsScraper
from app.analytics.technical_indicators import TechnicalIndicators
from loguru import logger
from typing import List, Optional, Dict

INDICATOR_COLUMNS = [
    "sma_20", "sma_50", "sma_200", "ema_12", "ema_26", "rsi",
    "macd", "macd_signal", "macd_hist",
    "bollinger_upper", "bollinger_middle", "bollinger_lower",
    "atr", "obv",
]


def get_company_name(symbol: str) -> str:
    """Fetch a company's display name from external data sources, falling back to the symbol."""
    try:
        info = TwelveDataCollector().get_company_info(symbol)
        if info and info.get("name"):
            return info["name"]
    except Exception as e:
        logger.error(f"Error fetching company name for {symbol} from Twelve Data: {e}")

    try:
        info = YFinanceCollector().get_info(symbol)
        if info:
            name = info.get("longName") or info.get("shortName")
            if name:
                return name
    except Exception as e:
        logger.error(f"Error fetching company name for {symbol} from Yahoo Finance: {e}")

    return symbol


class DataService:
    """Service for collecting and storing market data."""
    
    def __init__(self, db: Session):
        self.db = db
        self.twelve_data = TwelveDataCollector()
        self.yfinance = YFinanceCollector()
    
    def update_stock_prices(self, symbols: Optional[List[str]] = None) -> int:
        """Update stock prices for given symbols or all active stocks."""
        logger.info("Starting stock price update")
        
        if symbols is None:
            # Get all active stocks from database
            stocks = self.db.query(Stock).filter(Stock.is_active == True).all()
            symbols = [stock.symbol for stock in stocks]
        
        updated_count = 0
        
        for symbol in symbols:
            try:
                # Try Twelve Data first
                quote = self.twelve_data.get_quote(symbol)
                
                if quote:
                    self._save_price_from_quote(symbol, quote)
                    updated_count += 1
                    logger.info(f"Updated price for {symbol} using Twelve Data")
                    self._update_technical_indicators(symbol)
                else:
                    # Fallback to Yahoo Finance
                    logger.warning(f"Twelve Data failed for {symbol}, trying Yahoo Finance")
                    hist = self.yfinance.get_history(symbol, period="1d", interval="1d")

                    if hist is not None and not hist.empty:
                        self._save_price_from_history(symbol, hist)
                        updated_count += 1
                        logger.info(f"Updated price for {symbol} using Yahoo Finance")
                        self._update_technical_indicators(symbol)
                    else:
                        logger.error(f"Failed to get price for {symbol} from both sources")
            
            except Exception as e:
                logger.error(f"Error updating price for {symbol}: {e}")
        
        self.db.commit()
        logger.info(f"Updated prices for {updated_count} symbols")
        return updated_count
    
    def _save_price_from_quote(self, symbol: str, quote: Dict) -> None:
        """Save price data from Twelve Data quote."""
        try:
            # Parse timestamp
            timestamp_str = quote.get("timestamp")
            if timestamp_str:
                timestamp = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
            else:
                timestamp = datetime.utcnow()
            
            # Check if we already have data for this timestamp
            existing = self.db.query(StockPrice).filter(
                StockPrice.symbol == symbol,
                StockPrice.timestamp == timestamp
            ).first()
            
            if existing:
                return
            
            # Get or create stock
            stock = self.db.query(Stock).filter(Stock.symbol == symbol).first()
            if not stock:
                # Create basic stock entry
                stock = Stock(symbol=symbol, name=get_company_name(symbol))
                self.db.add(stock)
                self.db.flush()
            
            price = StockPrice(
                stock_id=stock.id,
                symbol=symbol,
                timestamp=timestamp,
                open_price=float(quote.get("open", 0)),
                high_price=float(quote.get("high", 0)),
                low_price=float(quote.get("low", 0)),
                close_price=float(quote.get("close", 0)),
                volume=int(quote.get("volume", 0)),
            )
            
            self.db.add(price)
        
        except Exception as e:
            logger.error(f"Error saving price from quote: {e}")
    
    def _save_price_from_history(self, symbol: str, hist) -> None:
        """Save price data from Yahoo Finance history."""
        try:
            # Get the most recent data point
            if hist.empty:
                return
            
            latest = hist.iloc[-1]
            timestamp = latest.name.to_pydatetime()
            
            # Check if we already have data for this timestamp
            existing = self.db.query(StockPrice).filter(
                StockPrice.symbol == symbol,
                StockPrice.timestamp == timestamp
            ).first()
            
            if existing:
                return
            
            # Get or create stock
            stock = self.db.query(Stock).filter(Stock.symbol == symbol).first()
            if not stock:
                stock = Stock(symbol=symbol, name=get_company_name(symbol))
                self.db.add(stock)
                self.db.flush()

            price = StockPrice(
                stock_id=stock.id,
                symbol=symbol,
                timestamp=timestamp,
                open_price=float(latest.get("Open", 0)),
                high_price=float(latest.get("High", 0)),
                low_price=float(latest.get("Low", 0)),
                close_price=float(latest.get("Close", 0)),
                adjusted_close=float(latest.get("Adj Close", 0)),
                volume=int(latest.get("Volume", 0)),
            )
            
            self.db.add(price)
        
        except Exception as e:
            logger.error(f"Error saving price from history: {e}")
    
    def _update_technical_indicators(self, symbol: str) -> None:
        """Recalculate and persist technical indicators for a symbol's price history."""
        prices = self.db.query(StockPrice).filter(
            StockPrice.symbol == symbol
        ).order_by(StockPrice.timestamp.asc()).all()

        if len(prices) < 2:
            return

        try:
            df = pd.DataFrame({
                "open": [p.open_price for p in prices],
                "high": [p.high_price for p in prices],
                "low": [p.low_price for p in prices],
                "close": [p.close_price for p in prices],
                "volume": [p.volume or 0 for p in prices],
            })

            result_df = TechnicalIndicators.calculate_all_indicators(df)

            for price, (_, row) in zip(prices, result_df.iterrows()):
                for column in INDICATOR_COLUMNS:
                    value = row.get(column)
                    setattr(price, column, None if pd.isna(value) else float(value))

            self.db.commit()
        except Exception as e:
            logger.error(f"Error updating technical indicators for {symbol}: {e}")

    def collect_news(self) -> int:
        """Collect news from all sources."""
        logger.info("Starting news collection")
        
        news_scraper = NewsScraper(self.db)
        articles = news_scraper.scrape_all_sources()
        
        logger.info(f"Collected {len(articles)} news articles")
        return len(articles)
    
    def update_historical_data(self, symbol: str, days: int = 365) -> int:
        """Update historical data for a symbol."""
        logger.info(f"Updating historical data for {symbol}")
        
        # Calculate date range
        end_date = datetime.utcnow()
        start_date = end_date - timedelta(days=days)
        
        # Try Twelve Data first
        historical = self.twelve_data.get_historical_data(
            symbol,
            start_date=start_date.strftime("%Y-%m-%d"),
            end_date=end_date.strftime("%Y-%m-%d"),
            outputsize=days
        )
        
        if historical:
            saved = self._save_historical_data(symbol, historical)
            logger.info(f"Saved {saved} historical data points for {symbol} from Twelve Data")
            self._update_technical_indicators(symbol)
            return saved
        
        # Fallback to Yahoo Finance
        hist = self.yfinance.get_history_date_range(
            symbol,
            start_date=start_date.strftime("%Y-%m-%d"),
            end_date=end_date.strftime("%Y-%m-%d"),
            interval="1d"
        )
        
        if hist is not None and not hist.empty:
            saved = 0
            for index, row in hist.iterrows():
                timestamp = index.to_pydatetime()
                
                existing = self.db.query(StockPrice).filter(
                    StockPrice.symbol == symbol,
                    StockPrice.timestamp == timestamp
                ).first()
                
                if not existing:
                    stock = self.db.query(Stock).filter(Stock.symbol == symbol).first()
                    if not stock:
                        stock = Stock(symbol=symbol, name=get_company_name(symbol))
                        self.db.add(stock)
                        self.db.flush()

                    price = StockPrice(
                        stock_id=stock.id,
                        symbol=symbol,
                        timestamp=timestamp,
                        open_price=float(row.get("Open", 0)),
                        high_price=float(row.get("High", 0)),
                        low_price=float(row.get("Low", 0)),
                        close_price=float(row.get("Close", 0)),
                        adjusted_close=float(row.get("Adj Close", 0)),
                        volume=int(row.get("Volume", 0)),
                    )
                    
                    self.db.add(price)
                    saved += 1
            
            self.db.commit()
            logger.info(f"Saved {saved} historical data points for {symbol} from Yahoo Finance")
            self._update_technical_indicators(symbol)
            return saved
        
        logger.error(f"Failed to get historical data for {symbol}")
        return 0
    
    def _save_historical_data(self, symbol: str, data: List[Dict]) -> int:
        """Save historical data points."""
        saved = 0
        
        for point in data:
            try:
                timestamp_str = point.get("datetime")
                if not timestamp_str:
                    continue
                # Daily/weekly/monthly intervals return "YYYY-MM-DD" with no time
                # component, while intraday intervals include "HH:MM:SS".
                date_format = "%Y-%m-%d" if len(timestamp_str) == 10 else "%Y-%m-%d %H:%M:%S"
                timestamp = datetime.strptime(timestamp_str, date_format)
                
                # Check if already exists
                existing = self.db.query(StockPrice).filter(
                    StockPrice.symbol == symbol,
                    StockPrice.timestamp == timestamp
                ).first()
                
                if existing:
                    continue
                
                # Get or create stock
                stock = self.db.query(Stock).filter(Stock.symbol == symbol).first()
                if not stock:
                    stock = Stock(symbol=symbol, name=get_company_name(symbol))
                    self.db.add(stock)
                    self.db.flush()
                
                price = StockPrice(
                    stock_id=stock.id,
                    symbol=symbol,
                    timestamp=timestamp,
                    open_price=float(point.get("open", 0)),
                    high_price=float(point.get("high", 0)),
                    low_price=float(point.get("low", 0)),
                    close_price=float(point.get("close", 0)),
                    volume=int(point.get("volume", 0)),
                )
                
                self.db.add(price)
                saved += 1
            
            except Exception as e:
                logger.error(f"Error saving historical data point: {e}")
        
        self.db.commit()
        return saved
