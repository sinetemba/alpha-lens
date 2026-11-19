import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from sqlalchemy import func
from sqlalchemy.orm import Session
from typing import List, Dict, Optional
from app.models.base import SessionLocal
from app.models.news import NewsArticle
from app.models.stock import Stock, StockPrice
from app.dashboard.utils import format_price, format_stock_label, style_price_columns, render_csv_download, render_last_fetch_caption, _to_display_tz
from app.services.company_news import CompanyNewsService
from datetime import datetime, timedelta, timezone
from loguru import logger


@st.cache_data(ttl=300)
def _get_active_stocks():
    """Cached list of active stocks for the selectbox (refresh every 5 minutes)."""
    db = SessionLocal()
    try:
        stocks = db.query(Stock).filter(Stock.is_active == True).all()
        return [(stock.symbol, stock.name) for stock in stocks]
    finally:
        db.close()


def show_company(db: Session):
    """Display the company view page."""
    st.header("🏢 Company View")
    st.markdown("View detailed information about a specific company.")
    
    # Symbol selection
    all_stocks = _get_active_stocks()
    stock_symbols = [s[0] for s in all_stocks]
    stock_names = {s[0]: s[1] for s in all_stocks}

    if not stock_symbols:
        st.info(
            "No stocks tracked yet. Add one on the **Watchlist** or **Portfolio** page "
            "(search by ticker or company name) and it will appear here."
        )
        return

    selected_symbol = st.selectbox(
        "Select Stock",
        stock_symbols,
        format_func=lambda s: format_stock_label(s, stock_names.get(s))
    )
    
    if not selected_symbol:
        return

    render_last_fetch_caption(db, [selected_symbol])

    # Get stock information
    stock = db.query(Stock).filter(Stock.symbol == selected_symbol).first()
    
    if not stock:
        st.error(f"Stock {selected_symbol} not found.")
        return
    
    # Display company overview
    col1, col2, col3 = st.columns(3)
    
    with col1:
        st.subheader(f"**{format_stock_label(stock.symbol, stock.name)}**")
        if stock.sector:
            st.write(f"Sector: {stock.sector}")
        if stock.industry:
            st.write(f"Industry: {stock.industry}")
    
    with col2:
        latest_price = db.query(StockPrice).filter(
            StockPrice.symbol == selected_symbol
        ).order_by(StockPrice.timestamp.desc()).first()
        
        if latest_price:
            st.metric("Current Price", format_price(latest_price.close_price))
            if latest_price.volume:
                st.metric("Volume", f"{latest_price.volume:,}")
    
    with col3:
        if stock.market_cap:
            st.metric("Market Cap", f"R {stock.market_cap:,.0f}")
        if stock.shares_outstanding:
            st.metric("Shares Outstanding", f"{stock.shares_outstanding:,.0f}")
    
    st.markdown("---")
    
    # Tabs for different views
    tab1, tab2, tab3, tab4 = st.tabs(["Price Chart", "Historical Data", "News", "Financials"])

    with tab1:
        _render_price_chart(db, selected_symbol)

    with tab2:
        _render_historical_data(db, selected_symbol)

    with tab3:
        _render_company_news(db, selected_symbol)

    with tab4:
        _render_financials(selected_symbol)

    st.markdown("---")
    _render_manage_stocks(db)


def _render_manage_stocks(db: Session):
    """Deactivate tracked stocks (bad tickers, delisted, unwanted).

    Deactivating hides a stock here and stops the updater tracking it, but
    keeps its history. A deactivated stock is reactivated automatically by
    re-adding it on the Watchlist or Portfolio page.
    """
    with st.expander("🗑️ Manage tracked stocks"):
        flash = st.session_state.pop("company_flash", None)
        if flash:
            level, message = flash
            getattr(st, level)(message)

        active = _get_active_stocks()
        names = {s[0]: s[1] for s in active}
        to_deactivate = st.multiselect(
            "Stocks to deactivate",
            [s[0] for s in active],
            format_func=lambda s: format_stock_label(s, names.get(s)),
            key="deactivate_stocks",
        )

        deactivate_key = "deactivate_pending"
        if st.button("Deactivate selected", disabled=not to_deactivate):
            st.session_state[deactivate_key] = tuple(to_deactivate)

        pending = st.session_state.get(deactivate_key)
        if pending:
            st.warning(
                f"Deactivate {len(pending)} stock(s) ({', '.join(pending)})? "
                "Their history is kept but the updater stops fetching them."
            )
            confirm_col, cancel_col = st.columns(2)
            with confirm_col:
                if st.button("Yes, deactivate", key="deactivate_confirm"):
                    db.query(Stock).filter(
                        Stock.symbol.in_(list(pending))
                    ).update({"is_active": False}, synchronize_session=False)
                    db.commit()
                    _get_active_stocks.clear()
                    st.session_state[deactivate_key] = None
                    st.session_state["company_flash"] = (
                        "success", f"Deactivated {len(pending)} stock(s)."
                    )
                    st.rerun()
            with cancel_col:
                if st.button("Cancel", key="deactivate_cancel"):
                    st.session_state[deactivate_key] = None
                    st.rerun()


