import streamlit as st
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from app.models.stock import Stock, StockPrice
from app.collectors.alpha_vantage import AlphaVantageCollector
from app.collectors.gold_api import GoldAPICollector
from app.dashboard.utils import get_latest_market_data_timestamp, is_market_data_stale, render_last_fetch_caption
from loguru import logger


# GoldAPI live symbols.
GOLD_USD_LIVE_SYMBOL = "XAU_USD"
GOLD_LIVE_SYMBOL = "XAU_ZAR"
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
    render_last_fetch_caption(db, [GOLD_USD_LIVE_SYMBOL, GOLD_LIVE_SYMBOL])

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

    show_silver = st.toggle("Show Silver Spot", value=False)

    gold_usd, gold_zar, gram_22k = _get_or_refresh_gold_prices(db, gold_api)
    usd_zar = (gold_zar / gold_usd) if gold_usd and gold_usd > 0 else 0.0

    st.metric("USD/ZAR", f"R {usd_zar:,.2f}" if usd_zar else "N/A")

    col1, col2 = st.columns(2)

    with col1:
        st.metric("Gold Spot (USD/oz)", f"$ {gold_usd:,.2f}" if gold_usd else "N/A")

    with col2:
        st.metric(
            "Gold Spot (ZAR/oz)",
            f"R {gold_zar:,.2f}" if gold_zar else "N/A"
        )

    gram_silver = 0.0

    if show_silver:
        silver_usd_data = gold_api.get_live_price(metal="XAG", currency="USD")
        silver_zar_data = gold_api.get_live_price(metal="XAG", currency="ZAR")

        if silver_usd_data and silver_zar_data:
            st.markdown("---")
            st.subheader("Silver Spot")
            silver_usd = float(silver_usd_data["price"]) if "price" in silver_usd_data else 0.0
            silver_zar = float(silver_zar_data["price"]) if "price" in silver_zar_data else 0.0

            sc1, sc2 = st.columns(2)
            with sc1:
                st.metric("Silver Spot (USD/oz)", f"$ {silver_usd:,.2f}" if silver_usd else "N/A")
            with sc2:
                st.metric("Silver Spot (ZAR/oz)", f"R {silver_zar:,.2f}" if silver_zar else "N/A")

            gram_silver = (silver_zar / 33.93) if silver_zar else 0.0
        else:
            st.info("Unable to fetch silver prices. Ensure GoldAPI key is configured and valid.")

    st.markdown("---")
    _render_krugerrand_calculator(gram_22k, gram_silver)

    st.markdown("---")
    _render_historical_grid(usd_zar=usd_zar)

    if show_silver:
        st.markdown("---")
        _render_historical_grid(metal="XAG", title="Silver", usd_zar=usd_zar)


def _ensure_gold_symbols(db: Session) -> None:
    """Ensure live gold spot symbols exist as Stock records."""
    symbols = {
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
    """Force a live refresh of gold spot from GoldAPI."""
    if gold_api.enabled:
        try:
            _get_or_refresh_gold_prices(db, gold_api, force=True)
        except Exception as e:
            logger.error(f"Failed to refresh gold prices from GoldAPI: {e}")


def _get_latest_price(db: Session, symbol: str) -> tuple:
    """Return the latest close price and timestamp for a symbol."""
    price = db.query(StockPrice).filter(
        StockPrice.symbol == symbol
    ).order_by(StockPrice.timestamp.desc()).first()

    if price:
        return price.close_price, price.timestamp
    return 0.0, None


def _render_krugerrand_calculator(gram_22k: float, gram_silver: float = 0.0):
    """Render a calculator for Krugerrand values across sizes, optionally with silver."""
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
    has_silver = gram_silver > 0
    for size, grams in KRUGERRAND_SIZES.items():
        spot_value = gram_22k * grams
        retail_value = spot_value * premium_factor
        row = {
            "Size": size,
            "Spot value": f"R {spot_value:,.2f}",
            f"Retail value ({premium:.0f}% premium)": f"R {retail_value:,.2f}",
            "_spot_value": spot_value,
        }
        if has_silver:
            silver_spot = gram_silver * grams
            silver_retail = silver_spot * premium_factor
            row["Silver spot value"] = f"R {silver_spot:,.2f}"
            row[f"Silver retail value ({premium:.0f}% premium)"] = f"R {silver_retail:,.2f}"
        size_data.append(row)

    st.markdown("**Value by size (live)**")
    display_columns = [
        "Size",
        "Spot value",
        f"Retail value ({premium:.0f}% premium)",
    ]
    if has_silver:
        display_columns += [
            "Silver spot value",
            f"Silver retail value ({premium:.0f}% premium)",
        ]
    st.dataframe(
        [{c: d[c] for c in display_columns} for d in size_data],
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


def _get_historical_year_end_prices(gold_api: GoldAPICollector, metal: str = "XAU", usd_zar: float = 1.0) -> list[dict]:
    """Fetch year-end metal/ZAR prices for the last 10 years."""
    if not gold_api.enabled:
        return []

    current_year = datetime.now(timezone.utc).year
    rows = []
    prev_price: float | None = None
    for year in range(current_year - 10, current_year):
        date = f"{year}1231"
        data = gold_api.get_historical_price(metal, "ZAR", date)
        if not data or "price" not in data:
            continue

        price = float(data["price"])
        # Metal prices are per troy ounce. Krugerrand sizes are denominated in
        # troy ounces of pure metal (1, 0.5, 0.25, 0.1), so the size value is a
        # simple fraction of the 1 oz price.
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

    if rows:
        return rows

    # Fallback to Alpha Vantage if GoldAPI does not support historical data.
    av = AlphaVantageCollector()
    if not av.enabled or usd_zar <= 0:
        return []

    commodity = "gold" if metal == "XAU" else "silver"
    history = av.get_commodity_history(commodity)
    if not history:
        return []

    # Convert USD year-end prices to ZAR using the current USD/ZAR cross.
    conversion = usd_zar

    prev_price = None
    for year in range(current_year - 10, current_year):
        year_points = [p for p in history if p["timestamp"].year == year]
        if not year_points:
            continue
        year_points.sort(key=lambda p: p["timestamp"])
        price_usd = float(year_points[-1]["close"])
        price = price_usd * conversion

        row = {"Year": str(year)}
        for size, grams in KRUGERRAND_SIZES.items():
            pure_oz = grams / 33.930
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
def _get_cached_historical_grid(version: int = 2, metal: str = "XAU", usd_zar: float = 1.0) -> list[dict]:
    """Cache the 10-year year-end grid for 30 days to stay within the free tier.

    The ``version`` argument is a cache-buster: bump it when the grid schema
    changes so existing cached results are ignored.
    """
    return _get_historical_year_end_prices(GoldAPICollector(), metal, usd_zar)


def _render_historical_grid(metal: str = "XAU", title: str = "Krugerrand", usd_zar: float = 1.0):
    """Render a 10-year grid of year-end values for a metal."""
    st.subheader(f"Historical Year-End {title} Values (ZAR)")

    with st.spinner(f"Loading 10-year {title.lower()} historical values..."):
        version = 2 if metal == "XAU" else 1
        rows = _get_cached_historical_grid(version=version, metal=metal, usd_zar=usd_zar)

    if not rows:
        st.info("No historical data available. Check your GoldAPI or Alpha Vantage configuration.")
        return

    st.dataframe(rows, use_container_width=True, hide_index=True)



