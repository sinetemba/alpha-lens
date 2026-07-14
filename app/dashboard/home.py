import streamlit as st
import plotly.graph_objects as go
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from app.models.base import SessionLocal
from app.models.stock import Stock, StockPrice
from app.models.news import NewsArticle
from app.models.watchlist import WatchlistItem
from app.models.portfolio import Portfolio, PortfolioHolding
from app.config.settings import settings
from loguru import logger


def show_home():
    """Display the home dashboard."""
    st.set_page_config(
        page_title="JSE Stock Analysis Platform",
        page_icon="📈",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    
    st.title("📈 JSE Stock Analysis Platform")
    st.markdown("---")
    
    # Initialize database session
    db = SessionLocal()
    
    try:
        # Sidebar navigation
        page = st.sidebar.radio(
            "Navigate",
            ["Home", "Watchlist", "Portfolio", "Company View"],
            index=0
        )
        
        if page == "Home":
            _render_home(db)
        elif page == "Watchlist":
            from .watchlist import show_watchlist
            show_watchlist(db)
        elif page == "Portfolio":
            from .portfolio import show_portfolio
            show_portfolio(db)
        elif page == "Company View":
            from .company import show_company
            show_company(db)
    
    finally:
        db.close()


def _render_home(db: Session):
    """Render the home page content."""
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
    """Get current JSE index value."""
    # This would typically fetch the actual JSE index
    # For now, return a placeholder
    return "78,450"


def _get_jse_change(db: Session) -> str:
    """Get JSE index change."""
    return "+1.2%"


def _get_portfolio_value(db: Session) -> float:
    """Get total portfolio value."""
    portfolio = db.query(Portfolio).first()
    if portfolio:
        return portfolio.current_value or 0.0
    return 0.0


def _get_portfolio_change(db: Session) -> str:
    """Get portfolio change."""
    portfolio = db.query(Portfolio).first()
    if portfolio and portfolio.total_gain_loss_percentage:
        return f"{portfolio.total_gain_loss_percentage:.2f}%"
    return "0.0%"


def _get_daily_gain(db: Session) -> float:
    """Get daily gain/loss."""
    # Calculate based on portfolio holdings
    holdings = db.query(PortfolioHolding).all()
    total_gain = sum(h.gain_loss or 0 for h in holdings)
    return total_gain


def _get_daily_gain_percentage(db: Session) -> str:
    """Get daily gain percentage."""
    portfolio = db.query(Portfolio).first()
    if portfolio and portfolio.current_value and portfolio.current_value > 0:
        daily_gain = _get_daily_gain(db)
        pct = (daily_gain / portfolio.current_value) * 100
        return f"{pct:.2f}%"
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
            # Calculate daily change (simplified)
            prev_price = db.query(StockPrice).filter(
                StockPrice.symbol == item.symbol,
                StockPrice.timestamp < latest_price.timestamp
            ).order_by(StockPrice.timestamp.desc()).first()
            
            if prev_price:
                change = ((latest_price.close_price - prev_price.close_price) / prev_price.close_price) * 100
            else:
                change = 0.0
            
            data.append({
                "Symbol": item.symbol,
                "Price": f"R {latest_price.close_price:.2f}",
                "Change": f"{change:+.2f}%",
                "Volume": f"{latest_price.volume:,}" if latest_price.volume else "N/A"
            })
    
    if data:
        st.dataframe(data, use_container_width=True)
    else:
        st.warning("No price data available for watchlist items.")


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
            StockPrice.timestamp >= datetime.utcnow() - timedelta(days=30)
        ).order_by(StockPrice.timestamp.asc()).all()
        
        if prices:
            fig.add_trace(go.Scatter(
                x=[p.timestamp for p in prices],
                y=[p.close_price for p in prices],
                name=item.symbol,
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
