import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.models.stock import Stock, StockPrice
from app.models.news import NewsArticle
from app.dashboard.utils import format_stock_label
from datetime import datetime, timedelta, timezone
from loguru import logger


def show_company(db: Session):
    """Display the company view page."""
    st.header("🏢 Company View")
    st.markdown("View detailed information about a specific company.")
    
    # Symbol selection
    all_stocks = db.query(Stock).filter(Stock.is_active == True).all()
    stock_symbols = [stock.symbol for stock in all_stocks]
    stock_names = {stock.symbol: stock.name for stock in all_stocks}

    if not stock_symbols:
        st.warning("No stocks available in database.")
        return

    selected_symbol = st.selectbox(
        "Select Stock",
        stock_symbols,
        format_func=lambda s: format_stock_label(s, stock_names.get(s))
    )
    
    if not selected_symbol:
        return
    
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
            st.metric("Current Price", f"R {latest_price.close_price:.2f}")
            if latest_price.volume:
                st.metric("Volume", f"{latest_price.volume:,}")
    
    with col3:
        if stock.market_cap:
            st.metric("Market Cap", f"R {stock.market_cap:,.0f}")
        if stock.shares_outstanding:
            st.metric("Shares Outstanding", f"{stock.shares_outstanding:,.0f}")
    
    st.markdown("---")
    
    # Tabs for different views
    tab1, tab2, tab3 = st.tabs(["Price Chart", "Historical Data", "News"])
    
    with tab1:
        _render_price_chart(db, selected_symbol)
    
    with tab2:
        _render_historical_data(db, selected_symbol)
    
    with tab3:
        _render_company_news(db, selected_symbol)


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
    start_date = datetime.now(timezone.utc) - timedelta(days=days)

    stock = db.query(Stock).filter(Stock.symbol == symbol).first()
    label = format_stock_label(symbol, stock.name if stock else None)

    # Get price data
    prices = db.query(StockPrice).filter(
        StockPrice.symbol == symbol,
        StockPrice.timestamp >= start_date
    ).order_by(StockPrice.timestamp.asc()).all()
    
    if not prices:
        st.warning(f"No price data available for {symbol} in selected period.")
        return
    
    timestamps = [p.timestamp for p in prices]

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
        open=[p.open_price for p in prices],
        high=[p.high_price for p in prices],
        low=[p.low_price for p in prices],
        close=[p.close_price for p in prices],
        name=label
    ), row=1, col=1)

    if any(p.bollinger_upper for p in prices):
        fig.add_trace(go.Scatter(
            x=timestamps, y=[p.bollinger_upper for p in prices],
            name="Bollinger Upper", line=dict(color="rgba(173,204,255,0.8)", width=1)
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=timestamps, y=[p.bollinger_middle for p in prices],
            name="Bollinger Middle", line=dict(color="rgba(150,150,150,0.8)", width=1, dash="dot")
        ), row=1, col=1)
        fig.add_trace(go.Scatter(
            x=timestamps, y=[p.bollinger_lower for p in prices],
            name="Bollinger Lower", line=dict(color="rgba(173,204,255,0.8)", width=1),
            fill="tonexty", fillcolor="rgba(173,204,255,0.15)"
        ), row=1, col=1)

    # Row 2: Volume
    fig.add_trace(go.Bar(
        x=timestamps, y=[p.volume for p in prices],
        name="Volume", marker_color="rgba(100,149,237,0.7)"
    ), row=2, col=1)

    # Row 3: RSI
    if any(p.rsi for p in prices):
        fig.add_trace(go.Scatter(
            x=timestamps, y=[p.rsi for p in prices],
            name="RSI", line=dict(color="orange", width=1.5)
        ), row=3, col=1)
        fig.add_hline(y=70, line=dict(color="red", width=1, dash="dash"), row=3, col=1)
        fig.add_hline(y=30, line=dict(color="green", width=1, dash="dash"), row=3, col=1)
        fig.update_yaxes(range=[0, 100], row=3, col=1)

    # Row 4: MACD
    if any(p.macd for p in prices):
        fig.add_trace(go.Scatter(
            x=timestamps, y=[p.macd for p in prices],
            name="MACD", line=dict(color="blue", width=1.5)
        ), row=4, col=1)
        fig.add_trace(go.Scatter(
            x=timestamps, y=[p.macd_signal for p in prices],
            name="Signal", line=dict(color="orange", width=1.5)
        ), row=4, col=1)
        colors = [
            "green" if (p.macd_hist or 0) >= 0 else "red"
            for p in prices
        ]
        fig.add_trace(go.Bar(
            x=timestamps, y=[p.macd_hist for p in prices],
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
        if prices and prices[-1].sma_20:
            st.metric("SMA 20", f"R {prices[-1].sma_20:.2f}")
    
    with ma_col2:
        if prices and prices[-1].sma_50:
            st.metric("SMA 50", f"R {prices[-1].sma_50:.2f}")
    
    with ma_col3:
        if prices and prices[-1].sma_200:
            st.metric("SMA 200", f"R {prices[-1].sma_200:.2f}")


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
            "Open": f"R {price.open_price:.2f}" if price.open_price else "N/A",
            "High": f"R {price.high_price:.2f}" if price.high_price else "N/A",
            "Low": f"R {price.low_price:.2f}" if price.low_price else "N/A",
            "Close": f"R {price.close_price:.2f}",
            "Volume": f"{price.volume:,}" if price.volume else "N/A",
        })

    st.caption(f"Page {page} of {total_pages} ({total} rows, {page_size} per page)")
    st.dataframe(data, use_container_width=True)


def _render_company_news(db: Session, symbol: str):
    """Render news related to a company."""
    st.subheader(f"News for {symbol}")
    
    news = db.query(NewsArticle).filter(
        NewsArticle.stock_symbol == symbol
    ).order_by(NewsArticle.published_at.desc()).limit(20).all()
    
    if not news:
        st.info(f"No news available for {symbol}.")
        return
    
    for article in news:
        with st.expander(f"**{article.title}** - {article.source}"):
            st.markdown(f"*Published: {article.published_at.strftime('%Y-%m-%d %H:%M')}*")
            if article.sentiment:
                st.markdown(f"Sentiment: **{article.sentiment}**")
            st.markdown(article.summary)
            st.markdown(f"[Read more]({article.url})")
