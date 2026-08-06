import streamlit as st
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
from typing import Optional
from app.models.stock import Stock, StockPrice
from app.dashboard.utils import format_stock_label, get_latest_market_data_timestamp, is_market_data_stale
from app.collectors.alpha_vantage import AlphaVantageCollector
from app.collectors.metals_dev import MetalsDevCollector
from loguru import logger


# Commodity symbols tracked by CommodityPriceAPI and their Yahoo Finance
# fallback symbols used for historical chart data.
COMMODITY_SYMBOLS = {
    "gold": ("Gold", "AV_GOLD"),
    "silver": ("Silver", "AV_SILVER"),
    "platinum": ("Platinum", "AV_PLAT"),
    "palladium": ("Palladium", "AV_PALL"),
    "copper": ("Copper", "AV_COPPER"),
}
HISTORICAL_COMMODITIES = {"gold", "silver", "copper"}


def show_commodities(db: Session):
    """Display the commodities tracking page."""
    st.header("🌍 Commodities")
    st.markdown("Track global commodity prices and recent performance.")

    _ensure_commodity_stocks(db)

    api_collector = MetalsDevCollector()
    historical_collector = AlphaVantageCollector()
    if not api_collector.enabled:
        st.warning("Metals.dev API key is not configured. Set METALS_DEV_API_KEY in .env to enable live metal prices.")
    if not historical_collector.enabled:
        st.warning("Alpha Vantage API key is not configured. Set ALPHA_VANTAGE_API_KEY in .env to enable historical metal prices.")

    col1, col2 = st.columns([3, 1])
    with col2:
        if st.button("🔄 Refresh Prices"):
            with st.spinner("Fetching commodity data..."):
                _refresh_commodity_prices(db)
            st.success("Prices refreshed.")
            st.rerun()

    st.markdown("---")

    _render_commodity_summary(db, api_collector)

    st.markdown("---")

    _render_commodity_charts(db)


def _save_latest_rates(db: Session, latest_rates: dict[str, float]) -> None:
    """Persist Metals.dev's latest snapshot so day-over-day change can be computed.

    Metals.dev only returns a live spot rate with no history, so without
    storing each fetch there is nothing to diff against for a daily change.
    """
    if not latest_rates:
        return

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    for api_symbol, rate in latest_rates.items():
        if api_symbol not in COMMODITY_SYMBOLS:
            continue
        name, yf_symbol = COMMODITY_SYMBOLS[api_symbol]

        stock = db.query(Stock).filter(Stock.symbol == yf_symbol).first()
        if not stock:
            stock = Stock(symbol=yf_symbol, name=name)
            db.add(stock)
            db.flush()

        existing_today = db.query(StockPrice).filter(
            StockPrice.symbol == yf_symbol,
            StockPrice.timestamp >= today_start,
        ).order_by(StockPrice.timestamp.desc()).first()

        if existing_today:
            existing_today.close_price = rate
            existing_today.timestamp = now
        else:
            db.add(StockPrice(stock_id=stock.id, symbol=yf_symbol, timestamp=now, close_price=rate))

    db.commit()


def _get_previous_day_price(db: Session, symbol: str, latest_timestamp: datetime) -> Optional[StockPrice]:
    """Get the most recent price for a symbol from a calendar day before `latest_timestamp`."""
    latest_day_start = latest_timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
    return db.query(StockPrice).filter(
        StockPrice.symbol == symbol,
        StockPrice.timestamp < latest_day_start,
    ).order_by(StockPrice.timestamp.desc()).first()


def _ensure_commodity_stocks(db: Session) -> None:
    """Ensure Yahoo Finance fallback symbols exist as Stock records."""
    for api_symbol, (name, yf_symbol) in COMMODITY_SYMBOLS.items():
        stock = db.query(Stock).filter(Stock.symbol == yf_symbol).first()
        if not stock:
            stock = Stock(symbol=yf_symbol, name=name)
            db.add(stock)
    db.commit()


def _refresh_commodity_prices(db: Session) -> None:
    """Fetch latest rates from Metals.dev and historical prices via DataService."""
    api_collector = MetalsDevCollector()

    if api_collector.enabled:
        try:
            latest_rates = api_collector.get_latest_rates()
            _save_latest_rates(db, latest_rates)
        except Exception as e:
            logger.error(f"Failed to fetch latest commodity rates: {e}")

    historical_collector = AlphaVantageCollector()
    for api_symbol in HISTORICAL_COMMODITIES:
        try:
            historical_prices = historical_collector.get_commodity_history(api_symbol)
            if historical_prices:
                cutoff = datetime.now(timezone.utc) - timedelta(days=365)
                _save_historical_prices(
                    db,
                    api_symbol,
                    [point for point in historical_prices if point["timestamp"] >= cutoff],
                )
        except Exception as e:
            logger.error(f"Failed to refresh historical prices for {api_symbol}: {e}")


def _save_historical_prices(db: Session, api_symbol: str, historical_prices: list[dict]) -> int:
    _, database_symbol = COMMODITY_SYMBOLS[api_symbol]
    stock = db.query(Stock).filter(Stock.symbol == database_symbol).first()
    if not stock:
        stock = Stock(symbol=database_symbol, name=COMMODITY_SYMBOLS[api_symbol][0])
        db.add(stock)
        db.flush()

    saved = 0
    for point in historical_prices:
        existing = db.query(StockPrice).filter(
            StockPrice.symbol == database_symbol,
            StockPrice.timestamp == point["timestamp"],
        ).first()
        if existing:
            continue
        db.add(StockPrice(
            stock_id=stock.id,
            symbol=database_symbol,
            timestamp=point["timestamp"],
            close_price=point["close"],
        ))
        saved += 1

    db.commit()
    return saved