_CHART_FIELDS = (
    "timestamp", "open_price", "high_price", "low_price", "close_price",
    "volume", "sma_20", "sma_50", "sma_200", "rsi", "macd", "macd_signal",
    "macd_hist", "bollinger_upper", "bollinger_middle", "bollinger_lower",
)


@st.cache_data(ttl=60, show_spinner=False, hash_funcs={Session: lambda db: id(db.bind)})
def _get_chart_prices(db: Session, symbol: str, days: int) -> List[Dict]:
    """Price rows for the chart as plain dicts, cached so widget reruns don't
    re-read (and re-ORM) up to 365 rows every interaction."""
    start_date = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    prices = db.query(StockPrice).filter(
        StockPrice.symbol == symbol,
        StockPrice.timestamp >= start_date
    ).order_by(StockPrice.timestamp.asc()).all()
    return [{field: getattr(p, field) for field in _CHART_FIELDS} for p in prices]


def _render_price_chart(db: Session, symbol: str):
    """Render price chart for a stock."""
    st.subheader("Price Chart")

    # Time period selection
    period = st.selectbox(
        "Time Period",
        ["1 Week", "1 Month", "3 Months", "6 Months", "1 Year"],
        index=2
    )

    days_map = {
        "1 Week": 7,
        "1 Month": 30,
        "3 Months": 90,
        "6 Months": 180,
        "1 Year": 365
    }

    days = days_map[period]

    stock = db.query(Stock).filter(Stock.symbol == symbol).first()
    label = format_stock_label(symbol, stock.name if stock else None)

    prices = _get_chart_prices(db, symbol, days)

    if not prices:
        st.warning(f"No price data available for {symbol} in selected period.")
        return

    timestamps = [p["timestamp"] for p in prices]

    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.45, 0.15, 0.2, 0.2],
        subplot_titles=(f"{label} Price Chart - {period}", "Volume", "RSI", "MACD")
    )

    # Row 1: Candlestick + Bollinger Bands
    fig.add_trace(go.Candlestick(
        x=timestamps,
        open=[p["open_price"] for p in prices],
        high=[p["high_price"] for p in prices],
        low=[p["low_price"] for p in prices],
        close=[p["close_price"] for p in prices],
        name=label
    ), row=1, col=1)

    if any(p["bollinger_upper"] for p in prices):
        fig.add_trace(go.Scatter(
            x=timestamps, y=[p["bollinger_upper"] for p in prices],
            name="Bollinger Upper", line=dict(color="rgba(173,204,255,0.8)", width=1)
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=timestamps, y=[p["bollinger_middle"] for p in prices],
            name="Bollinger Middle", line=dict(color="rgba(150,150,150,0.8)", width=1, dash="dot")
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=timestamps, y=[p["bollinger_lower"] for p in prices],
            name="Bollinger Lower", line=dict(color="rgba(173,204,255,0.8)", width=1),
            fill="tonexty", fillcolor="rgba(173,204,255,0.15)"
        ), row=1, col=1)

    # Row 2: Volume
    fig.add_trace(go.Bar(
        x=timestamps, y=[p["volume"] for p in prices],
        name="Volume", marker_color="rgba(100,149,237,0.7)"
    ), row=2, col=1)

    # Row 3: RSI
    if any(p["rsi"] for p in prices):
        fig.add_trace(go.Scatter(
            x=timestamps, y=[p["rsi"] for p in prices],
            name="RSI", line=dict(color="orange", width=1.5)
        ), row=3, col=1)
        fig.add_hline(y=70, line=dict(color="red", width=1, dash="dash"), row=3, col=1)
        fig.add_hline(y=30, line=dict(color="green", width=1, dash="dash"), row=3, col=1)
        fig.update_yaxes(range=[0, 100], row=3, col=1)

    # Row 4: MACD
    if any(p["macd"] for p in prices):
        fig.add_trace(go.Scatter(
            x=timestamps, y=[p["macd"] for p in prices],
            name="MACD", line=dict(color="blue", width=1.5)
        ), row=4, col=1)
        fig.add_trace(go.Scatter(
            x=timestamps, y=[p["macd_signal"] for p in prices],
            name="Signal", line=dict(color="orange", width=1.5)
        ), row=4, col=1)
        colors = [
            "green" if (p["macd_hist"] or 0) >= 0 else "red"
            for p in prices
        ]
        fig.add_trace(go.Bar(
            x=timestamps, y=[p["macd_hist"] for p in prices],
            name="Histogram", marker_color=colors
        ), row=4, col=1)

    fig.update_layout(
        height=900,
        showlegend=True,
        xaxis4_title="Date",
        yaxis_title="Price (ZAR)",
        xaxis_rangeslider_visible=False,
    )

    st.plotly_chart(fig, use_container_width=True)

    # Add moving averages if available
    st.subheader("Moving Averages")
    
    ma_col1, ma_col2, ma_col3 = st.columns(3)
    
    with ma_col1:
        if prices and prices[-1]["sma_20"]:
            st.metric("SMA 20", format_price(prices[-1]["sma_20"]))

    with ma_col2:
        if prices and prices[-1]["sma_50"]:
            st.metric("SMA 50", format_price(prices[-1]["sma_50"]))

    with ma_col3:
        if prices and prices[-1]["sma_200"]:
            st.metric("SMA 200", format_price(prices[-1]["sma_200"]))


