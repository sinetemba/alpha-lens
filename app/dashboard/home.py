import streamlit as st
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session
from app.models.base import SessionLocal
from app.models.stock import Stock, StockPrice
from app.models.news import NewsArticle
from app.models.portfolio import Portfolio, PortfolioHolding
from app.config.settings import settings
from app.collectors.moneyweb import MoneywebCollector
from app.dashboard.utils import (
    format_stock_label,
    PriceSnapshot,
    get_latest_market_data_timestamp,
    get_latest_news_timestamp,
    format_market_data_age,
    format_news_age,
)
from app.dashboard.portfolio import EXCLUDED_ACCOUNT_TYPES
from app.services.data_service import DataService
from loguru import logger

# Yahoo Finance symbol for the JSE All Share Index (J203)
JSE_INDEX_YF_SYMBOL = "^J203.JO"

# Data sources shown per page in the top-right header.
PAGE_DATA_SOURCES = {
    "Home": "Moneyweb (top movers), Twelve Data, Yahoo Finance",
    "Watchlist": "Twelve Data, Yahoo Finance",
    "Portfolio": "EasyEquities, Twelve Data, Yahoo Finance",
    "Dividends": "Yahoo Finance, Moneyweb",
    "Company View": "Twelve Data, Yahoo Finance",
    "Commodities": "Metals.dev, Yahoo Finance",
    "Krugerrands": "GoldAPI",
}

# Thread pool for non-blocking home page price/news refreshes.
_REFRESH_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="home_market_refresh_")


def _run_refresh(symbols: list[str], fetch_news: bool) -> None:
    """Background refresh of prices and, if allowed, news."""
    db = SessionLocal()
    try:
        data_service = DataService(db)
        data_service.update_stock_prices(symbols)
        if fetch_news:
            data_service.collect_news()
        logger.info(f"Background refresh completed for {len(symbols)} symbols")
    except Exception as e:
        logger.error(f"Background refresh failed: {e}")
    finally:
        db.close()


def _should_fetch_news(db: Session) -> bool:
    """Only fetch news if it has not already been fetched today."""
    latest = get_latest_news_timestamp(db)
    if latest is None:
        return True
    latest = latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)
    return latest.date() < datetime.now(timezone.utc).date()


def show_home():
    """Display the home dashboard."""
    st.set_page_config(
        page_title="JSE Stock Analysis Platform",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded"
    )

    # Initialize database session
    db = SessionLocal()

    try:
        # Sidebar navigation
        page = st.sidebar.radio(
            "Navigate",
            ["Home", "Watchlist", "Portfolio", "Dividends", "Company View", "Commodities", "Krugerrands"],
            index=0
        )

        # Page-specific header with data sources
        data_source = PAGE_DATA_SOURCES.get(page, "Multiple sources")
        header_col, source_col = st.columns([4, 1])
        with header_col:
            st.title("JSE Stock Analysis Platform")
        with source_col:
            st.markdown(
                f"<p style='text-align: right; font-size: small; color: #888; margin: 0;'>"
                f"Data: {data_source}"
                f"</p>",
                unsafe_allow_html=True,
            )
        st.markdown("---")

        with st.spinner("Loading page..."):
            if page == "Home":
                _render_home(db)
            elif page == "Watchlist":
                from .watchlist import show_watchlist
                show_watchlist(db)
            elif page == "Portfolio":
                from .portfolio import show_portfolio
                show_portfolio(db)
            elif page == "Dividends":
                from .dividends import show_dividends
                show_dividends(db)
            elif page == "Company View":
                from .company import show_company
                show_company(db)
            elif page == "Commodities":
                from .commodities import show_commodities
                show_commodities(db)
            elif page == "Krugerrands":
                from .krugerrands import show_krugerrands
                show_krugerrands(db)

    finally:
        db.close()


def _render_home(db: Session):
    """Render the home page content."""
    # Ensure JSE index symbol exists in the database
    if not db.query(Stock).filter(Stock.symbol == JSE_INDEX_YF_SYMBOL).first():
        db.add(Stock(symbol=JSE_INDEX_YF_SYMBOL, name="JSE All Share Index"))
        db.commit()

    portfolio_symbols = [
        h.symbol for h in db.query(PortfolioHolding).filter(
            ~PortfolioHolding.account_type.in_(EXCLUDED_ACCOUNT_TYPES)
        ).all()
    ]
    refresh_symbols = sorted(set(portfolio_symbols + [JSE_INDEX_YF_SYMBOL]))

    _render_home_refresh_control(db, refresh_symbols)

    # Get latest market data
    col1, col2, col3 = st.columns(3)

    with col1:
        st.metric(
            label="JSE Index",
            value=_get_jse_index(db),
            delta=_get_jse_change(db)
        )

    with col2:
        portfolio_value = _get_portfolio_value(db)
        st.metric(
            label="Portfolio Value",
            value=f"R {portfolio_value:,.2f}",
            delta=_get_portfolio_change(db)
        )

    with col3:
        st.metric(
            label="Daily Gain",
            value=f"R {_get_daily_gain(db):,.2f}",
            delta=_get_daily_gain_percentage(db)
        )

    st.markdown("---")

    # Top movers for the day
    st.subheader("🚀 Top Movers Today")
    _render_top_movers(db)

    st.markdown("---")

    # Market news
    st.subheader("📰 Market News")
    _render_news_feed(db)


