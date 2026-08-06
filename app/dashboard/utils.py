from datetime import datetime, timedelta, timezone
from typing import Iterable, NamedTuple, Optional
from zoneinfo import ZoneInfo
import pandas as pd
import streamlit as st
from sqlalchemy.orm import Session
from app.config.settings import settings
from app.models.news import NewsArticle
from app.models.stock import StockPrice
from app.services.data_service import DataService


class PriceSnapshot(NamedTuple):
    """Lightweight, hashable price row for cache-friendly lookups."""
    close_price: float
    timestamp: datetime
    volume: Optional[int] = None
    rsi: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None


def format_stock_label(symbol: str, name: Optional[str] = None) -> str:
    """Format a stock symbol with its company name, e.g. 'SOL - Sasol Limited'."""
    if name and name != symbol:
        return f"{symbol} - {name}"
    return symbol


def style_gain_loss_row(row: pd.Series) -> list[str]:
    """Colour a table row green/red/yellow based on its '_gain_loss' value.

    Shared styling used by both the Portfolio and Watchlist pages so gain/loss
    rows are colour-coded consistently across the app.
    """
    gain_loss = row["_gain_loss"]
    if gain_loss is None:
        color = "#fef3c7"
    elif gain_loss > 0:
        color = "#dcfce7"
    elif gain_loss < 0:
        color = "#fee2e2"
    else:
        color = "#fef3c7"
    return [f"background-color: {color}"] * len(row)


def get_latest_market_data_timestamp(
    db: Session, symbols: Optional[Iterable[str]] = None
) -> Optional[datetime]:
    query = db.query(StockPrice.timestamp)
    if symbols is not None:
        symbols = list(symbols)
        if not symbols:
            return None
        query = query.filter(StockPrice.symbol.in_(symbols))
    latest_price = query.order_by(StockPrice.timestamp.desc()).first()
    return latest_price[0] if latest_price else None


def _ensure_aware(timestamp: datetime) -> datetime:
    """Existing DB rows may store naive (tz-unaware) timestamps."""
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp


def _to_display_tz(timestamp: datetime) -> datetime:
    """Convert an aware UTC timestamp to the configured display timezone."""
    timestamp = _ensure_aware(timestamp)
    try:
        return timestamp.astimezone(ZoneInfo(settings.scheduler_timezone))
    except Exception:
        return timestamp


def is_market_data_stale(timestamp: Optional[datetime]) -> bool:
    if timestamp is None:
        return True
    age = datetime.now(timezone.utc) - _ensure_aware(timestamp)
    return age > timedelta(seconds=settings.cache_ttl_seconds)


def format_market_data_age(timestamp: Optional[datetime]) -> str:
    if timestamp is None:
        return "No market data has been synced yet."

    timestamp = _ensure_aware(timestamp)
    display_timestamp = _to_display_tz(timestamp)
    age = max(datetime.now(timezone.utc) - timestamp, timedelta(0))
    if age.days:
        age_label = f"{age.days} day{'s' if age.days != 1 else ''} ago"
    elif age.seconds >= 3600:
        hours = age.seconds // 3600
        age_label = f"{hours} hour{'s' if hours != 1 else ''} ago"
    else:
        minutes = age.seconds // 60
        age_label = f"{minutes} minute{'s' if minutes != 1 else ''} ago"

    return f"Last market data: {display_timestamp.strftime('%d %b %Y %H:%M')} ({age_label})"


def get_latest_news_timestamp(db: Session) -> Optional[datetime]:
    """Return the most recent news fetch time (from created_at)."""
    latest = (
        db.query(NewsArticle.created_at)
        .order_by(NewsArticle.created_at.desc())
        .first()
    )
    return latest[0] if latest else None


def format_news_age(timestamp: Optional[datetime]) -> str:
    """Format the age of the latest news fetch for display."""
    if timestamp is None:
        return "No news has been fetched yet."

    timestamp = _ensure_aware(timestamp)
    display_timestamp = _to_display_tz(timestamp)
    age = max(datetime.now(timezone.utc) - timestamp, timedelta(0))
    if age.days:
        age_label = f"{age.days} day{'s' if age.days != 1 else ''} ago"
    elif age.seconds >= 3600:
        hours = age.seconds // 3600
        age_label = f"{hours} hour{'s' if hours != 1 else ''} ago"
    else:
        minutes = age.seconds // 60
        age_label = f"{minutes} minute{'s' if minutes != 1 else ''} ago"

    return f"Last news fetch: {display_timestamp.strftime('%d %b %Y %H:%M')} ({age_label})"


def render_market_data_refresh_control(
    db: Session,
    key: str,
    symbols: Optional[Iterable[str]] = None,
    include_news: bool = False,
    auto_refresh: bool = False,
) -> None:
    """Render the manual "Refresh Prices" control.

    If `auto_refresh` is set, market data older than `settings.cache_ttl_seconds`
    is refreshed automatically (no button click needed) before the rest of the
    page renders, so callers reading prices afterwards see current data.
    """
    symbol_list = list(symbols) if symbols is not None else None
    latest_timestamp = get_latest_market_data_timestamp(db, symbol_list)
    needs_auto_refresh = auto_refresh and is_market_data_stale(latest_timestamp)

    _, refresh_column = st.columns([3, 1])
    with refresh_column:
        manual_refresh = st.button("🔄 Refresh Prices", key=key)
        if manual_refresh or needs_auto_refresh:
            spinner_text = (
                "Fetching latest market data..." if manual_refresh
                else "Auto-refreshing market data..."
            )
            with st.spinner(spinner_text):
                data_service = DataService(db)
                updated_prices = data_service.update_stock_prices(symbol_list)
                new_articles = data_service.collect_news() if include_news else 0
            if data_service.quota_message:
                st.warning(
                    f"{data_service.quota_message} "
                    f"{format_market_data_age(latest_timestamp)}"
                )
            if manual_refresh:
                status = f"Updated prices for {updated_prices} symbol{'s' if updated_prices != 1 else ''}."
                if include_news:
                    status += f" Collected {new_articles} new article{'s' if new_articles != 1 else ''}."
                st.success(status)
                st.rerun()
            latest_timestamp = get_latest_market_data_timestamp(db, symbol_list)
        st.caption(format_market_data_age(latest_timestamp))
