import streamlit as st
import plotly.graph_objects as go
from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlalchemy.orm import Session
from app.models.base import SessionLocal
from app.models.stock import Stock, StockPrice
from app.models.news import NewsArticle
from app.models.watchlist import WatchlistItem
from app.models.portfolio import Portfolio, PortfolioHolding
from app.config.settings import settings
from app.dashboard.utils import format_stock_label, render_market_data_refresh_control
from app.dashboard.portfolio import EXCLUDED_ACCOUNT_TYPES
from loguru import logger

# Yahoo Finance symbol for the JSE All Share Index (J203)
JSE_INDEX_YF_SYMBOL = "^J203.JO"


def show_home():
    """Display the home dashboard."""
    st.set_page_config(
        page_title="JSE Stock Analysis Platform",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    
    header_col, source_col = st.columns([4, 1])
    with header_col:
        st.title("JSE Stock Analysis Platform")
    with source_col:
        st.markdown(
            "<p style='text-align: right; font-size: small; color: #888; margin: 0;'>"
            "Data: Yahoo Finance, Twelve Data, EasyEquities, JSE"
            "</p>",
            unsafe_allow_html=True,
        )
    st.markdown("---")
    
    # Initialize database session
    db = SessionLocal()
    
    try:
        # Sidebar navigation
        page = st.sidebar.radio(
            "Navigate",
            ["Home", "Watchlist", "Portfolio", "Dividends", "Company View", "Commodities", "Krugerrands"],
            index=0
        )

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

    render_market_data_refresh_control(
        db, key="home_market_data_refresh", include_news=True, auto_refresh=True
    )
    # Get latest market data
    col1, col2, col3, col4 = st.columns(4)
    
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
    
    with col4:
        watchlist_count = db.query(WatchlistItem).filter(
            WatchlistItem.is_active == True
        ).count()
        st.metric(
            label="Watchlist Items",
            value=watchlist_count,
            delta="Stocks tracked"
        )
    
    st.markdown("---")
    
    # Top movers for the day
    st.subheader("🚀 Top Movers Today")
    _render_top_movers(db)

    st.markdown("---")

    # Two-column layout
    col_left, col_right = st.columns([2, 1])

    with col_left:
        st.subheader("📊 Watchlist Summary")
        _render_watchlist_summary(db)

    with col_right:
        st.subheader("📰 Market News")
        _render_news_feed(db)

    st.markdown("---")
    
    # Recent performance chart
    st.subheader("📈 Recent Market Performance")
    _render_performance_chart(db)


def _get_jse_index(db: Session) -> str:
    """Get current JSE index value from the database."""
    latest = db.query(StockPrice).filter(
        StockPrice.symbol == JSE_INDEX_YF_SYMBOL
    ).order_by(StockPrice.timestamp.desc()).first()

    if latest and latest.close_price:
        return f"{latest.close_price:,.0f}"
    return "N/A"


def _get_jse_change(db: Session) -> str:
    """Get JSE index daily percentage change."""
    latest = db.query(StockPrice).filter(
        StockPrice.symbol == JSE_INDEX_YF_SYMBOL
    ).order_by(StockPrice.timestamp.desc()).first()

    if not latest:
        return "N/A"

    prev = _get_previous_day_price(db, JSE_INDEX_YF_SYMBOL, latest.timestamp)

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
    daily_gain = 0.0

    for h in holdings:
        if not h.current_price or not h.quantity:
            continue
        # Get the previous trading day's close for this symbol
        latest = db.query(StockPrice).filter(
            StockPrice.symbol == h.symbol
        ).order_by(StockPrice.timestamp.desc()).first()

        if not latest:
            continue

        prev = _get_previous_day_price(db, h.symbol, latest.timestamp)

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


def _render_watchlist_summary(db: Session):
    """Render watchlist summary table."""
    watchlist_items = db.query(WatchlistItem).filter(
        WatchlistItem.is_active == True
    ).all()
    
    if not watchlist_items:
        st.info("No stocks in watchlist. Add stocks to track them.")
        return
    
    # Get latest prices for watchlist items
    data = []
    for item in watchlist_items:
        latest_price = db.query(StockPrice).filter(
            StockPrice.symbol == item.symbol
        ).order_by(StockPrice.timestamp.desc()).first()
        
        if latest_price:
            prev_price = _get_previous_day_price(db, item.symbol, latest_price.timestamp)

            if prev_price:
                change = ((latest_price.close_price - prev_price.close_price) / prev_price.close_price) * 100
            else:
                change = 0.0
            
            data.append({
                "Symbol": format_stock_label(item.symbol, item.stock.name if item.stock else None),
                "Price": f"R {latest_price.close_price:.2f}",
                "Change": f"{change:+.2f}%",
                "Volume": f"{latest_price.volume:,}" if latest_price.volume else "N/A"
            })
    
    if data:
        st.dataframe(data, use_container_width=True)
    else:
        st.warning("No price data available for watchlist items.")


def _get_previous_day_price(db: Session, symbol: str, latest_timestamp: datetime) -> Optional[StockPrice]:
    """Get the most recent price for a symbol from a calendar day before `latest_timestamp`.

    Auto-refresh can store multiple price points for the same symbol on the
    same day (e.g. hourly), so comparing against "whatever the second-most-recent
    row is" would understate/overstate moves as an intraday change rather than
    a true day-over-day one. This looks back to the last *prior* calendar day instead.
    """
    latest_day_start = latest_timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
    return db.query(StockPrice).filter(
        StockPrice.symbol == symbol,
        StockPrice.timestamp < latest_day_start
    ).order_by(StockPrice.timestamp.desc()).first()


def _get_daily_movers(db: Session, limit: int = 5) -> tuple[list, list]:
    """Compute the top daily winners and losers across all tracked JSE stocks.

    This considers every active stock in the database (default seeded stocks,
    watchlist additions, and portfolio holdings alike) — not just the ones on
    the user's current watchlist or in their portfolio.
    """
    stocks = db.query(Stock).filter(
        Stock.is_active == True,
        Stock.symbol != JSE_INDEX_YF_SYMBOL,
    ).all()

    movers = []
    for stock in stocks:
        latest = db.query(StockPrice).filter(
            StockPrice.symbol == stock.symbol
        ).order_by(StockPrice.timestamp.desc()).first()

        if not latest:
            continue

        prev = _get_previous_day_price(db, stock.symbol, latest.timestamp)

        if not prev or not prev.close_price:
            continue

        change_pct = ((latest.close_price - prev.close_price) / prev.close_price) * 100
        movers.append({
            "Symbol": format_stock_label(stock.symbol, stock.name),
            "Price": f"R {latest.close_price:.2f}",
            "Change": f"{change_pct:+.2f}%",
            "_change_pct": change_pct,
        })

    movers.sort(key=lambda m: m["_change_pct"], reverse=True)
    winners = [m for m in movers if m["_change_pct"] > 0][:limit]
    losers = sorted(
        [m for m in movers if m["_change_pct"] < 0], key=lambda m: m["_change_pct"]
    )[:limit]
    return winners, losers


def _render_top_movers(db: Session):
    """Render the top 5 winners and losers of the day side by side."""
    winners, losers = _get_daily_movers(db)

    col_winners, col_losers = st.columns(2)

    with col_winners:
        st.markdown("**🟢 Top 5 Winners**")
        if winners:
            st.dataframe(
                [{"Symbol": m["Symbol"], "Price": m["Price"], "Change": m["Change"]} for m in winners],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No winners found. Try refreshing market data.")

    with col_losers:
        st.markdown("**🔴 Top 5 Losers**")
        if losers:
            st.dataframe(
                [{"Symbol": m["Symbol"], "Price": m["Price"], "Change": m["Change"]} for m in losers],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No losers found. Try refreshing market data.")


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


def _render_performance_chart(db: Session):
    """Render performance chart for watchlist stocks."""
    watchlist_items = db.query(WatchlistItem).filter(
        WatchlistItem.is_active == True
    ).limit(5).all()
    
    if not watchlist_items:
        st.info("Add stocks to watchlist to see performance charts.")
        return
    
    fig = go.Figure()
    
    for item in watchlist_items:
        prices = db.query(StockPrice).filter(
            StockPrice.symbol == item.symbol,
            StockPrice.timestamp >= datetime.now(timezone.utc) - timedelta(days=30)
        ).order_by(StockPrice.timestamp.asc()).all()
        
        if prices:
            fig.add_trace(go.Scatter(
                x=[p.timestamp for p in prices],
                y=[p.close_price for p in prices],
                name=format_stock_label(item.symbol, item.stock.name if item.stock else None),
                mode='lines'
            ))
    
    fig.update_layout(
        title="30-Day Price Performance",
        xaxis_title="Date",
        yaxis_title="Price (ZAR)",
        hovermode='x unified',
        height=400
    )
    
    st.plotly_chart(fig, use_container_width=True)
