import streamlit as st
import plotly.graph_objects as go
from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import joinedload, Session
from app.models.base import SessionLocal
from app.models.stock import Stock, StockPrice
from app.models.news import NewsArticle
from app.models.watchlist import WatchlistItem
from app.models.portfolio import Portfolio, PortfolioHolding
from app.config.settings import settings
from app.collectors.moneyweb import MoneywebCollector
from app.dashboard.utils import format_stock_label, PriceSnapshot, render_market_data_refresh_control
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

    watchlist_symbols = [
        item.symbol for item in db.query(WatchlistItem).filter(WatchlistItem.is_active == True).all()
    ]
    portfolio_symbols = [
        h.symbol for h in db.query(PortfolioHolding).filter(
            ~PortfolioHolding.account_type.in_(EXCLUDED_ACCOUNT_TYPES)
        ).all()
    ]
    refresh_symbols = sorted(set(watchlist_symbols + portfolio_symbols + [JSE_INDEX_YF_SYMBOL]))

    render_market_data_refresh_control(
        db,
        key="home_market_data_refresh",
        symbols=refresh_symbols,
        include_news=True,
        auto_refresh=False,
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


def _render_watchlist_summary(db: Session):
    """Render watchlist summary table."""
    watchlist_items = db.query(WatchlistItem).filter(
        WatchlistItem.is_active == True
    ).all()
    
    if not watchlist_items:
        st.info("No stocks in watchlist. Add stocks to track them.")
        return
    
    # Get latest prices for watchlist items
    prices = _get_latest_and_previous_prices(db, [item.symbol for item in watchlist_items])
    data = []
    for item in watchlist_items:
        latest_price, prev_price = prices.get(item.symbol, (None, None))

        if latest_price:
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
    """Return the top daily winners and losers from Moneyweb, falling back to locally stored prices."""
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

    # Fallback: compute from the local stock_prices table
    stocks = db.query(Stock).filter(
        Stock.is_active == True,
        Stock.symbol != JSE_INDEX_YF_SYMBOL,
    ).all()

    prices = _get_latest_and_previous_prices(db, [stock.symbol for stock in stocks])

    movers = []
    for stock in stocks:
        latest, prev = prices.get(stock.symbol, (None, None))

        if not latest:
            continue

        if not prev or not prev.close_price:
            continue

        price_move = latest.close_price - prev.close_price
        change_pct = (price_move / prev.close_price) * 100
        movers.append({
            "Symbol": format_stock_label(stock.symbol, stock.name),
            "Price": f"R {latest.close_price:.2f}",
            "Move": f"{price_move:,.2f}",
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
    """Render the top 10 winners and losers of the day side by side."""
    winners, losers = _get_daily_movers(db)

    col_winners, col_losers = st.columns(2)

    with col_winners:
        st.markdown("**🟢 Top 10 Winners**")
        if winners:
            st.dataframe(
                [{"Symbol": m["Symbol"], "Price": m["Price"], "Move": m["Move"], "Change": m["Change"]} for m in winners],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No winners found. Try refreshing market data.")

    with col_losers:
        st.markdown("**🔴 Top 10 Losers**")
        if losers:
            st.dataframe(
                [{"Symbol": m["Symbol"], "Price": m["Price"], "Move": m["Move"], "Change": m["Change"]} for m in losers],
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


@st.cache_data(
    ttl=60,
    show_spinner=False,
    hash_funcs={
        Session: lambda db: id(db.bind),
        list: lambda x: hash(tuple(sorted(x))),
    },
)
def _get_performance_chart_data(
    db: Session, symbols: list[str]
) -> dict[str, list[PriceSnapshot]]:
    """Batch-fetch 30-day price history for the requested symbols."""
    start = datetime.now(timezone.utc) - timedelta(days=30)
    rows = db.query(
        StockPrice.symbol,
        StockPrice.timestamp,
        StockPrice.close_price,
    ).filter(
        StockPrice.symbol.in_(symbols),
        StockPrice.timestamp >= start,
    ).order_by(StockPrice.symbol, StockPrice.timestamp.asc()).all()

    data: dict[str, list[PriceSnapshot]] = {}
    for row in rows:
        data.setdefault(row.symbol, []).append(
            PriceSnapshot(
                close_price=row.close_price,
                timestamp=row.timestamp,
                volume=None,
            )
        )
    return data


def _render_performance_chart(db: Session):
    """Render performance chart for watchlist stocks."""
    watchlist_items = db.query(WatchlistItem).options(
        joinedload(WatchlistItem.stock)
    ).filter(
        WatchlistItem.is_active == True
    ).limit(5).all()

    if not watchlist_items:
        st.info("Add stocks to watchlist to see performance charts.")
        return

    chart_data = _get_performance_chart_data(db, [item.symbol for item in watchlist_items])

    fig = go.Figure()

    for item in watchlist_items:
        prices = chart_data.get(item.symbol, [])
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