def _render_home_refresh_control(db: Session, symbols: list[str]) -> None:
    """Render a non-blocking refresh control with last fetch times."""
    latest_price_ts = get_latest_market_data_timestamp(db, symbols)
    latest_news_ts = get_latest_news_timestamp(db)
    fetch_news = _should_fetch_news(db)

    _, refresh_col = st.columns([3, 1])
    with refresh_col:
        if st.button("🔄 Refresh Prices", key="home_market_data_refresh"):
            _REFRESH_EXECUTOR.submit(_run_refresh, symbols, fetch_news)
            st.info("Refresh started in the background; data will appear on the next page load.")

        price_age = format_market_data_age(latest_price_ts)
        news_age = format_news_age(latest_news_ts)
        st.caption(f"{price_age} | {news_age}")


@st.cache_data(ttl=60, show_spinner=False, hash_funcs={Session: lambda db: id(db.bind)})
def _get_jse_index(db: Session) -> str:
    """Get current JSE index value from the database."""
    latest = db.query(StockPrice).filter(
        StockPrice.symbol == JSE_INDEX_YF_SYMBOL
    ).order_by(StockPrice.timestamp.desc()).first()

    if latest and latest.close_price:
        return f"{latest.close_price:,.0f}"
    return "N/A"


@st.cache_data(ttl=60, show_spinner=False, hash_funcs={Session: lambda db: id(db.bind)})
def _get_jse_change(db: Session) -> str:
    """Get JSE index daily percentage change."""
    prices = _get_latest_and_previous_prices(db, [JSE_INDEX_YF_SYMBOL])
    latest, prev = prices.get(JSE_INDEX_YF_SYMBOL, (None, None))

    if not latest:
        return "N/A"

    if prev and prev.close_price and prev.close_price > 0:
        change_pct = ((latest.close_price - prev.close_price) / prev.close_price) * 100
        return f"{change_pct:+.2f}%"
    return "N/A"


def _get_portfolio_value(db: Session) -> float:
    """Get total portfolio value, excluding demo account types."""
    portfolio = db.query(Portfolio).first()
    if not portfolio:
        return 0.0
    holdings = db.query(PortfolioHolding).filter(
        PortfolioHolding.portfolio_id == portfolio.id,
        ~PortfolioHolding.account_type.in_(EXCLUDED_ACCOUNT_TYPES),
    ).all()
    return sum(h.current_value or 0.0 for h in holdings)


def _get_portfolio_change(db: Session) -> str:
    """Get portfolio gain/loss percentage, excluding demo account types."""
    portfolio = db.query(Portfolio).first()
    if not portfolio:
        return "0.0%"
    holdings = db.query(PortfolioHolding).filter(
        PortfolioHolding.portfolio_id == portfolio.id,
        ~PortfolioHolding.account_type.in_(EXCLUDED_ACCOUNT_TYPES),
    ).all()
    total_value = sum(h.current_value or 0.0 for h in holdings)
    total_cost = sum(h.quantity * h.purchase_price for h in holdings)
    pct = (
        ((total_value - total_cost) / total_cost) * 100
        if total_cost else 0.0
    )
    return f"{pct:.2f}%"


def _get_daily_gain(db: Session) -> float:
    """Get daily portfolio gain/loss by comparing current prices to previous day's close."""
    holdings = db.query(PortfolioHolding).filter(
        ~PortfolioHolding.account_type.in_(EXCLUDED_ACCOUNT_TYPES)
    ).all()
    if not holdings:
        return 0.0

    prices = _get_latest_and_previous_prices(db, [h.symbol for h in holdings])
    daily_gain = 0.0

    for h in holdings:
        if not h.current_price or not h.quantity:
            continue
        latest, prev = prices.get(h.symbol, (None, None))
        if not latest:
            continue
        if prev and prev.close_price:
            daily_gain += (latest.close_price - prev.close_price) * h.quantity

    return daily_gain


def _get_daily_gain_percentage(db: Session) -> str:
    """Get daily gain as a percentage of portfolio value."""
    portfolio = db.query(Portfolio).first()
    if portfolio and portfolio.current_value and portfolio.current_value > 0:
        daily_gain = _get_daily_gain(db)
        pct = (daily_gain / portfolio.current_value) * 100
        return f"{pct:+.2f}%"
    return "0.0%"


