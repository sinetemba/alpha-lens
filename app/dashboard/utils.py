from datetime import datetime, timedelta
from typing import Iterable, Optional
import streamlit as st
from sqlalchemy.orm import Session
from app.models.stock import StockPrice
from app.services.data_service import DataService


def format_stock_label(symbol: str, name: Optional[str] = None) -> str:
    """Format a stock symbol with its company name, e.g. 'SOL - Sasol Limited'."""
    if name and name != symbol:
        return f"{symbol} - {name}"
    return symbol


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


def format_market_data_age(timestamp: Optional[datetime]) -> str:
    if timestamp is None:
        return "No market data has been synced yet."

    age = max(datetime.utcnow() - timestamp, timedelta(0))
    if age.days:
        age_label = f"{age.days} day{'s' if age.days != 1 else ''} ago"
    elif age.seconds >= 3600:
        hours = age.seconds // 3600
        age_label = f"{hours} hour{'s' if hours != 1 else ''} ago"
    else:
        minutes = age.seconds // 60
        age_label = f"{minutes} minute{'s' if minutes != 1 else ''} ago"

    return f"Last market data: {timestamp.strftime('%d %b %Y %H:%M')} ({age_label})"


def render_market_data_refresh_control(
    db: Session,
    key: str,
    symbols: Optional[Iterable[str]] = None,
    include_news: bool = False,
) -> None:
    symbol_list = list(symbols) if symbols is not None else None
    _, refresh_column = st.columns([3, 1])
    with refresh_column:
        if st.button("🔄 Refresh Prices", key=key):
            with st.spinner("Fetching latest market data..."):
                data_service = DataService(db)
                updated_prices = data_service.update_stock_prices(symbol_list)
                new_articles = data_service.collect_news() if include_news else 0
            status = f"Updated prices for {updated_prices} symbol{'s' if updated_prices != 1 else ''}."
            if include_news:
                status += f" Collected {new_articles} new article{'s' if new_articles != 1 else ''}."
            st.success(status)
            st.rerun()
        st.caption(format_market_data_age(get_latest_market_data_timestamp(db, symbol_list)))