def _load_cached_rates(db: Session) -> dict[str, float]:
    """Reload the most recently persisted rate for each commodity from StockPrice."""
    rates = {}
    for api_symbol, (_, yf_symbol) in COMMODITY_SYMBOLS.items():
        latest = db.query(StockPrice).filter(
            StockPrice.symbol == yf_symbol
        ).order_by(StockPrice.timestamp.desc()).first()
        if latest:
            rates[api_symbol] = latest.close_price
    return rates


def _render_commodity_summary(db: Session, api_collector: MetalsDevCollector):
    """Render a summary table of current commodity prices."""
    st.subheader("Current Prices")

    latest_rates = {}
    if api_collector.enabled:
        yf_symbols = [yf_symbol for _, yf_symbol in COMMODITY_SYMBOLS.values()]
        latest_timestamp = get_latest_market_data_timestamp(db, yf_symbols)
        if is_market_data_stale(latest_timestamp):
            latest_rates = api_collector.get_latest_rates() or {}
            if latest_rates:
                _save_latest_rates(db, latest_rates)
        else:
            # Reuse today's already-persisted snapshot instead of hitting the
            # live API again on every page visit/navigation.
            latest_rates = _load_cached_rates(db)

    latest_prices = []
    for api_symbol, (name, yf_symbol) in COMMODITY_SYMBOLS.items():
        api_rate = latest_rates.get(api_symbol)
        latest_db = db.query(StockPrice).filter(
            StockPrice.symbol == yf_symbol
        ).order_by(StockPrice.timestamp.desc()).first()

        price = api_rate if api_rate is not None else (latest_db.close_price if latest_db else None)
        if price is None:
            continue

        # Compare against the last prior calendar day's stored price, whether
        # today's price came from the live Metals.dev rate or the DB fallback.
        reference_timestamp = latest_db.timestamp if latest_db else datetime.now(timezone.utc)
        prev_db = _get_previous_day_price(db, yf_symbol, reference_timestamp)

        change_pct = None
        if prev_db and prev_db.close_price and prev_db.close_price > 0:
            change_pct = ((price - prev_db.close_price) / prev_db.close_price) * 100

        latest_prices.append({
            "Commodity": format_stock_label(api_symbol, name),
            "Currency": "ZAR" if api_rate is not None else "USD",
            "Price": float(price),
            "Change": float(change_pct) if change_pct is not None else float("nan"),
            "High": float(latest_db.high_price) if api_rate is None and latest_db and latest_db.high_price is not None else float("nan"),
            "Low": float(latest_db.low_price) if api_rate is None and latest_db and latest_db.low_price is not None else float("nan"),
            "Source": "Metals.dev" if api_rate is not None else "Yahoo Finance",
        })

    if latest_prices:
        st.dataframe(
            latest_prices,
            use_container_width=True,
            column_config={
                "Commodity": st.column_config.TextColumn("Commodity"),
                "Currency": st.column_config.TextColumn("Currency"),
                "Price": st.column_config.NumberColumn("Price (per gram)", format="%.2f"),
                "Change": st.column_config.NumberColumn("Change", format="%.2f%%"),
                "High": st.column_config.NumberColumn("High", format="%.2f"),
                "Low": st.column_config.NumberColumn("Low", format="%.2f"),
                "Source": st.column_config.TextColumn("Source"),
            },
        )
    else:
        st.info("No commodity price data available. Click Refresh Prices to fetch data.")


def _render_commodity_charts(db: Session):
    """Render historical price charts for commodities using Yahoo Finance data."""
    st.subheader("Price Charts")

    import plotly.graph_objects as go

    database_symbols = {api_symbol: database_symbol for api_symbol, (_, database_symbol) in COMMODITY_SYMBOLS.items()}
    selected = st.multiselect(
        "Select commodities to compare",
        sorted(HISTORICAL_COMMODITIES),
        default=sorted(HISTORICAL_COMMODITIES),
        format_func=lambda s: format_stock_label(s, COMMODITY_SYMBOLS[s][0])
    )

    if not selected:
        st.info("Select at least one commodity to view the chart.")
        return

    fig = go.Figure()
    start_date = datetime.now(timezone.utc) - timedelta(days=90)

    for api_symbol in selected:
        database_symbol = database_symbols[api_symbol]
        prices = db.query(StockPrice).filter(
            StockPrice.symbol == database_symbol,
            StockPrice.timestamp >= start_date
        ).order_by(StockPrice.timestamp.asc()).all()

        if prices:
            fig.add_trace(go.Scatter(
                x=[p.timestamp for p in prices],
                y=[p.close_price for p in prices],
                name=format_stock_label(api_symbol, COMMODITY_SYMBOLS[api_symbol][0]),
                mode="lines"
            ))

    fig.update_layout(
        title="90-Day Commodity Price Performance",
        xaxis_title="Date",
        yaxis_title="Price (source units)",
        hovermode="x unified",
        height=500
    )

    st.plotly_chart(fig, use_container_width=True)