@st.cache_data(
    ttl=60,
    show_spinner=False,
    hash_funcs={
        Session: lambda db: id(db.bind),
        list: lambda x: hash(tuple(sorted(x))),
    },
)
def _get_latest_and_previous_prices(
    db: Session, symbols: list[str]
) -> dict[str, tuple[Optional[PriceSnapshot], Optional[PriceSnapshot]]]:
    """Batch-fetch each symbol's latest price and its most recent prior-day price.

    Uses a single windowed CTE to pull only the latest and prior-day rows per
    requested symbol, avoiding full scans and per-symbol N+1 queries.
    """
    if not symbols:
        return {}

    ranked = db.query(
        StockPrice.symbol,
        StockPrice.timestamp,
        StockPrice.close_price,
        StockPrice.volume,
        StockPrice.rsi,
        StockPrice.macd,
        StockPrice.macd_signal,
        func.dense_rank().over(
            partition_by=StockPrice.symbol,
            order_by=func.strftime('%Y-%m-%d', StockPrice.timestamp).desc(),
        ).label('day_rank'),
        func.row_number().over(
            partition_by=[StockPrice.symbol, func.strftime('%Y-%m-%d', StockPrice.timestamp)],
            order_by=StockPrice.timestamp.desc(),
        ).label('intra_rank'),
    ).filter(StockPrice.symbol.in_(symbols)).cte('ranked')

    rows = db.query(
        ranked.c.symbol,
        ranked.c.timestamp,
        ranked.c.close_price,
        ranked.c.volume,
        ranked.c.rsi,
        ranked.c.macd,
        ranked.c.macd_signal,
        ranked.c.day_rank,
    ).filter(or_(
        and_(ranked.c.day_rank == 1, ranked.c.intra_rank == 1),
        and_(ranked.c.day_rank == 2, ranked.c.intra_rank == 1),
    )).order_by(ranked.c.symbol, ranked.c.day_rank).all()

    result: dict[str, tuple[Optional[PriceSnapshot], Optional[PriceSnapshot]]] = {}
    for row in rows:
        sym = row.symbol
        latest, prev = result.get(sym, (None, None))
        snapshot = PriceSnapshot(
            close_price=row.close_price,
            timestamp=row.timestamp,
            volume=row.volume,
            rsi=row.rsi,
            macd=row.macd,
            macd_signal=row.macd_signal,
        )
        if row.day_rank == 1:
            latest = snapshot
        else:
            prev = snapshot
        result[sym] = (latest, prev)
    return result


@st.cache_data(ttl=60, show_spinner=False, hash_funcs={Session: lambda db: id(db.bind)})
def _get_daily_movers(db: Session, limit: int = 10) -> tuple[list, list]:
    """Return the top daily winners and losers from Moneyweb (always used, no fallback)."""
    collector = MoneywebCollector()
    result = collector.get_winners_and_losers()

    if result is not None:
        winners, losers = result
        return (
            [{
                "Symbol": format_stock_label(m["Symbol"], m.get("Name")),
                "Price": m["Price"],
                "Move": m["Move"],
                "Change": m["Change"],
            } for m in winners[:limit]],
            [{
                "Symbol": format_stock_label(m["Symbol"], m.get("Name")),
                "Price": m["Price"],
                "Move": m["Move"],
                "Change": m["Change"],
            } for m in losers[:limit]],
        )

    return [], []


def _render_top_movers(db: Session):
    """Render the top 10 winners and losers of the day side by side."""
    winners, losers = _get_daily_movers(db)

    col_winners, col_losers = st.columns(2)

    column_config = {
        "Price": st.column_config.NumberColumn("Price", format="R %.2f"),
        "Move": st.column_config.NumberColumn("Move", format="%.2f"),
        "Change": st.column_config.NumberColumn("Change", format="%.2f%%"),
    }

    with col_winners:
        st.markdown("**🟢 Top 10 Winners**")
        if winners:
            st.dataframe(
                [{"Symbol": m["Symbol"], "Price": m["Price"], "Move": m["Move"], "Change": m["Change"]} for m in winners],
                use_container_width=True,
                hide_index=True,
                column_config=column_config,
            )
        else:
            st.info("No winners from Moneyweb. Check Moneyweb credentials or try again later.")

    with col_losers:
        st.markdown("**🔴 Top 10 Losers**")
        if losers:
            st.dataframe(
                [{"Symbol": m["Symbol"], "Price": m["Price"], "Move": m["Move"], "Change": m["Change"]} for m in losers],
                use_container_width=True,
                hide_index=True,
                column_config=column_config,
            )
        else:
            st.info("No losers from Moneyweb. Check Moneyweb credentials or try again later.")


def _render_news_feed(db: Session):
    """Render recent news feed."""
    recent_news = db.query(NewsArticle).order_by(
        NewsArticle.published_at.desc()
    ).limit(10).all()

    if not recent_news:
        st.info("No recent news available.")
        return

    for article in recent_news:
        with st.expander(f"**{article.title}** - {article.source}"):
            st.markdown(f"*Published: {article.published_at.strftime('%Y-%m-%d %H:%M')}*")
            st.markdown(article.summary)
            st.markdown(f"[Read more]({article.url})")
