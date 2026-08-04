import pandas as pd
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
from app.models.dividend import Dividend
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

# Local overrides for JSE symbols that external APIs consistently fail to
# resolve, so the UI shows "SYMBOL - Company Name" like the rest.
COMPANY_NAME_OVERRIDES = {
    "PHP": "Primary Health Properties PLC",
}


def get_company_name(symbol: str) -> str:
    """Fetch a company's display name from external data sources, falling back to the symbol."""
    if symbol in COMPANY_NAME_OVERRIDES:
        return COMPANY_NAME_OVERRIDES[symbol]

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

    return COMPANY_NAME_OVERRIDES.get(symbol, symbol)


class DataService:
    """Service for collecting and storing market data."""
    
    def __init__(self, db: Session):
        self.db = db
        self.twelve_data = TwelveDataCollector()
        self.yfinance = YFinanceCollector()
        self.quota_message: Optional[str] = None

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

        self.quota_message = self.twelve_data.last_rate_limit_message() or self.twelve_data.quota_exceeded_message()

        return updated_count
    
    def _save_price_from_quote(self, symbol: str, quote: Dict) -> None:
        """Save price data from Twelve Data quote."""
        try:
            # Parse timestamp
            timestamp_str = quote.get("timestamp")
            if timestamp_str:
                timestamp = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            else:
                timestamp = datetime.now(timezone.utc)
            
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
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            
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
    
    def _ensure_stock(self, symbol: str) -> Stock:
        """Get or create a Stock row for a symbol, returning the (possibly new) instance."""
        stock = self.db.query(Stock).filter(Stock.symbol == symbol).first()
        if not stock:
            stock = Stock(symbol=symbol, name=get_company_name(symbol))
            self.db.add(stock)
            self.db.flush()
        return stock

    def _bulk_save_stock_prices(self, symbol: str, records: List[Dict]) -> int:
        """Batch-save historical prices. Existing timestamps for the symbol are skipped."""
        if not records:
            return 0

        stock = self._ensure_stock(symbol)

        # One query to fetch all existing timestamps for this symbol.
        existing = {
            t[0] for t in
            self.db.query(StockPrice.timestamp).filter(StockPrice.symbol == symbol).all()
        }

        new_prices = []
        for record in records:
            timestamp = record.get("timestamp")
            if not timestamp or timestamp in existing:
                continue

            close = float(record.get("close", 0) or 0)
            price = StockPrice(
                stock_id=stock.id,
                symbol=symbol,
                timestamp=timestamp,
                open_price=float(record.get("open", 0) or 0),
                high_price=float(record.get("high", 0) or 0),
                low_price=float(record.get("low", 0) or 0),
                close_price=close,
                adjusted_close=float(record.get("adjusted_close") or close),
                volume=int(record.get("volume", 0) or 0),
            )
            new_prices.append(price)

        if new_prices:
            self.db.add_all(new_prices)
            self.db.commit()

        return len(new_prices)

    def _update_technical_indicators(self, symbol: str, full: bool = False) -> None:
        """Recalculate and persist technical indicators for a symbol's price history.

        By default, only the newest price row is updated using the last 250
        rows of history (enough for SMA-200). This keeps incremental updates
        O(1) per symbol instead of O(total history). Set ``full=True`` to
        recalculate and persist the entire available history.
        """
        if full:
            prices = self.db.query(StockPrice).filter(
                StockPrice.symbol == symbol
            ).order_by(StockPrice.timestamp.asc()).all()
        else:
            prices = list(reversed(
                self.db.query(StockPrice).filter(
                    StockPrice.symbol == symbol
                ).order_by(StockPrice.timestamp.desc()).limit(250).all()
            ))

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

            rows_to_update = prices if full else prices[-1:]
            result_rows = result_df.tail(len(rows_to_update)).iterrows()

            for price, (_, row) in zip(rows_to_update, result_rows):
                for column in INDICATOR_COLUMNS:
                    value = row.get(column)
                    setattr(price, column, None if pd.isna(value) else float(value))

            self.db.commit()
        except Exception as e:
            logger.error(f"Error updating technical indicators for {symbol}: {e}")

    def update_dividends(self, symbols: Optional[List[str]] = None) -> int:
        """Fetch and store dividend history for active stocks."""
        if symbols is None:
            stocks = self.db.query(Stock).filter(Stock.is_active == True).all()
            symbols = [stock.symbol for stock in stocks]

        saved = 0
        for symbol in symbols:
            try:
                td_dividends = self.twelve_data.get_dividends(symbol)

                if td_dividends:
                    stock = self.db.query(Stock).filter(Stock.symbol == symbol).first()
                    if not stock:
                        stock = Stock(symbol=symbol, name=get_company_name(symbol))
                        self.db.add(stock)
                        self.db.flush()

                    for record in td_dividends:
                        ex_date_str = record.get("ex_date")
                        amount = record.get("amount")
                        if not ex_date_str or amount is None:
                            continue

                        ex_date = datetime.strptime(ex_date_str, "%Y-%m-%d")

                        def _parse_date(key: str) -> Optional[datetime]:
                            value = record.get(key)
                            return datetime.strptime(value, "%Y-%m-%d") if value else None

                        payment_date = _parse_date("payment_date")
                        record_date = _parse_date("record_date")
                        declaration_date = _parse_date("declaration_date")

                        existing = self.db.query(Dividend).filter(
                            Dividend.symbol == symbol,
                            Dividend.ex_dividend_date == ex_date,
                        ).first()
                        if existing:
                            if payment_date and not existing.payment_date:
                                existing.payment_date = payment_date
                            if record_date and not existing.record_date:
                                existing.record_date = record_date
                            if declaration_date and not existing.declaration_date:
                                existing.declaration_date = declaration_date
                            continue

                        dividend = Dividend(
                            stock_id=stock.id,
                            symbol=symbol,
                            amount=float(amount),
                            currency="ZAR",
                            ex_dividend_date=ex_date,
                            payment_date=payment_date,
                            record_date=record_date,
                            declaration_date=declaration_date,
                        )
                        self.db.add(dividend)
                        saved += 1

                    continue

                # Fallback: Yahoo Finance has ex-dividend dates but no payment dates.
                dividends = self.yfinance.get_dividends(symbol)
                if dividends is None or dividends.empty:
                    continue

                stock = self.db.query(Stock).filter(Stock.symbol == symbol).first()
                if not stock:
                    stock = Stock(symbol=symbol, name=get_company_name(symbol))
                    self.db.add(stock)
                    self.db.flush()

                for date, amount in dividends.items():
                    ex_date = date.to_pydatetime() if hasattr(date, "to_pydatetime") else date
                    existing = self.db.query(Dividend).filter(
                        Dividend.symbol == symbol,
                        Dividend.ex_dividend_date == ex_date,
                    ).first()
                    if existing:
                        continue

                    dividend = Dividend(
                        stock_id=stock.id,
                        symbol=symbol,
                        amount=float(amount),
                        currency="ZAR",
                        ex_dividend_date=ex_date,
                    )
                    self.db.add(dividend)
                    saved += 1

            except Exception as e:
                logger.error(f"Error updating dividends for {symbol}: {e}")

        self.db.commit()
        logger.info(f"Saved {saved} new dividend records")
        return saved

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
        end_date = datetime.now(timezone.utc)
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
            self._update_technical_indicators(symbol, full=True)
            return saved
        
        # Fallback to Yahoo Finance
        hist = self.yfinance.get_history_date_range(
            symbol,
            start_date=start_date.strftime("%Y-%m-%d"),
            end_date=end_date.strftime("%Y-%m-%d"),
            interval="1d"
        )
        
        if hist is not None and not hist.empty:
            records = []
            for index, row in hist.iterrows():
                timestamp = index.to_pydatetime()
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                records.append({
                    "timestamp": timestamp,
                    "open": row.get("Open", 0),
                    "high": row.get("High", 0),
                    "low": row.get("Low", 0),
                    "close": row.get("Close", 0),
                    "adjusted_close": row.get("Adj Close", 0),
                    "volume": row.get("Volume", 0),
                })

            saved = self._bulk_save_stock_prices(symbol, records)
            logger.info(f"Saved {saved} historical data points for {symbol} from Yahoo Finance")
            self._update_technical_indicators(symbol, full=True)
            return saved
        
        logger.error(f"Failed to get historical data for {symbol}")
        return 0
    
    def _save_historical_data(self, symbol: str, data: List[Dict]) -> int:
        """Save historical data points from Twelve Data in bulk."""
        records = []
        for point in data:
            try:
                timestamp_str = point.get("datetime")
                if not timestamp_str:
                    continue
                # Daily/weekly/monthly intervals return "YYYY-MM-DD" with no time
                # component, while intraday intervals include "HH:MM:SS".
                date_format = "%Y-%m-%d" if len(timestamp_str) == 10 else "%Y-%m-%d %H:%M:%S"
                timestamp = datetime.strptime(timestamp_str, date_format).replace(tzinfo=timezone.utc)
                records.append({
                    "timestamp": timestamp,
                    "open": point.get("open", 0),
                    "high": point.get("high", 0),
                    "low": point.get("low", 0),
                    "close": point.get("close", 0),
                    "volume": point.get("volume", 0),
                })
            except Exception as e:
                logger.error(f"Error parsing historical data point: {e}")

        return self._bulk_save_stock_prices(symbol, records)