def _render_historical_data(db: Session, symbol: str):
    """Render historical data table with pagination."""
    st.subheader("Historical Price Data")

    page_size = 50
    total = db.query(func.count(StockPrice.id)).filter(StockPrice.symbol == symbol).scalar() or 0

    if total == 0:
        st.warning(f"No historical data available for {symbol}.")
        return

    total_pages = (total + page_size - 1) // page_size
    page = st.number_input("Page", min_value=1, max_value=total_pages, value=1, step=1, key=f"hist_page_{symbol}")
    offset = (page - 1) * page_size

    prices = db.query(StockPrice).filter(
        StockPrice.symbol == symbol
    ).order_by(StockPrice.timestamp.desc()).offset(offset).limit(page_size).all()

    data = []
    for price in prices:
        data.append({
            "Date": price.timestamp.strftime("%Y-%m-%d"),
            "Open": price.open_price,
            "High": price.high_price,
            "Low": price.low_price,
            "Close": price.close_price,
            "Volume": price.volume,
        })

    # Numeric cells so column sorting works; the "R"/grouped display comes
    # from NumberColumn formats rather than pre-formatted strings.
    history_table = pd.DataFrame(data)
    # Per-row decimals keyed on the close: R 0.62, not R 0.6200.
    styled_history = style_price_columns(
        history_table.style, history_table, "Close",
        price_cols=("Open", "High", "Low", "Close"),
    )
    st.caption(f"Page {page} of {total_pages} ({total} rows, {page_size} per page)")
    st.dataframe(
        styled_history,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Open": st.column_config.NumberColumn(),
            "High": st.column_config.NumberColumn(),
            "Low": st.column_config.NumberColumn(),
            "Close": st.column_config.NumberColumn(),
            "Volume": st.column_config.NumberColumn(format="%d"),
        },
    )
    render_csv_download(history_table, "price_history", key=f"hist_csv_{symbol}_{page}")


@st.cache_data(ttl=600, show_spinner=False, hash_funcs={Session: lambda db: id(db.bind)})
def _get_company_news(db: Session, symbol: str, company_name: Optional[str]) -> List[Dict]:
    """Fetch live news for a company, cached 10 min.

    session_state caching was unbounded (never refreshed) when it hit and
    re-fetched on every rerun when it missed; a timed cache fixes both.
    """
    return CompanyNewsService(db).get_company_news(symbol, company_name) or []


@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_financials(symbol: str) -> Optional[Dict]:
    """Yahoo income statement / balance sheet / cash flow, cached 1h."""
    from app.collectors.yfinance import YFinanceCollector
    ticker = YFinanceCollector().get_ticker(symbol)
    if not ticker:
        return None
    try:
        return {
            "Income statement": ticker.income_stmt,
            "Balance sheet": ticker.balance_sheet,
            "Cash flow": ticker.cashflow,
        }
    except Exception as e:
        logger.error(f"Financials fetch failed for {symbol}: {e.__class__.__name__}")
        return None


def _render_financials(symbol: str):
    """Annual financial statements from Yahoo (columns are fiscal periods)."""
    st.subheader("Financial Statements")
    st.caption("Source: Yahoo Finance - annual figures in the company's reporting currency.")
    data = _fetch_financials(symbol)
    if not data:
        st.info("No financial statements available for this symbol.")
        return
    tabs = st.tabs(list(data))
    for tab, (label, df) in zip(tabs, data.items()):
        with tab:
            if df is None or df.empty:
                st.info(f"No {label.lower()} available.")
                continue
            st.dataframe(df, use_container_width=True)


def _render_company_news(db: Session, symbol: str):
    """Render news related to a company."""
    stock = db.query(Stock).filter(Stock.symbol == symbol).first()
    company_name = stock.name if stock else None
    
    st.subheader(f"News for {format_stock_label(symbol, company_name)}")
    
    with st.spinner("Loading company news..."):
        news = _get_company_news(db, symbol, company_name)
    
    if not news:
        archived = db.query(NewsArticle).filter(
            NewsArticle.stock_symbol == symbol
        ).order_by(NewsArticle.published_at.desc()).limit(10).all()
        
        if archived:
            st.caption("No live news found — showing archived articles from the database.")
            news = [
                {
                    "title": a.title,
                    "summary": a.summary or "",
                    "url": a.url,
                    "source": a.source,
                    "published_at": a.published_at,
                }
                for a in archived
            ]
        else:
            st.info(f"No recent news available for {symbol}.")
            return
    
    for article in news:
        published = article.get("published_at")
        published_str = _to_display_tz(published).strftime("%Y-%m-%d %H:%M") if published else "Unknown"
        with st.expander(f"**{article['title']}** - {article['source']}"):
            st.markdown(f"*Published: {published_str}*")
            st.markdown(article.get("summary", ""))
            st.markdown(f"[Read more]({article['url']})")
