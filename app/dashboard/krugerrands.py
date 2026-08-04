import streamlit as st
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
from app.models.stock import Stock, StockPrice
from app.services.data_service import DataService
from app.collectors.gold_api import GoldAPICollector
from app.dashboard.utils import get_latest_market_data_timestamp, is_market_data_stale
from loguru import logger


# GoldAPI live symbols.
GOLD_USD_LIVE_SYMBOL = "XAU_USD"
GOLD_LIVE_SYMBOL = "XAU_ZAR"
# Yahoo Finance symbol used for historical gold chart data.
GOLD_YF_SYMBOL = "GC=F"
# USD/ZAR exchange rate symbol via Yahoo Finance.
USDZAR_SYMBOL = "ZAR=X"
# Krugerrand coin sizes (label -> total grams of 22k coin).
KRUGERRAND_SIZES = {
    "1 oz": 33.930,
    "1/2 oz": 16.965,
    "1/4 oz": 8.482,
    "1/10 oz": 3.393,
}


def show_krugerrands(db: Session):
    """Display the dedicated Krugerrand tracking page."""
    st.header("🪙 Krugerrands")
    st.markdown("Track the spot-gold value of South African Krugerrands and your holdings.")

    _ensure_gold_symbols(db)

    gold_api = GoldAPICollector()
    if not gold_api.enabled:
        st.warning(
            "GoldAPI key is not configured. Set GOLD_API_KEY in .env "
            "to enable live gold prices."
        )

    if st.button("🔄 Refresh Gold & FX Prices"):
        with st.spinner("Fetching gold and USD/ZAR data..."):
            _refresh_gold_prices(db, gold_api)
        st.success("Prices refreshed.")
        st.rerun()

    col1, col2, col3 = st.columns(3)

    gold_usd, gold_zar, gram_22k = _get_or_refresh_gold_prices(db, gold_api)
    usd_zar = (gold_zar / gold_usd) if gold_usd and gold_usd > 0 else 0.0

    with col1:
        st.metric("Gold Spot (USD/oz)", f"$ {gold_usd:,.2f}" if gold_usd else "N/A")

    with col2:
        st.metric("USD/ZAR", f"R {usd_zar:,.2f}" if usd_zar else "N/A")

    with col3:
        st.metric(
            "Gold Spot (ZAR/oz)",
            f"R {gold_zar:,.2f}" if gold_zar else "N/A"
        )

    st.markdown("---")

    col_left, col_right = st.columns([1, 2])

    with col_left:
        _render_krugerrand_calculator(gram_22k)

    with col_right:
        _render_krugerrand_chart(db)

    st.markdown("---")
    _render_historical_grid()


def _ensure_gold_symbols(db: Session) -> None:
    """Ensure gold and USD/ZAR Yahoo Finance symbols exist as Stock records."""
    symbols = {
        GOLD_YF_SYMBOL: "Gold Futures",
        USDZAR_SYMBOL: "USD/ZAR",
        GOLD_USD_LIVE_SYMBOL: "Gold Spot USD (Live)",
        GOLD_LIVE_SYMBOL: "Gold Spot ZAR (Live)",
    }
    for symbol, name in symbols.items():
        stock = db.query(Stock).filter(Stock.symbol == symbol).first()
        if not stock:
            stock = Stock(symbol=symbol, name=name)
            db.add(stock)
    db.commit()


def _get_api_gold_prices(gold_api: GoldAPICollector) -> tuple[float, float, float]:
    """Return the latest gold spot prices (USD, ZAR) and ZAR per gram of 22k from GoldAPI."""
    if not gold_api.enabled:
        return 0.0, 0.0, 0.0

    usd_data = gold_api.get_live_price(metal="XAU", currency="USD")
    zar_data = gold_api.get_live_price(metal="XAU", currency="ZAR")

    gold_usd = float(usd_data["price"]) if usd_data and "price" in usd_data else 0.0
    gold_zar = float(zar_data["price"]) if zar_data and "price" in zar_data else 0.0
    gram_22k = float(zar_data["price_gram_22k"]) if zar_data and "price_gram_22k" in zar_data else 0.0

    if not gold_usd or not gold_zar:
        logger.warning("Failed to fetch gold prices from GoldAPI")

    return gold_usd, gold_zar, gram_22k


