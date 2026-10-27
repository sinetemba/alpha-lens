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
        self.last_failed_symbols: List[str] = []

    def update_stock_prices(self, symbols: Optional[List[str]] = None) -> int:
        """Update stock prices for given symbols or all active stocks."""
        logger.info("Starting stock price update")

        if symbols is None:
            # Get all active stocks from database
            stocks = self.db.query(Stock).filter(Stock.is_active == True).all()
            symbols = [stock.symbol for stock in stocks]

        # Twelve Data's quote endpoint accepts up to 10 symbols per request, so
        # a few batched calls replace one throttled call per symbol.
        quotes: Dict[str, Dict] = {}
        for i in range(0, len(symbols), 10):
            chunk = symbols[i:i + 10]
            try:
                quotes.update(self.twelve_data.get_multiple_quotes(chunk))
            except Exception as e:
                logger.error(f"Error fetching Twelve Data quote batch {chunk}: {e}")

        # Batch the Yahoo Finance fallback for everything Twelve Data missed.
        missing = [s for s in symbols if s not in quotes]
        history: Dict[str, pd.DataFrame] = {}
        if missing:
            logger.info(f"{len(missing)} symbols unresolved by Twelve Data; trying Yahoo Finance")
            try:
                history = self.yfinance.get_latest_bars(missing)
            except Exception as e:
                logger.error(f"Yahoo Finance batch fallback failed: {e}")

        updated_count = 0
        self.last_failed_symbols = []

        for symbol in symbols:
            quote = quotes.get(symbol)
            hist = history.get(symbol)
            try:
                if quote and self._save_price_from_quote(symbol, quote):
                    self.db.commit()
                    self._update_technical_indicators(symbol)
                    self.db.commit()
                    updated_count += 1
                    logger.info(f"Updated price for {symbol} using Twelve Data")
                elif hist is not None and not hist.empty and self._save_price_from_history(symbol, hist):
                    self.db.commit()
                    self._update_technical_indicators(symbol)
                    self.db.commit()
                    updated_count += 1
                    logger.info(f"Updated price for {symbol} using Yahoo Finance")
                elif not quote and (hist is None or hist.empty):
                    self.last_failed_symbols.append(symbol)
                    logger.error(f"Failed to get price for {symbol} from both sources")
                else:
                    # Fetch succeeded but the save was rejected (e.g. invalid close) - already logged.
                    self.last_failed_symbols.append(symbol)
            except Exception as e:
                self.last_failed_symbols.append(symbol)
                logger.error(f"Error saving price for {symbol}: {e}")
                self.db.rollback()

        logger.info(f"Updated prices for {updated_count}/{len(symbols)} symbols")

        self.quota_message = self.twelve_data.last_rate_limit_message() or self.twelve_data.quota_exceeded_message()

        return updated_count
    
    @staticmethod
    def _to_float(value) -> Optional[float]:
        """Parse a numeric field, returning None for missing/NaN/garbage values."""
        if value is None or value == "":
            return None
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        if pd.isna(result):
            return None
        return result

    @staticmethod
    def _naive_utc(ts: datetime) -> datetime:
        """Normalise a datetime to naive UTC for consistent storage/comparison.

        The ``timestamp`` columns are naive ``DateTime`` — aware values are
        stored as their UTC wall time, so normalising here keeps Python-side
        comparisons (sets, dict keys) consistent with what the DB stores.
        """
        if ts.tzinfo is not None:
            return ts.astimezone(timezone.utc).replace(tzinfo=None)
        return ts

    @staticmethod
    def _bar_date_timestamp(ts: datetime) -> datetime:
        """Normalise a daily bar timestamp to naive midnight of its exchange-local date.

        Daily bars are keyed by trading day, not an instant: a JSE bar labelled
        ``00:00+02:00`` must land under ``YYYY-MM-DD 00:00`` like the Twelve Data
        daily bars, not ``22:00`` the previous day.
        """
        return datetime(ts.year, ts.month, ts.day)

    def _apply_price_values(self, price: StockPrice, record: Dict) -> None:
        """Overwrite a stored bar's OHLCV fields with newly fetched values.

        Daily bars are mutable while the trading day is in progress: a bar
        fetched at 09:00 is an intraday snapshot that later refreshes must
        overwrite, not freeze at first fetch.
        """
        for key, attr in (
            ("open", "open_price"),
            ("high", "high_price"),
            ("low", "low_price"),
            ("close", "close_price"),
            ("adjusted_close", "adjusted_close"),
        ):
            value = self._to_float(record.get(key))
            if value is not None:
                setattr(price, attr, value)
        volume = self._to_float(record.get("volume"))
        if volume is not None:
            price.volume = int(volume)

    def _save_price_from_quote(self, symbol: str, quote: Dict) -> bool:
        """Save price data from Twelve Data quote. Returns True if a bar was written/refreshed."""
        try:
            # Parse timestamp
            timestamp_str = quote.get("timestamp")
            if timestamp_str:
                timestamp = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
            else:
                timestamp = datetime.now(timezone.utc)
            timestamp = self._naive_utc(timestamp)

            record = {
                "open": quote.get("open"),
                "high": quote.get("high"),
                "low": quote.get("low"),
                "close": quote.get("close"),
                "volume": quote.get("volume"),
            }
            close = self._to_float(record["close"])
            if close is None or close <= 0:
                logger.warning(f"Skipping {symbol}: invalid close price {record['close']!r} in quote")
                return False

            # Check if we already have data for this timestamp
            existing = self.db.query(StockPrice).filter(
                StockPrice.symbol == symbol,
                StockPrice.timestamp == timestamp
            ).first()

            if existing:
                self._apply_price_values(existing, record)
                existing.fetched_at = datetime.now(timezone.utc)
                return True

            stock = self._ensure_stock(symbol)

            price = StockPrice(
                stock_id=stock.id,
                symbol=symbol,
                timestamp=timestamp,
                close_price=close,
            )
            self._apply_price_values(price, record)

            self.db.add(price)
            return True

        except Exception as e:
            logger.error(f"Error saving price from quote: {e}")
            self.db.rollback()
            return False
    
    def _save_price_from_history(self, symbol: str, hist) -> bool:
        """Save price data from Yahoo Finance history. Returns True if a bar was written/refreshed."""
        try:
            # Get the most recent data point
            if hist.empty:
                return False

            latest = hist.iloc[-1]
            timestamp = latest.name.to_pydatetime()
            # Daily bars are keyed by the exchange-local trading date.
            timestamp = self._bar_date_timestamp(timestamp)

            record = {
                "open": latest.get("Open"),
                "high": latest.get("High"),
                "low": latest.get("Low"),
                "close": latest.get("Close"),
                "adjusted_close": latest.get("Adj Close"),
                "volume": latest.get("Volume"),
            }
            close = self._to_float(record["close"])
            if close is None or close <= 0:
                logger.warning(f"Skipping {symbol}: invalid close price {record['close']!r} in history")
                return False

            # Check if we already have data for this timestamp
            existing = self.db.query(StockPrice).filter(
                StockPrice.symbol == symbol,
                StockPrice.timestamp == timestamp
            ).first()

            if existing:
                self._apply_price_values(existing, record)
                existing.fetched_at = datetime.now(timezone.utc)
                return True

            stock = self._ensure_stock(symbol)

            price = StockPrice(
                stock_id=stock.id,
                symbol=symbol,
                timestamp=timestamp,
                close_price=close,
            )
            self._apply_price_values(price, record)
            if price.adjusted_close is None:
                price.adjusted_close = price.close_price

            self.db.add(price)
            return True

        except Exception as e:
            logger.error(f"Error saving price from history: {e}")
            self.db.rollback()
            return False
    
    def _ensure_stock(self, symbol: str) -> Stock:
        """Get or create a Stock row for a symbol, returning the (possibly new) instance."""
        stock = self.db.query(Stock).filter(Stock.symbol == symbol).first()
        if not stock:
            stock = Stock(symbol=symbol, name=get_company_name(symbol))
            self.db.add(stock)
            self.db.flush()
        return stock

    def _bulk_save_stock_prices(self, symbol: str, records: List[Dict]) -> int:
        """Batch-save historical prices. Existing bars are refreshed in place."""
        if not records:
            return 0

        stock = self._ensure_stock(symbol)

        # Normalise timestamps up front so Python-side set membership matches
        # the naive values the DB stores/returns.
        records_by_ts = {}
        for record in records:
            timestamp = record.get("timestamp")
            if timestamp:
                records_by_ts[self._naive_utc(timestamp)] = record

        if not records_by_ts:
            return 0

        # Only query the timestamps we are about to write.
        existing_rows = self.db.query(StockPrice).filter(
            StockPrice.symbol == symbol,
            StockPrice.timestamp.in_(list(records_by_ts.keys())),
        ).all()

        now = datetime.now(timezone.utc)
        for row in existing_rows:
            record = records_by_ts.get(row.timestamp)
            if record:
                self._apply_price_values(row, record)
            row.fetched_at = now

        existing = {row.timestamp for row in existing_rows}

        new_prices = []
        for timestamp, record in records_by_ts.items():
            if timestamp in existing:
                continue

            close = self._to_float(record.get("close"))
            if close is None or close <= 0:
                logger.warning(
                    f"Skipping {symbol} @ {timestamp:%Y-%m-%d}: "
                    f"invalid close {record.get('close')!r}"
                )
                continue

            price = StockPrice(
                stock_id=stock.id,
                symbol=symbol,
                timestamp=timestamp,
                close_price=close,
            )
            self._apply_price_values(price, record)
            if price.adjusted_close is None:
                price.adjusted_close = price.close_price
            new_prices.append(price)

        if new_prices:
            self.db.add_all(new_prices)
            self.db.commit()
        elif existing_rows:
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

            # No commit here: the caller commits once per run so a failure
            # mid-loop cannot leave a half-updated symbol set behind.
        except Exception as e:
            logger.error(f"Error updating technical indicators for {symbol}: {e}")
            self.db.rollback()

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
                    # yfinance returns tz-aware exchange-local timestamps; store the
                    # ex-date as naive midnight so dedup matches the Twelve Data path.
                    ex_date = datetime(ex_date.year, ex_date.month, ex_date.day)
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
                self.db.rollback()

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
            self.db.commit()
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
                # Daily bars are keyed by the exchange-local trading date.
                timestamp = self._bar_date_timestamp(timestamp)
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
            self.db.commit()
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
                # component, while intraday intervals include "HH:MM:SS". Twelve
                # Data labels daily bars by exchange-local date, so they are
                # stored as naive midnight - consistent with the Yahoo path.
                date_format = "%Y-%m-%d" if len(timestamp_str) == 10 else "%Y-%m-%d %H:%M:%S"
                timestamp = datetime.strptime(timestamp_str, date_format)
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
