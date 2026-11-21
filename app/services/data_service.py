import pandas as pd
from sqlalchemy import func, case
from sqlalchemy.orm import Session, load_only
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from app.config.settings import settings
from app.models.base import utcnow_naive
from app.models.dividend import Dividend
from app.models.stock import Stock, StockPrice
from app.models.news import NewsArticle
from app.collectors.twelve_data import TwelveDataCollector
from app.collectors.yfinance import YFinanceCollector
from app.scrapers.news import NewsScraper
from app.analytics.technical_indicators import TechnicalIndicators
from app.utils.formatting import format_price
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
    "MTU": "Mantengu Limited",
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

    def update_stock_prices(self, symbols: List[str]) -> int:
        """Update stock prices for the given symbols.

        `symbols` is required: an implicit "all active stocks" fallback made it
        too easy for a caller to burn the whole day's Twelve Data quota by
        forgetting to pass the tracked list.
        """
        logger.info("Starting stock price update")

        # Batched quotes cost one credit per symbol, so each batch is capped at
        # the per-minute credit limit (a 10-symbol batch overshoots 8/min).
        quotes: Dict[str, Dict] = {}
        batch = self.twelve_data.max_batch_size()
        for i in range(0, len(symbols) if self.twelve_data.enabled else 0, batch):
            chunk = symbols[i:i + batch]
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
                    self._update_technical_indicators(symbol, tail=10)
                    self.db.commit()
                    updated_count += 1
                    logger.info(f"Updated price for {symbol} using Twelve Data")
                elif hist is not None and not hist.empty and self._save_recent_bars(symbol, hist):
                    self.db.commit()
                    self._update_technical_indicators(symbol, tail=10)
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
            # A zero/negative price is a provider placeholder, never a real
            # value - writing it would overwrite a good stored price.
            if value is not None and value > 0:
                setattr(price, attr, value)
        volume = self._to_float(record.get("volume"))
        # stock_prices.volume is a 32-bit INTEGER; Yahoo has glitch volumes
        # beyond it (RMH 2011-12-12: 8.35bn) that abort the whole save on
        # PostgreSQL - store those as unknown.
        if volume is not None and 0 <= volume <= 2_147_483_647:
            price.volume = int(volume)

    def _quote_bar_timestamp(self, quote: Dict) -> datetime:
        """Resolve a Twelve Data quote's trading day to naive exchange-local midnight.

        The /quote response carries ``datetime`` (exchange-local "YYYY-MM-DD" or
        "YYYY-MM-DD HH:MM:SS") and ``timestamp`` (epoch seconds) - neither is a
        bare "%Y-%m-%d %H:%M:%S" string, so parsing the wrong field/format threw
        (failing the save), while a missing timestamp fell back to now() and
        inserted a fresh non-midnight bar on every refresh. Normalising to the
        bar date keeps quote writes on the same (symbol, timestamp) key as the
        Yahoo/historical paths.
        """
        tz = ZoneInfo(settings.scheduler_timezone)

        def _parse(text: str) -> datetime:
            # fromisoformat handles "YYYY-MM-DD", "YYYY-MM-DD HH:MM:SS" and
            # offset forms like "+02:00"; strptime covers anything left over
            # rather than crashing the whole save on an unexpected format.
            try:
                return datetime.fromisoformat(text)
            except ValueError:
                for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
                    try:
                        return datetime.strptime(text, fmt)
                    except ValueError:
                        continue
                raise

        dt_str = quote.get("datetime")
        if dt_str:
            return self._bar_date_timestamp(_parse(str(dt_str)))
        epoch = quote.get("timestamp")
        if epoch:
            if isinstance(epoch, (int, float)) or str(epoch).isdigit():
                return self._bar_date_timestamp(datetime.fromtimestamp(int(epoch), tz))
            return self._bar_date_timestamp(_parse(str(epoch)))
        return self._bar_date_timestamp(datetime.now(tz))

    # Closes more than this factor away from the last stored close are
    # implausible. Ordinary JSE daily moves never get near it; unit
    # mismatches (a feed returning ZAc where ZAR was stored - a 100x
    # error) do.
    MAX_CLOSE_RATIO = 3.0
    _PRICE_FIELDS = ("open", "high", "low", "close", "adjusted_close")

    # The JSE's minimum price is 1c: a unit "correction" that lands below it
    # is matching a corrupted reference, not fixing the incoming bar (SKA
    # R0.85 was "corrected" to R0.0085 against a bad stored bar).
    MIN_JSE_PRICE = 0.0095

    def _ratio_ok(self, a: float, b: float) -> bool:
        if not a or not b:
            return False
        return max(a, b) / min(a, b) <= self.MAX_CLOSE_RATIO

    @staticmethod
    def _is_jse_equity(symbol: str) -> bool:
        """Plain JSE share codes - not indexes, FX, futures or pseudo-symbols."""
        return not any(ch in symbol for ch in ".^_=")

    def _unit_factor(self, symbol: str, close: float, ref_close: float) -> Optional[float]:
        """x100 / x0.01 factor that reconciles `close` with `ref_close`, or None.

        A factor yielding a sub-cent JSE price is refused - it can only mean
        the reference itself is 100x off.
        """
        for factor in (100.0, 0.01):
            if not self._ratio_ok(close * factor, ref_close):
                continue
            if self._is_jse_equity(symbol) and close * factor < self.MIN_JSE_PRICE:
                logger.warning(
                    f"{symbol}: x{factor} would give sub-cent R{close * factor:.5f} - "
                    f"reference close {ref_close} looks corrupted; not rescaling"
                )
                return None
            return factor
        return None

    def _close_scale_factor(
        self, symbol: str, timestamp: datetime, close: float
    ) -> tuple:
        """Decide whether `close` needs rescaling vs the last stored close.

        Returns (factor, prev_close):
          - factor == 1.0: plausible as-is (or no recent reference bar)
          - factor in (100, 0.01): apparent ZAc/ZAR unit mismatch - rescale
          - factor is None: implausible even after unit correction - reject

        Rescaling assumes the *stored* series is correct (it came through the
        same pipeline) and corrects the incoming datum. A stale reference
        (>31d, e.g. dormant/re-listed) doesn't gate - gaps over long quiet
        periods are plausible.
        """
        prev = self.db.query(StockPrice.close_price, StockPrice.timestamp).filter(
            StockPrice.symbol == symbol,
            StockPrice.timestamp < timestamp,
        ).order_by(StockPrice.timestamp.desc()).first()
        if not prev or not prev.close_price:
            return 1.0, None
        if timestamp - prev.timestamp > timedelta(days=31):
            return 1.0, prev.close_price
        if self._ratio_ok(close, prev.close_price):
            return 1.0, prev.close_price
        return self._unit_factor(symbol, close, prev.close_price), prev.close_price

    def _rescale_record(self, record: Dict, factor: float) -> None:
        """Multiply all numeric OHLC fields in a record dict by `factor`."""
        for key in self._PRICE_FIELDS:
            value = self._to_float(record.get(key))
            if value is not None:
                record[key] = value * factor

    # Two independent feeds agreeing within this tolerance corroborate a
    # large move (crash/split); a single-source unit glitch does not.
    _CROSS_CHECK_TOLERANCE = 0.05

    def _close_corroborated(
        self, symbol: str, timestamp: datetime, close: float, source: str
    ) -> Optional[bool]:
        """Cross-check a rejected close against the *other* provider.

        Returns True when the alternate source reports a close within
        _CROSS_CHECK_TOLERANCE (a real crash or corporate action looks
        identical on both feeds), False when it clearly contradicts, and
        None when the check could not be performed.
        """
        try:
            candidates = []
            if source == "twelve_data":
                bars = self.yfinance.get_latest_bars([symbol], period="10d")
                frame = bars.get(symbol)
                if frame is None or frame.empty:
                    return None
                for idx, row in frame.iterrows():
                    idx_date = idx.date() if hasattr(idx, "date") else None
                    if idx_date == timestamp.date():
                        value = self._to_float(row.get("Close"))
                        if value is not None:
                            candidates.append(value)
            else:
                quote = self.twelve_data.get_quote(symbol)
                if not quote:
                    return None
                for key in ("close", "previous_close"):
                    value = self._to_float(quote.get(key))
                    if value is not None:
                        candidates.append(value)
            if not candidates:
                return None
            return any(
                abs(candidate - close) / candidate <= self._CROSS_CHECK_TOLERANCE
                for candidate in candidates if candidate
            )
        except Exception as e:
            logger.warning(f"Alternate-source check failed for {symbol}: {e}")
            return None

    def _notify_bar_rejection(
        self, symbol: str, timestamp: datetime, close: float, prev_close: Optional[float], accepted: bool
    ) -> None:
        """Record an in-app notification for an implausible-close decision.

        Frozen prices are otherwise invisible: a rejected bar only shows up
        in the log, so a crash can leave a symbol stale for weeks without the
        user ever knowing. Notification rows are dedup'd over 20h, so repeated
        rejections across update runs do not spam the table.
        """
        try:
            from app.notifications.notification_service import NotificationService
            notifier = NotificationService(self.db)
            if accepted:
                notifier.create_notification(
                    notification_type="system",
                    title=f"Large price move accepted: {symbol}",
                    message=(
                        f"{symbol} closed at {format_price(close)} on {timestamp:%Y-%m-%d} vs the previous "
                        f"close of {format_price(prev_close)} - implausible on its own but corroborated by "
                        "the alternate data source. Possibly a market crash or corporate action "
                        "(split/consolidation). Historical bars are NOT adjusted for corporate actions."
                    ),
                    stock_symbol=symbol,
                )
            else:
                notifier.create_notification(
                    notification_type="system",
                    title=f"Suspicious price rejected: {symbol}",
                    message=(
                        f"{symbol} reported a close of {format_price(close)} on {timestamp:%Y-%m-%d} vs the "
                        f"previous close of {format_price(prev_close)} and it was not corroborated by the "
                        "alternate data source, so it was rejected. Prices for this symbol may "
                        "appear frozen until a plausible close arrives - check for a ticker "
                        "change, delisting or corporate action."
                    ),
                    stock_symbol=symbol,
                )
        except Exception as e:
            logger.error(f"Failed to create bar-rejection notification for {symbol}: {e}")

    def _unit_adjust_close(
        self, symbol: str, timestamp: datetime, record: Dict, close: float, source: str
    ) -> bool:
        """Apply unit-mismatch detection to a single record.

        Rescales the record's OHLC when a ~100x ZAc/ZAR shift is detected.
        When the close is implausible even after that, it is cross-checked
        against the other provider before being rejected: corroborated moves
        (crashes, splits/consolidations) are accepted, uncorroborated ones
        rejected - and both outcomes raise an in-app notification.
        """
        factor, prev_close = self._close_scale_factor(symbol, timestamp, close)
        if factor is None:
            corroborated = self._close_corroborated(symbol, timestamp, close, source)
            if corroborated:
                logger.warning(
                    f"{symbol} close {close} @ {timestamp:%Y-%m-%d}: implausible vs previous "
                    f"close {prev_close} but corroborated by alternate source - accepting "
                    "(possible crash or corporate action)"
                )
                self._notify_bar_rejection(symbol, timestamp, close, prev_close, accepted=True)
                return True
            logger.warning(
                f"Rejecting {symbol} close {close} @ {timestamp:%Y-%m-%d}: "
                f"implausible vs previous close {prev_close} - possible data error"
            )
            self._notify_bar_rejection(symbol, timestamp, close, prev_close, accepted=False)
            return False
        if factor != 1.0:
            self._rescale_record(record, factor)
            logger.warning(
                f"Rescaled {symbol} @ {timestamp:%Y-%m-%d}: close {close} -> "
                f"{close * factor} (x{factor} unit mismatch vs previous close {prev_close})"
            )
        return True

    def _save_price_from_quote(self, symbol: str, quote: Dict) -> bool:
        """Save price data from Twelve Data quote. Returns True if a bar was written/refreshed."""
        try:
            timestamp = self._quote_bar_timestamp(quote)

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
            if not self._unit_adjust_close(symbol, timestamp, record, close, source="twelve_data"):
                return False

            # Check if we already have data for this timestamp
            existing = self.db.query(StockPrice).filter(
                StockPrice.symbol == symbol,
                StockPrice.timestamp == timestamp
            ).first()

            if existing:
                self._apply_price_values(existing, record)
                if not existing.adjusted_close:
                    existing.adjusted_close = existing.close_price
                existing.fetched_at = datetime.now(timezone.utc).replace(tzinfo=None)
                return True

            stock = self._ensure_stock(symbol)

            price = StockPrice(
                stock_id=stock.id,
                symbol=symbol,
                timestamp=timestamp,
                close_price=close,
            )
            self._apply_price_values(price, record)
            if not price.adjusted_close:
                price.adjusted_close = price.close_price

            self.db.add(price)
            return True

        except Exception as e:
            logger.error(f"Error saving price from quote: {e}")
            self.db.rollback()
            return False
    
    def _save_recent_bars(self, symbol: str, hist) -> bool:
        """Save every bar in a fetched Yahoo frame, not just the latest.

        get_latest_bars returns ~5 trading days; writing all of them fills
        gaps left by downtime/weekends in the same pass that refreshes
        today's bar. _bulk_save_stock_prices dedups and refreshes existing
        bars in place, and unit-checks the newest close. Returns True when at
        least one bar was written or the latest bar was already stored (a
        refresh is still a successful update).
        """
        try:
            if hist is None or hist.empty:
                return False

            records = []
            for index, row in hist.iterrows():
                records.append({
                    "timestamp": self._bar_date_timestamp(index.to_pydatetime()),
                    "open": row.get("Open"),
                    "high": row.get("High"),
                    "low": row.get("Low"),
                    "close": row.get("Close"),
                    "adjusted_close": row.get("Adj Close"),
                    "volume": row.get("Volume"),
                })
            if not records:
                return False

            if self._bulk_save_stock_prices(symbol, records) > 0:
                return True

            # Bulk returned 0: either every bar was a duplicate refresh (still
            # a successful update) or the newest close failed the unit check.
            newest_ts = max(r["timestamp"] for r in records)
            return self.db.query(StockPrice).filter(
                StockPrice.symbol == symbol,
                StockPrice.timestamp == newest_ts,
            ).first() is not None

        except Exception as e:
            logger.error(f"Error saving recent bars for {symbol}: {e}")
            self.db.rollback()
            return False
    
    def _ensure_stock(self, symbol: str) -> Stock:
        """Get or create a Stock row for a symbol, returning the (possibly new) instance."""
        stock = self.db.query(Stock).filter(Stock.symbol == symbol).first()
        if not stock:
            stock = Stock(symbol=symbol, name=get_company_name(symbol))
            self.db.add(stock)
            self.db.flush()
        if not stock.sector and stock.details_fetched_at is None:
            self._populate_stock_details(stock)
        return stock

    def _populate_stock_details(self, stock: Stock) -> None:
        """Backfill sector/industry/cap/description from Yahoo's company profile.

        These columns were only seeded for the 10 built-in stocks, so Company
        View showed blank metrics for everything else. Runs once per stock:
        details_fetched_at is stamped whenever a profile comes back, even a
        sector-less one (indexes, ETFs) - otherwise those symbols would pay a
        get_info call on every price save forever. A failed/empty fetch leaves
        it NULL so a transient outage retries next run.
        """
        try:
            info = self.yfinance.get_info(stock.symbol)
            if not isinstance(info, dict) or not info:
                return
            stock.details_fetched_at = utcnow_naive()

            def _str(value) -> Optional[str]:
                return value if isinstance(value, str) and value else None

            def _num(value) -> Optional[float]:
                return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

            stock.sector = stock.sector or _str(info.get("sector"))
            stock.industry = stock.industry or _str(info.get("industry"))
            stock.market_cap = stock.market_cap or _num(info.get("marketCap"))
            stock.shares_outstanding = stock.shares_outstanding or _num(info.get("sharesOutstanding"))
            stock.description = stock.description or _str(info.get("longBusinessSummary"))
            stock.website = stock.website or _str(info.get("website"))
        except Exception as e:
            logger.warning(f"Could not populate details for {stock.symbol}: {e}")

    def _bulk_save_stock_prices(
        self, symbol: str, records: List[Dict], source: str = "yfinance",
        trust_units: bool = False,
    ) -> int:
        """Batch-save historical prices. Existing bars are refreshed in place.

        Returns the number of NEW bars inserted - existing rows are updated
        but not counted, so the return value means "history points added".

        `trust_units=True` (repairs) neither anchors on the stored bar before
        the batch nor applies x100/x0.01 unit corrections: the incoming
        series is already unit-normalised (collectors/jse_units.py) and the
        stored bars are what is being repaired. Implausible jumps WITHIN the
        batch are still rejected.
        """
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

        # Per-bar unit/implausibility check (was: newest close decided a
        # batch-wide rescale-or-reject, which corrupted clean bars in mixed-
        # unit frames and dropped good bars over one bad one). Each bar is
        # compared to the previous reference close - the stored bar before
        # the batch, then each accepted bar in order.
        ordered = sorted(records_by_ts)
        prev_stored = self.db.query(StockPrice.close_price, StockPrice.timestamp).filter(
            StockPrice.symbol == symbol,
            StockPrice.timestamp < ordered[0],
        ).order_by(StockPrice.timestamp.desc()).first()
        ref_close = prev_stored.close_price if prev_stored and not trust_units else None
        ref_ts = prev_stored.timestamp if prev_stored and not trust_units else None

        rejected_ts = set()
        rescaled = []
        for ts in ordered:
            record = records_by_ts[ts]
            close = self._to_float(record.get("close"))
            if close is None or close <= 0:
                continue  # invalid closes are skipped in the insert loop
            factor: Optional[float] = 1.0
            if ref_close and ts - ref_ts <= timedelta(days=31):
                if not self._ratio_ok(close, ref_close):
                    factor = None if trust_units else self._unit_factor(symbol, close, ref_close)
            if factor is None:
                rejected_ts.add(ts)
                continue
            if factor != 1.0:
                self._rescale_record(record, factor)
                close = self._to_float(record.get("close"))
                rescaled.append((ts, factor))
            ref_close, ref_ts = close, ts

        if rescaled:
            # One line per batch, not per bar (a single run logged 930).
            factors = sorted({f for _, f in rescaled})
            logger.warning(
                f"Rescaled {len(rescaled)} {symbol} bar(s) x{'/x'.join(f'{f:g}' for f in factors)} "
                f"({rescaled[0][0]:%Y-%m-%d} .. {rescaled[-1][0]:%Y-%m-%d}) to match the stored series"
            )

        if rejected_ts:
            newest_bad = max(rejected_ts)
            bad_close = self._to_float(records_by_ts[newest_bad].get("close"))
            # Same corroboration rule as the single-bar path - but only a
            # lone rejected bar can be a crash; sustained divergence across
            # several bars is a unit/feed problem, not a market event.
            corroborated = (
                len(rejected_ts) == 1
                and self._close_corroborated(symbol, newest_bad, bad_close, source)
            )
            if corroborated:
                logger.warning(
                    f"{symbol} @ {newest_bad:%Y-%m-%d}: close {bad_close} implausible "
                    "but corroborated by alternate source - accepting "
                    "(possible crash or corporate action)"
                )
                self._notify_bar_rejection(symbol, newest_bad, bad_close, ref_close, accepted=True)
                rejected_ts.discard(newest_bad)
            else:
                logger.warning(
                    f"Rejecting {len(rejected_ts)} {symbol} bar(s): implausible vs "
                    f"reference closes (newest {newest_bad:%Y-%m-%d} close {bad_close})"
                )
                self._notify_bar_rejection(symbol, newest_bad, bad_close, ref_close, accepted=False)
                for ts in rejected_ts:
                    del records_by_ts[ts]
                if not records_by_ts:
                    return 0

        # Only query the timestamps we are about to write.
        existing_rows = self.db.query(StockPrice).filter(
            StockPrice.symbol == symbol,
            StockPrice.timestamp.in_(list(records_by_ts.keys())),
        ).all()

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for row in existing_rows:
            record = records_by_ts.get(row.timestamp)
            if record:
                self._apply_price_values(row, record)
            if not row.adjusted_close:
                row.adjusted_close = row.close_price
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
            if not price.adjusted_close:
                price.adjusted_close = price.close_price
            new_prices.append(price)

        if new_prices:
            self.db.add_all(new_prices)
            self.db.commit()
        elif existing_rows:
            self.db.commit()

        return len(new_prices)

    def _update_technical_indicators(self, symbol: str, full: bool = False, tail: int = 1) -> None:
        """Recalculate and persist technical indicators for a symbol's price history.

        By default, only the newest `tail` price rows are updated using the
        last 250 rows of history (enough for SMA-200). `tail` should cover any
        bars inserted since the last run - gap-filled days would otherwise
        keep NULL indicators forever. Set ``full=True`` to recalculate and
        persist the entire available history.
        """
        # Only OHLCV + id/timestamp feed the calculation - the ~12 indicator
        # columns being overwritten don't need to be loaded back first.
        base_query = self.db.query(StockPrice).options(load_only(
            StockPrice.id, StockPrice.timestamp, StockPrice.open_price,
            StockPrice.high_price, StockPrice.low_price,
            StockPrice.close_price, StockPrice.volume,
        )).filter(StockPrice.symbol == symbol)
        if full:
            prices = base_query.order_by(StockPrice.timestamp.asc()).all()
        else:
            prices = list(reversed(
                base_query.order_by(StockPrice.timestamp.desc()).limit(250).all()
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

            rows_to_update = prices if full else prices[-max(tail, 1):]
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

    def _existing_dividends_by_key(self, symbol: str) -> dict:
        """Return {(ex-date, amount): Dividend} for the symbol's stored dividends.

        Dedup is by calendar day + amount: providers disagree on the
        time-of-day component, and a special dividend can share an ex-date
        with an interim - day-only dedup silently dropped one of them.
        """
        rows = self.db.query(Dividend).filter(Dividend.symbol == symbol).all()
        return {
            (r.ex_dividend_date.date(), round(r.amount or 0.0, 6)): r
            for r in rows if r.ex_dividend_date
        }

    def _price_closes(
        self, symbol: str, since: Optional[datetime] = None, until: Optional[datetime] = None
    ) -> list:
        """(timestamp, close) pairs for a symbol, ascending - loaded once per symbol.

        `since`/`until` bound the scan to the window a caller can possibly
        need (dividend unit checks only look ±730 days around ex-dates, so a
        full-history load per symbol was wasted work).
        """
        query = self.db.query(StockPrice.timestamp, StockPrice.close_price).filter(
            StockPrice.symbol == symbol,
            StockPrice.close_price.isnot(None),
        )
        if since is not None:
            query = query.filter(StockPrice.timestamp >= since)
        if until is not None:
            query = query.filter(StockPrice.timestamp <= until)
        return query.order_by(StockPrice.timestamp.asc()).all()

    @staticmethod
    def _close_near(closes: list, ts: datetime, max_age_days: int = 730) -> Optional[float]:
        """Close nearest to `ts` within `max_age_days` either side, else None.

        Dividend amounts are only unit-checked against a contemporaneous
        price: comparing a decade-old dividend to today's (possibly crashed)
        share price misclassifies legitimate amounts as cents.
        """
        window = timedelta(days=max_age_days)
        best = None
        for close_ts, close_price in closes:
            if abs(close_ts - ts) <= window:
                if best is None or abs(close_ts - ts) < abs(best[0] - ts):
                    best = (close_ts, close_price)
        return best[1] if best else None

    @staticmethod
    def _dividend_amount_rand(symbol: str, amount: float, close: Optional[float]) -> float:
        """Return the dividend amount in Rand, rescaling obvious cents values.

        Yahoo reports .JO dividend amounts in cents for some tickers and Rand
        for others, so a blanket conversion is wrong. A cash dividend larger
        than half the contemporaneous share price is implausible and is
        treated as cents.
        """
        if close and amount > close * 0.5:
            logger.info(
                f"Rescaling {symbol} dividend {amount} -> {amount / 100}: "
                f"amount > 50% of share price {close:.2f}, assumed cents"
            )
            return amount / 100
        return amount

    def update_dividends(self, symbols: List[str], freshness_days: int = 7) -> int:
        """Fetch and store dividend history for the given symbols.

        `symbols` is required (see `update_stock_prices` - the implicit
        all-active-stocks fallback was a quota trap). Symbols whose
        dividends were fetched within `freshness_days` are skipped -
        declarations change at weekly cadence, not daily; pass 0 to force.
        """
        fresh: set = set()
        if freshness_days > 0 and symbols:
            cutoff = utcnow_naive() - timedelta(days=freshness_days)
            fresh = {
                s for (s,) in self.db.query(Stock.symbol).filter(
                    Stock.symbol.in_(symbols),
                    Stock.last_dividend_fetch >= cutoff,
                ).all()
            }

        saved = 0
        for symbol in symbols:
            if symbol in fresh:
                continue
            try:
                # Persist the previous symbol's rows before starting this one:
                # the rollback in `except` must only discard THIS symbol's
                # work - otherwise a failure on symbol N silently drops the
                # uncommitted rows for symbols 1..N-1 while `saved` still
                # counted them.
                self.db.commit()
                saved += self._update_dividends_for_symbol(symbol)
                stock = self.db.query(Stock).filter(Stock.symbol == symbol).first()
                if stock is None:
                    # A symbol that paid nothing gets no row from the fetch -
                    # without one the stamp never sticks and it is refetched
                    # every run.
                    stock = Stock(symbol=symbol, name=symbol)
                    self.db.add(stock)
                stock.last_dividend_fetch = utcnow_naive()
            except Exception as e:
                logger.error(f"Error updating dividends for {symbol}: {e}")
                self.db.rollback()

        self.db.commit()
        logger.info(f"Saved {saved} new dividend records")
        return saved

    def _update_dividends_for_symbol(self, symbol: str) -> int:
        """Fetch and store one symbol's dividends; returns rows added."""
        saved = 0
        td_dividends = self.twelve_data.get_dividends(symbol)

        if td_dividends:
            stock = self.db.query(Stock).filter(Stock.symbol == symbol).first()
            if not stock:
                stock = Stock(symbol=symbol, name=get_company_name(symbol))
                self.db.add(stock)
                self.db.flush()

            existing_by_key = self._existing_dividends_by_key(symbol)
            td_ex_dates = []
            for r in td_dividends:
                try:
                    td_ex_dates.append(datetime.strptime(r["ex_date"], "%Y-%m-%d"))
                except (TypeError, ValueError):
                    continue
            closes = self._price_closes(
                symbol,
                since=min(td_ex_dates) - timedelta(days=730) if td_ex_dates else None,
                until=max(td_ex_dates) + timedelta(days=730) if td_ex_dates else None,
            )

            usable = 0
            for record in td_dividends:
                ex_date_str = record.get("ex_date")
                amount = record.get("amount")
                if not ex_date_str or amount is None:
                    continue

                def _parse_date(key: str) -> Optional[datetime]:
                    value = record.get(key)
                    if not value:
                        return None
                    try:
                        return datetime.strptime(value, "%Y-%m-%d")
                    except ValueError:
                        return None

                try:
                    ex_date = datetime.strptime(ex_date_str, "%Y-%m-%d")
                except ValueError:
                    continue
                usable += 1
                # Twelve Data can report ZAc where Rand was expected -
                # same rescale rule as the Yahoo path.
                amount_rand = self._dividend_amount_rand(
                    symbol, float(amount), self._close_near(closes, ex_date)
                )
                key = (ex_date.date(), round(amount_rand, 6))

                existing = existing_by_key.get(key)
                if existing:
                    if _parse_date("payment_date") and not existing.payment_date:
                        existing.payment_date = _parse_date("payment_date")
                    if _parse_date("record_date") and not existing.record_date:
                        existing.record_date = _parse_date("record_date")
                    if _parse_date("declaration_date") and not existing.declaration_date:
                        existing.declaration_date = _parse_date("declaration_date")
                    continue

                if any(day == ex_date.date() for day, _ in existing_by_key):
                    logger.info(
                        f"{symbol}: storing additional same-day dividend "
                        f"{format_price(amount_rand)} on {ex_date:%Y-%m-%d} "
                        "(special/other payment sharing an ex-date)"
                    )

                dividend = Dividend(
                    stock_id=stock.id,
                    symbol=symbol,
                    amount=amount_rand,
                    currency="ZAR",
                    ex_dividend_date=ex_date,
                    payment_date=_parse_date("payment_date"),
                    record_date=_parse_date("record_date"),
                    declaration_date=_parse_date("declaration_date"),
                )
                self.db.add(dividend)
                existing_by_key[key] = dividend
                saved += 1

            # A truthy-but-useless TD payload (rows with no parseable
            # ex-date) must not suppress the Yahoo fallback.
            if usable:
                return saved
            logger.warning(
                f"{symbol}: Twelve Data dividends had no usable rows - trying Yahoo"
            )

        # Fallback: Yahoo Finance has ex-dividend dates but no payment dates.
        dividends = self.yfinance.get_dividends(symbol)
        if dividends is None or dividends.empty:
            return saved

        stock = self.db.query(Stock).filter(Stock.symbol == symbol).first()
        if not stock:
            stock = Stock(symbol=symbol, name=get_company_name(symbol))
            self.db.add(stock)
            self.db.flush()

        existing_by_key = self._existing_dividends_by_key(symbol)
        yf_ex_dates = []
        for d in dividends.index:
            dt = d.to_pydatetime() if hasattr(d, "to_pydatetime") else d
            yf_ex_dates.append(datetime(dt.year, dt.month, dt.day))
        closes = self._price_closes(
            symbol,
            since=min(yf_ex_dates) - timedelta(days=730) if yf_ex_dates else None,
            until=max(yf_ex_dates) + timedelta(days=730) if yf_ex_dates else None,
        )

        for date, amount in dividends.items():
            ex_date = date.to_pydatetime() if hasattr(date, "to_pydatetime") else date
            # yfinance returns tz-aware exchange-local timestamps; store the
            # ex-date as naive midnight so dedup matches the Twelve Data path.
            ex_date = datetime(ex_date.year, ex_date.month, ex_date.day)
            amount_rand = self._dividend_amount_rand(
                symbol, float(amount), self._close_near(closes, ex_date)
            )
            key = (ex_date.date(), round(amount_rand, 6))
            if key in existing_by_key:
                continue
            if any(day == ex_date.date() for day, _ in existing_by_key):
                logger.info(
                    f"{symbol}: storing additional same-day dividend "
                    f"{format_price(amount_rand)} on {ex_date:%Y-%m-%d} "
                    "(special/other payment sharing an ex-date)"
                )

            dividend = Dividend(
                stock_id=stock.id,
                symbol=symbol,
                amount=amount_rand,
                currency="ZAR",
                ex_dividend_date=ex_date,
            )
            self.db.add(dividend)
            existing_by_key[key] = dividend
            saved += 1

        return saved

    def collect_news(self) -> int:
        """Collect news from all sources."""
        logger.info("Starting news collection")
        
        news_scraper = NewsScraper(self.db)
        articles = news_scraper.scrape_all_sources()
        
        logger.info(f"Collected {len(articles)} news articles")
        return len(articles)
    
    def update_historical_data(self, symbol: str, days: int = 365) -> int:
        """Update historical data for a symbol.

        Fetches only the missing tail once a full backfill exists: a ~7-day
        overlap refreshes the last stored bar while older data doesn't
        change. The full `days` window is only fetched when there is no
        usable history - previously the daily job refetched a year of bars
        and recomputed every indicator per symbol, per run.
        """
        logger.info(f"Updating historical data for {symbol}")

        end_date = datetime.now(timezone.utc)
        window_start = end_date - timedelta(days=days)

        latest_stored = self.db.query(func.max(StockPrice.timestamp)).filter(
            StockPrice.symbol == symbol
        ).scalar()

        # Mid-history holes (an outage that left a gappy month) would never be
        # healed by a 7-day tail fetch, so a sparse window also forces the
        # full range. ~5 trading days per 7 calendar days; below half of that
        # means the stored series is not a real history, it is fragments.
        bars_in_window = self.db.query(func.count(StockPrice.id)).filter(
            StockPrice.symbol == symbol,
            StockPrice.timestamp >= window_start.replace(tzinfo=None),
        ).scalar() or 0
        expected_bars = days * 5 / 7
        has_gap = bars_in_window < expected_bars * 0.5

        backfill = (
            latest_stored is None
            or latest_stored < window_start.replace(tzinfo=None)
            or has_gap
        )
        if backfill:
            start_date = window_start
        else:
            # Small overlap: the last stored bar may still be a partial day.
            start_date = (latest_stored - timedelta(days=7)).replace(tzinfo=timezone.utc)

        # outputsize counts trading days, so the calendar-day window
        # overestimates safely; clamp it inside `days` either way.
        outputsize = min(days, max((end_date - start_date).days, 10))

        # Try Twelve Data first
        historical = self.twelve_data.get_historical_data(
            symbol,
            start_date=start_date.strftime("%Y-%m-%d"),
            end_date=end_date.strftime("%Y-%m-%d"),
            outputsize=outputsize
        )

        if historical:
            saved = self._save_historical_data(symbol, historical)
            logger.info(f"Saved {saved} historical data points for {symbol} from Twelve Data")
            # tail covers every TOUCHED bar (new + overwritten), not just
            # inserted ones - saved+1 left older refreshed bars' indicators stale.
            self._update_technical_indicators(symbol, full=backfill, tail=len(historical))
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
                    "open": row.get("Open"),
                    "high": row.get("High"),
                    "low": row.get("Low"),
                    "close": row.get("Close"),
                    # Absent with yfinance's default auto_adjust=True (Close is
                    # already adjusted) - falls back to close on save.
                    "adjusted_close": row.get("Adj Close"),
                    "volume": row.get("Volume"),
                })

            saved = self._bulk_save_stock_prices(symbol, records)
            logger.info(f"Saved {saved} historical data points for {symbol} from Yahoo Finance")
            self._update_technical_indicators(symbol, full=backfill, tail=len(records))
            self.db.commit()
            return saved

        logger.error(f"Failed to get historical data for {symbol}")
        return 0

    def update_historical_data_batch(
        self, symbols: List[str], days: int = 365
    ) -> tuple[Dict[str, int], List[str]]:
        """Update historical data for many symbols in one pass.

        Same incremental/backfill logic as update_historical_data, but the
        per-symbol stats come from a single grouped query and every symbol
        that falls through to Yahoo is covered by one batched yf.download
        instead of a serial request each. Twelve Data's history endpoint is
        per-symbol, so it is still tried first per symbol (a cheap no-op when
        disabled).

        Returns ({symbol: new_bars_saved}, failed_symbols).
        """
        if not symbols:
            return {}, []

        end_date = datetime.now(timezone.utc)
        end_naive = end_date.replace(tzinfo=None)
        window_start = end_naive - timedelta(days=days)

        # One grouped query replaces the two per-symbol queries
        # update_historical_data runs (latest bar, in-window bar count).
        stats = {}
        for symbol, latest, bars_in_window in self.db.query(
            StockPrice.symbol,
            func.max(StockPrice.timestamp),
            func.sum(case((StockPrice.timestamp >= window_start, 1), else_=0)),
        ).filter(StockPrice.symbol.in_(symbols)).group_by(StockPrice.symbol).all():
            stats[symbol] = (latest, bars_in_window or 0)

        # Decide per symbol whether it needs a full backfill or just the
        # ~7-day tail (same rules as the single-symbol path).
        expected_bars = days * 5 / 7
        plans = {}  # symbol -> (start_date, backfill)
        for symbol in symbols:
            latest_stored, bars_in_window = stats.get(symbol, (None, 0))
            backfill = (
                latest_stored is None
                or latest_stored < window_start
                or bars_in_window < expected_bars * 0.5
            )
            start = window_start if backfill else latest_stored - timedelta(days=7)
            plans[symbol] = (start, backfill)

        results: Dict[str, int] = {}
        failed: List[str] = []
        yahoo_needed: Dict[str, tuple] = {}

        # Twelve Data first, per symbol - its history endpoint takes one
        # symbol per request anyway.
        for symbol, (start, backfill) in plans.items():
            outputsize = min(days, max((end_naive - start).days, 10))
            try:
                historical = self.twelve_data.get_historical_data(
                    symbol,
                    start_date=start.strftime("%Y-%m-%d"),
                    end_date=end_date.strftime("%Y-%m-%d"),
                    outputsize=outputsize,
                )
            except Exception as e:
                historical = None
                logger.error(f"Twelve Data history failed for {symbol}: {e}")

            if not historical:
                yahoo_needed[symbol] = (start, backfill)
                continue
            try:
                saved = self._save_historical_data(symbol, historical)
                logger.info(f"Saved {saved} historical data points for {symbol} from Twelve Data")
                self._update_technical_indicators(symbol, full=backfill, tail=len(historical))
                self.db.commit()
                results[symbol] = saved
            except Exception as e:
                failed.append(symbol)
                logger.error(f"Historical update failed for {symbol}: {e}")
                self.db.rollback()

        if yahoo_needed:
            # One download from the earliest needed date; each symbol then
            # slices its own frame to its own window.
            min_start = min(start for start, _ in yahoo_needed.values())
            frames = self.yfinance.get_history_batch(
                list(yahoo_needed),
                start_date=min_start.strftime("%Y-%m-%d"),
                end_date=end_date.strftime("%Y-%m-%d"),
            )
            for symbol, (start, backfill) in yahoo_needed.items():
                try:
                    frame = frames.get(symbol)
                    if frame is None or frame.empty:
                        results[symbol] = 0
                        logger.error(f"Failed to get historical data for {symbol}")
                        continue
                    records = []
                    for index, row in frame.iterrows():
                        timestamp = self._bar_date_timestamp(index.to_pydatetime())
                        # Yahoo's window is day-granular while `start` may
                        # carry a time-of-day - compare dates, not times.
                        if timestamp.date() < start.date():
                            continue
                        records.append({
                            "timestamp": timestamp,
                            "open": row.get("Open"),
                            "high": row.get("High"),
                            "low": row.get("Low"),
                            "close": row.get("Close"),
                            # Absent with auto_adjust=True - falls back to close.
                            "adjusted_close": row.get("Adj Close"),
                            "volume": row.get("Volume"),
                        })
                    saved = self._bulk_save_stock_prices(symbol, records)
                    logger.info(f"Saved {saved} historical data points for {symbol} from Yahoo Finance")
                    self._update_technical_indicators(symbol, full=backfill, tail=len(records))
                    self.db.commit()
                    results[symbol] = saved
                except Exception as e:
                    failed.append(symbol)
                    logger.error(f"Historical update failed for {symbol}: {e}")
                    self.db.rollback()

        return results, failed

    def normalize_closes(self, symbols: List[str], years: int = 20) -> int:
        """One-shot repair: rewrite every stored bar from Yahoo's adjusted series.

        The price path historically stored raw closes while the historical
        path stored dividend-adjusted closes - bars written before the
        conventions were unified mix both. Re-fetching the full adjusted
        history and updating rows in place puts the whole series on one
        convention; indicators are recomputed since values shifted.

        Returns the number of symbols re-written.
        """
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=years * 365)
        try:
            frames = self.yfinance.get_history_batch(
                symbols,
                start_date=start_date.strftime("%Y-%m-%d"),
                end_date=end_date.strftime("%Y-%m-%d"),
            )
        except Exception as e:
            logger.error(f"Close normalization download failed: {e}")
            return 0

        fixed = 0
        for symbol in symbols:
            frame = frames.get(symbol)
            if frame is None or frame.empty:
                continue
            try:
                records = []
                for index, row in frame.iterrows():
                    records.append({
                        "timestamp": self._bar_date_timestamp(index.to_pydatetime()),
                        "open": row.get("Open"),
                        "high": row.get("High"),
                        "low": row.get("Low"),
                        "close": row.get("Close"),
                        "adjusted_close": row.get("Adj Close"),
                        "volume": row.get("Volume"),
                    })
                # The stored bars are what's being repaired - don't anchor on them.
                self._bulk_save_stock_prices(symbol, records, trust_units=True)
                self._update_technical_indicators(symbol, full=True)
                self.db.commit()
                fixed += 1
            except Exception as e:
                logger.error(f"Close normalization failed for {symbol}: {e}")
                self.db.rollback()
        logger.info(f"Normalized closes for {fixed}/{len(symbols)} symbols")
        return fixed

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

        return self._bulk_save_stock_prices(symbol, records, source="twelve_data")
