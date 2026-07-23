import streamlit as st
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from app.models.stock import Stock, StockPrice
from app.services.data_service import DataService
from app.collectors.commodity_price_api import CommodityPriceAPICollector
from loguru import logger


# CommodityPriceAPI symbol for gold spot.
GOLD_API_SYMBOL = "XAU"
# Yahoo Finance symbol used for historical gold chart data.
GOLD_YF_SYMBOL = "GC=F"
# USD/ZAR exchange rate symbol via Yahoo Finance.
USDZAR_SYMBOL = "ZAR=X"


def show_krugerrands(db: Session):
    """Display the dedicated Krugerrand tracking page."""
    st.header("🪙 Krugerrands")
    st.markdown("Track the spot-gold value of South African Krugerrands and your holdings.")

    _ensure_gold_symbols(db)

    api_collector = CommodityPriceAPICollector()
    if not api_collector.enabled:
        st.warning(
            "CommodityPriceAPI key is not configured. Set COMMODITY_PRICE_API_KEY in .env "
            "to enable live gold prices."
        )

    if st.button("🔄 Refresh Gold & FX Prices"):
        with st.spinner("Fetching gold and USD/ZAR data..."):
            _refresh_gold_prices(db, api_collector)
        st.success("Prices refreshed.")
        st.rerun()

    col1, col2, col3 = st.columns(3)

    gold_usd = _get_api_gold_price(api_collector)
    usd_zar, _ = _get_latest_price(db, USDZAR_SYMBOL)

    with col1:
        st.metric("Gold Spot (USD/oz)", f"$ {gold_usd:,.2f}" if gold_usd else "N/A")

    with col2:
        st.metric("USD/ZAR", f"R {usd_zar:,.2f}" if usd_zar else "N/A")

    with col3:
        st.metric(
            "Gold Spot (ZAR/oz)",
            f"R {gold_usd * usd_zar:,.2f}" if gold_usd and usd_zar else "N/A"
        )

    st.markdown("---")

    col_left, col_right = st.columns([1, 2])

    with col_left:
        _render_krugerrand_calculator(gold_usd, usd_zar)

    with col_right:
        _render_krugerrand_chart(db)


def _ensure_gold_symbols(db: Session) -> None:
    """Ensure gold and USD/ZAR Yahoo Finance symbols exist as Stock records."""
    symbols = {GOLD_YF_SYMBOL: "Gold Futures", USDZAR_SYMBOL: "USD/ZAR"}
    for symbol, name in symbols.items():
        stock = db.query(Stock).filter(Stock.symbol == symbol).first()
        if not stock:
            stock = Stock(symbol=symbol, name=name)
            db.add(stock)
    db.commit()


def _get_api_gold_price(api_collector: CommodityPriceAPICollector) -> float:
    """Return the latest gold spot price in USD from CommodityPriceAPI."""
    if not api_collector.enabled:
        return 0.0

    rates = api_collector.get_latest_rates([GOLD_API_SYMBOL])
    if rates and GOLD_API_SYMBOL in rates:
        return float(rates[GOLD_API_SYMBOL])

    logger.warning("Failed to fetch gold price from CommodityPriceAPI")
    return 0.0


def _refresh_gold_prices(db: Session, api_collector: CommodityPriceAPICollector) -> None:
    """Fetch gold spot from CommodityPriceAPI and historical data from Yahoo Finance."""
    if api_collector.enabled:
        try:
            _get_api_gold_price(api_collector)
        except Exception as e:
            logger.error(f"Failed to refresh gold price from CommodityPriceAPI: {e}")

    data_service = DataService(db)
    for symbol in (GOLD_YF_SYMBOL, USDZAR_SYMBOL):
        try:
            data_service.update_historical_data(symbol, days=180)
        except Exception as e:
            logger.error(f"Failed to refresh {symbol}: {e}")


def _get_latest_price(db: Session, symbol: str) -> tuple:
    """Return the latest close price and timestamp for a symbol."""
    price = db.query(StockPrice).filter(
        StockPrice.symbol == symbol
    ).order_by(StockPrice.timestamp.desc()).first()

    if price:
        return price.close_price, price.timestamp
    return 0.0, None


def _render_krugerrand_calculator(gold_usd: float, usd_zar: float):
    """Render a calculator for individual Krugerrand value."""
    st.subheader("Krugerrand Calculator")

    premium = st.number_input(
        "Dealer premium (%)",
        min_value=0.0,
        max_value=100.0,
        value=5.0,
        step=0.5,
        help="Typical retail premium above the gold spot price."
    )

    quantity = st.number_input(
        "Number of Krugerrands",
        min_value=0,
        value=1,
        step=1
    )

    if gold_usd <= 0 or usd_zar <= 0:
        st.info("Click Refresh Gold & FX Prices above to enable the calculator.")
        return

    base_zar = gold_usd * usd_zar
    premium_factor = 1 + (premium / 100)
    per_coin = base_zar * premium_factor
    total = per_coin * quantity

    st.metric("Value per Krugerrand", f"R {per_coin:,.2f}")
    st.metric(f"Total Value ({quantity} coin{'s' if quantity != 1 else ''})", f"R {total:,.2f}")


def _render_krugerrand_chart(db: Session):
    """Render a 90-day gold price chart in ZAR."""
    st.subheader("Gold Price Trend (ZAR)")

    import plotly.graph_objects as go

    gold_prices = db.query(StockPrice).filter(
        StockPrice.symbol == GOLD_YF_SYMBOL,
        StockPrice.timestamp >= datetime.utcnow() - timedelta(days=90)
    ).order_by(StockPrice.timestamp.asc()).all()

    fx_prices = {
        p.timestamp.date(): p.close_price
        for p in db.query(StockPrice).filter(
            StockPrice.symbol == USDZAR_SYMBOL,
            StockPrice.timestamp >= datetime.utcnow() - timedelta(days=90)
        ).all()
    }

    if not gold_prices or not fx_prices:
        st.info("No gold or FX data available. Click Refresh Gold & FX Prices to fetch data.")
        return

    x_vals = []
    y_vals = []
    for gp in gold_prices:
        fx_rate = fx_prices.get(gp.timestamp.date())
        if not fx_rate:
            # Find nearest available FX rate
            nearest = min(
                fx_prices.items(),
                key=lambda item: abs((item[0] - gp.timestamp.date()).days)
            )
            fx_rate = nearest[1]
        x_vals.append(gp.timestamp)
        y_vals.append(gp.close_price * fx_rate)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=x_vals,
        y=y_vals,
        name="Gold Spot in ZAR",
        mode="lines",
        line=dict(color="#FFD700")
    ))

    fig.update_layout(
        title="90-Day Gold Spot Price (ZAR per ounce)",
        xaxis_title="Date",
        yaxis_title="ZAR / oz",
        hovermode="x unified",
        height=450
    )

    st.plotly_chart(fig, use_container_width=True)