def _save_gold_prices(db: Session, gold_usd: float, gold_zar: float) -> None:
    """Persist today's live gold spot snapshots so they can be reused without refetching."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    for symbol, price in ((GOLD_USD_LIVE_SYMBOL, gold_usd), (GOLD_LIVE_SYMBOL, gold_zar)):
        stock = db.query(Stock).filter(Stock.symbol == symbol).first()
        if not stock:
            stock = Stock(symbol=symbol, name=f"Gold Spot {symbol.split('_')[-1]} (Live)")
            db.add(stock)
            db.flush()

        existing_today = db.query(StockPrice).filter(
            StockPrice.symbol == symbol,
            StockPrice.timestamp >= today_start,
        ).order_by(StockPrice.timestamp.desc()).first()
        if existing_today:
            existing_today.close_price = price
            existing_today.timestamp = now
        else:
            db.add(StockPrice(stock_id=stock.id, symbol=symbol, timestamp=now, close_price=price))
    db.commit()


def _get_or_refresh_gold_prices(
    db: Session, gold_api: GoldAPICollector, force: bool = False
) -> tuple[float, float, float]:
    """Return live gold prices in USD and ZAR plus 22k per-gram ZAR, hitting GoldAPI only when stale/forced."""
    if not gold_api.enabled:
        gold_usd = _get_latest_price(db, GOLD_USD_LIVE_SYMBOL)[0]
        gold_zar = _get_latest_price(db, GOLD_LIVE_SYMBOL)[0]
        gram_22k = gold_zar / 33.93 if gold_zar else 0.0
        return gold_usd, gold_zar, gram_22k

    latest_timestamp = get_latest_market_data_timestamp(db, [GOLD_LIVE_SYMBOL])
    if not force and not is_market_data_stale(latest_timestamp):
        gold_usd = _get_latest_price(db, GOLD_USD_LIVE_SYMBOL)[0]
        gold_zar = _get_latest_price(db, GOLD_LIVE_SYMBOL)[0]
        gram_22k = gold_zar / 33.93 if gold_zar else 0.0
        return gold_usd, gold_zar, gram_22k

    gold_usd, gold_zar, gram_22k = _get_api_gold_prices(gold_api)
    if gold_usd and gold_zar:
        _save_gold_prices(db, gold_usd, gold_zar)
        return gold_usd, gold_zar, gram_22k

    gold_usd = _get_latest_price(db, GOLD_USD_LIVE_SYMBOL)[0]
    gold_zar = _get_latest_price(db, GOLD_LIVE_SYMBOL)[0]
    gram_22k = gold_zar / 33.93 if gold_zar else 0.0
    return gold_usd, gold_zar, gram_22k


def _refresh_gold_prices(db: Session, gold_api: GoldAPICollector) -> None:
    """Fetch gold spot from GoldAPI and historical data from Yahoo Finance."""
    if gold_api.enabled:
        try:
            _get_or_refresh_gold_prices(db, gold_api, force=True)
        except Exception as e:
            logger.error(f"Failed to refresh gold prices from GoldAPI: {e}")

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


def _render_krugerrand_calculator(gram_22k: float):
    """Render a calculator for Krugerrand values across sizes."""
    st.subheader("Krugerrand Calculator")

    premium = st.number_input(
        "Dealer premium (%)",
        min_value=0.0,
        max_value=100.0,
        value=5.0,
        step=0.5,
        help="Typical retail premium above the gold spot price."
    )

    coin_size = st.selectbox("Coin size", list(KRUGERRAND_SIZES.keys()), index=0)

    quantity = st.number_input(
        "Number of Krugerrands",
        min_value=0,
        value=1,
        step=1
    )

    if gram_22k <= 0:
        st.info("Click Refresh Gold & FX Prices above to enable the calculator.")
        return

    premium_factor = 1 + (premium / 100)

    # Live values for every Krugerrand size, using the 22k per-gram spot from GoldAPI.
    size_data = []
    for size, grams in KRUGERRAND_SIZES.items():
        spot_value = gram_22k * grams
        retail_value = spot_value * premium_factor
        size_data.append({
            "Size": size,
            "Spot value": f"R {spot_value:,.2f}",
            f"Retail value ({premium:.0f}% premium)": f"R {retail_value:,.2f}",
            "_spot_value": spot_value,
        })

    st.markdown("**Value by size (live)**")
    st.dataframe(
        [{"Size": d["Size"], "Spot value": d["Spot value"], "Retail value": d[f"Retail value ({premium:.0f}% premium)"]} for d in size_data],
        use_container_width=True,
        hide_index=True,
    )

    # Selected size totals
    selected_grams = KRUGERRAND_SIZES[coin_size]
    base_zar = gram_22k * selected_grams
    per_coin = base_zar * premium_factor
    total = per_coin * quantity

    st.metric(f"Value per {coin_size} Krugerrand", f"R {per_coin:,.2f}")
    st.metric(f"Total Value ({quantity} × {coin_size})", f"R {total:,.2f}")


def _get_historical_year_end_prices(gold_api: GoldAPICollector) -> list[dict]:
    """Fetch year-end XAU/ZAR prices for the last 10 years."""
    if not gold_api.enabled:
        return []

    current_year = datetime.now(timezone.utc).year
    rows = []
    prev_price: float | None = None
    for year in range(current_year - 10, current_year):
        date = f"{year}1231"
        data = gold_api.get_historical_price("XAU", "ZAR", date)
        if not data or "price" not in data:
            continue

        price = float(data["price"])
        # GoldAPI returns price per troy ounce of fine gold. Krugerrand sizes are
        # denominated in troy ounces of pure gold (1, 0.5, 0.25, 0.1), so the
        # size value is a simple fraction of the 1 oz price.
        row = {"Year": str(year)}
        for size, grams in KRUGERRAND_SIZES.items():
            pure_oz = grams / 33.930  # exact fraction of a full Krugerrand
            row[size] = f"R {price * pure_oz:,.2f}"

        if prev_price is not None:
            growth = ((price - prev_price) / prev_price) * 100
            row["YoY Growth (1 oz)"] = f"{growth:+.2f}%"
        else:
            row["YoY Growth (1 oz)"] = "-"
        rows.append(row)
        prev_price = price
    return rows


@st.cache_data(ttl=2592000)
def _get_cached_historical_grid(version: int = 2) -> list[dict]:
    """Cache the 10-year year-end grid for 30 days to stay within the free tier.

    The ``version`` argument is a cache-buster: bump it when the grid schema
    changes so existing cached results are ignored.
    """
    return _get_historical_year_end_prices(GoldAPICollector())


def _render_historical_grid():
    """Render a 10-year grid of Krugerrand year-end values."""
    st.subheader("Historical Year-End Krugerrand Values (ZAR)")

    with st.spinner("Loading 10-year historical values..."):
        rows = _get_cached_historical_grid(version=2)

    if not rows:
        st.info("No historical data available. Add a GoldAPI key to enable.")
        return

    st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_krugerrand_chart(db: Session):
    """Render a 90-day gold price chart in ZAR."""
    st.subheader("Gold Price Trend (ZAR)")

    import plotly.graph_objects as go

    gold_prices = db.query(StockPrice).filter(
        StockPrice.symbol == GOLD_YF_SYMBOL,
        StockPrice.timestamp >= datetime.now(timezone.utc) - timedelta(days=90)
    ).order_by(StockPrice.timestamp.asc()).all()

    fx_prices = {
        p.timestamp.date(): p.close_price
        for p in db.query(StockPrice).filter(
            StockPrice.symbol == USDZAR_SYMBOL,
            StockPrice.timestamp >= datetime.now(timezone.utc) - timedelta(days=90)
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
