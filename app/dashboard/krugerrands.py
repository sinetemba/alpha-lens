import streamlit as st
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from app.models.krugerrand import KrugerrandHolding
from app.models.stock import Stock, StockPrice
from app.collectors.alpha_vantage import AlphaVantageCollector
from app.dashboard.utils import (
    get_latest_market_data_timestamp,
    holding_totals,
    is_market_data_stale,
    render_holding_totals,
    render_last_fetch_caption,
)
from loguru import logger


# GoldAPI live symbols.
GOLD_USD_LIVE_SYMBOL = "XAU_USD"
GOLD_LIVE_SYMBOL = "XAU_ZAR"
SILVER_USD_LIVE_SYMBOL = "XAG_USD"
SILVER_LIVE_SYMBOL = "XAG_ZAR"

TROY_OZ_GRAMS = 31.1034768


def _grams(g: float) -> float:
    """Grams of fine metal expressed as troy ounces."""
    return g / TROY_OZ_GRAMS


# Bullion products the page prices and tracks. Sizes map to FINE troy ounces
# per unit, so value = fine_oz x metal spot/oz - which prices 22k coins
# (a "1 oz" Krugerrand contains 1 oz of fine gold), .9999 minted bars and
# .999 silver uniformly. Silver Krugerrands are only minted as 1 oz bullion.
PRODUCT_CATALOG = {
    "gold_krugerrand": {
        "name": "Gold Krugerrand (22k)",
        "metal": "XAU",
        "sizes": {"1 oz": 1.0, "1/2 oz": 0.5, "1/4 oz": 0.25, "1/10 oz": 0.1},
    },
    "gold_bar": {
        "name": "Gold Minted Bar (.9999)",
        "metal": "XAU",
        "sizes": {
            "1 g": _grams(1), "2 g": _grams(2), "5 g": _grams(5),
            "10 g": _grams(10), "20 g": _grams(20), "1 oz": 1.0,
            "50 g": _grams(50), "100 g": _grams(100),
        },
    },
    "silver_krugerrand": {
        "name": "Silver Krugerrand (.999)",
        "metal": "XAG",
        "sizes": {"1 oz": 1.0},
    },
    "silver_bar": {
        "name": "Silver Minted Bar (.999)",
        "metal": "XAG",
        "sizes": {
            "1 oz": 1.0, "50 g": _grams(50), "100 g": _grams(100),
            "250 g": _grams(250), "500 g": _grams(500), "1 kg": _grams(1000),
        },
    },
}


def show_krugerrands(db: Session):
    """Display the dedicated Krugerrand tracking page."""
    st.header("🪙 Krugerrands & Bullion")
    st.markdown("Track gold and silver Krugerrands and minted bars at live spot prices, plus your own holdings.")
    render_last_fetch_caption(db, [GOLD_USD_LIVE_SYMBOL, GOLD_LIVE_SYMBOL])

    _ensure_spot_symbols(db)

    # Feedback from the post-action rerun (price refresh, add/remove
    # holding): messages rendered in the same run as st.rerun() are
    # discarded before the user sees them.
    flash = st.session_state.pop("krug_flash", None)
    if flash:
        level, message = flash
        getattr(st, level)(message)

    if st.button("🔄 Refresh Gold & FX Prices"):
        _get_live_spots.clear()
        with st.spinner("Fetching gold and USD/ZAR data..."):
            refreshed = _refresh_gold_prices(db)
        if refreshed:
            st.session_state["krug_flash"] = ("success", "Prices refreshed.")
        else:
            st.session_state["krug_flash"] = (
                "warning", "Could not refresh live prices - showing the last stored prices."
            )
        st.rerun()

    show_silver = st.toggle("Show Silver Spot", value=False)

    with st.spinner("Fetching latest gold prices..."):
        gold_usd, gold_zar = _get_or_refresh_gold_prices(db)
    usd_zar = (gold_zar / gold_usd) if gold_usd and gold_usd > 0 else 0.0
    _render_price_source(db)

    st.metric("USD/ZAR", f"R {usd_zar:,.2f}" if usd_zar else "N/A")

    col1, col2 = st.columns(2)

    with col1:
        st.metric("Gold Spot (USD/oz)", f"$ {gold_usd:,.2f}" if gold_usd else "N/A")

    with col2:
        st.metric(
            "Gold Spot (ZAR/oz)",
            f"R {gold_zar:,.2f}" if gold_zar else "N/A"
        )

    # Silver spot is fetched unconditionally (cached 5 min) so silver
    # products in the calculator and holdings price correctly even when the
    # display toggle is off - the toggle only controls the metric section.
    silver = _get_live_spots().get("XAG") or {}
    silver_usd, silver_zar = silver.get("usd", 0.0), silver.get("zar", 0.0)
    if silver_usd and silver_zar:
        _save_spot_prices(db, [
            (SILVER_USD_LIVE_SYMBOL, silver_usd),
            (SILVER_LIVE_SYMBOL, silver_zar),
        ])
    elif not silver_zar:
        # Same stored-bar fallback gold has - a failed fetch shouldn't leave
        # silver holdings silently unpriced.
        silver_zar = _get_latest_price(db, SILVER_LIVE_SYMBOL)[0]
    if not silver:
        st.caption(
            "Silver spot fetch failed"
            + (" - showing the last stored price." if silver_zar else " - silver prices unavailable.")
        )

    if show_silver:
        if silver_zar:
            st.markdown("---")
            st.subheader("Silver Spot")

            sc1, sc2 = st.columns(2)
            with sc1:
                st.metric("Silver Spot (USD/oz)", f"$ {silver_usd:,.2f}" if silver_usd else "N/A")
            with sc2:
                st.metric("Silver Spot (ZAR/oz)", f"R {silver_zar:,.2f}" if silver_zar else "N/A")
        else:
            st.info("Silver prices are unavailable right now (Yahoo Finance, with GoldAPI as fallback).")

    st.markdown("---")
    _render_krugerrand_calculator(gold_zar, silver_zar)

    st.markdown("---")
    _render_holdings(db, gold_zar, silver_zar)

    st.markdown("---")
    _render_historical_grid(usd_zar=usd_zar)

    if show_silver:
        st.markdown("---")
        _render_historical_grid(
            metal="XAG", title="Silver", usd_zar=usd_zar,
            sizes=PRODUCT_CATALOG["silver_krugerrand"]["sizes"],
        )


@st.cache_data(ttl=300, show_spinner=False)
def _get_live_spots() -> dict:
    """{"XAU"/"XAG": {"usd", "zar", "source"}} - cached 5 min, FAILURES TOO.

    Yahoo (COMEX futures x USD/ZAR) first: free, no quota. GoldAPI only fills
    a metal Yahoo didn't return, and is itself skipped for hours after a
    401/403/429 (collectors/gold_api.py). Caching a failed result is what
    stops a dead source being retried on every rerun.
    """
    from app.services.metal_prices import live_metal_spots
    return live_metal_spots()


def _ensure_spot_symbols(db: Session) -> None:
    """Ensure live metal spot symbols exist as Stock records."""
    symbols = {
        GOLD_USD_LIVE_SYMBOL: "Gold Spot USD (Live)",
        GOLD_LIVE_SYMBOL: "Gold Spot ZAR (Live)",
        SILVER_USD_LIVE_SYMBOL: "Silver Spot USD (Live)",
        SILVER_LIVE_SYMBOL: "Silver Spot ZAR (Live)",
    }
    for symbol, name in symbols.items():
        stock = db.query(Stock).filter(Stock.symbol == symbol).first()
        if not stock:
            stock = Stock(symbol=symbol, name=name)
            db.add(stock)
    try:
        db.commit()
    except IntegrityError:
        # A concurrent render inserted the same symbols - keep theirs.
        db.rollback()


def _render_price_source(db: Session) -> None:
    """Say where the spot came from - or that it's a stored, older price."""
    gold = _get_live_spots().get("XAU")
    if gold:
        st.caption(f"Spot: {gold['source']} - refreshed every 5 minutes while the page is open.")
        return
    _, as_of = _get_latest_price(db, GOLD_LIVE_SYMBOL)
    st.warning(
        "Live gold prices are unavailable (Yahoo Finance and GoldAPI both failed) - "
        + (f"showing the last stored price from {as_of:%d %b %Y}." if as_of else "no stored price yet.")
    )


def _save_spot_prices(db: Session, prices: list) -> None:
    """Persist today's live metal spot snapshots so they can be reused
    without refetching (net worth reads these bars too)."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    for symbol, price in prices:
        stock = db.query(Stock).filter(Stock.symbol == symbol).first()
        if not stock:
            metal = "Gold" if "XAU" in symbol else "Silver"
            stock = Stock(symbol=symbol, name=f"{metal} Spot {symbol.split('_')[-1]} (Live)")
            db.add(stock)
            db.flush()

        existing_today = db.query(StockPrice).filter(
            StockPrice.symbol == symbol,
            StockPrice.timestamp >= today_start,
        ).order_by(StockPrice.timestamp.desc()).first()
        if existing_today:
            existing_today.close_price = price
            # Keep the bar keyed to day-midnight like every other write path;
            # fetched_at carries the actual refresh instant.
            existing_today.timestamp = today_start
            existing_today.fetched_at = now
        else:
            db.add(StockPrice(stock_id=stock.id, symbol=symbol, timestamp=today_start, close_price=price))
    try:
        db.commit()
    except IntegrityError:
        # A concurrent render wrote today's bar first - keep theirs.
        db.rollback()


def _get_or_refresh_gold_prices(db: Session, force: bool = False) -> tuple[float, float]:
    """Gold USD and ZAR per troy oz: the cached live spot, else the stored bar."""
    def _stored() -> tuple[float, float]:
        return (
            _get_latest_price(db, GOLD_USD_LIVE_SYMBOL)[0],
            _get_latest_price(db, GOLD_LIVE_SYMBOL)[0],
        )

    latest_timestamp = get_latest_market_data_timestamp(db, [GOLD_LIVE_SYMBOL])
    if not force and not is_market_data_stale(latest_timestamp):
        return _stored()

    gold = _get_live_spots().get("XAU") or {}
    gold_usd, gold_zar = gold.get("usd", 0.0), gold.get("zar", 0.0)
    if gold_usd and gold_zar:
        _save_spot_prices(db, [
            (GOLD_USD_LIVE_SYMBOL, gold_usd),
            (GOLD_LIVE_SYMBOL, gold_zar),
        ])
        return gold_usd, gold_zar

    return _stored()


def _refresh_gold_prices(db: Session) -> bool:
    """Force a live refresh of gold spot. Returns True on success."""
    try:
        gold_usd, gold_zar = _get_or_refresh_gold_prices(db, force=True)
        return bool(gold_usd and gold_zar)
    except Exception as e:
        logger.error(f"Failed to refresh gold prices: {e}")
        return False


def _get_latest_price(db: Session, symbol: str) -> tuple:
    """Return the latest close price and timestamp for a symbol."""
    price = db.query(StockPrice).filter(
        StockPrice.symbol == symbol
    ).order_by(StockPrice.timestamp.desc()).first()

    if price:
        return price.close_price, price.timestamp
    return 0.0, None


def _render_krugerrand_calculator(gold_zar: float, silver_zar: float):
    """Render a value calculator for any bullion product in PRODUCT_CATALOG."""
    st.subheader("Bullion Calculator")

    premium = st.number_input(
        "Dealer premium (%)",
        min_value=0.0,
        max_value=100.0,
        value=5.0,
        step=0.5,
        help="Typical retail premium above spot. Coins and small bars carry "
             "higher premiums than large bars - adjust per product."
    )

    product_key = st.selectbox(
        "Product",
        list(PRODUCT_CATALOG.keys()),
        format_func=lambda k: PRODUCT_CATALOG[k]["name"],
    )
    product = PRODUCT_CATALOG[product_key]
    spot = gold_zar if product["metal"] == "XAU" else silver_zar

    if spot <= 0:
        # product['metal'] is the ISO code (XAU/XAG) - show the metal's
        # name, not the ticker, in user-facing text.
        metal_name = "gold" if product["metal"] == "XAU" else "silver"
        st.info(f"No live {metal_name} spot price - refresh prices above to enable the calculator.")
        return

    sizes = product["sizes"]
    size_label = st.selectbox("Size", list(sizes.keys()))
    quantity = st.number_input("Quantity", min_value=0, value=1, step=1)

    premium_factor = 1 + (premium / 100)

    st.markdown(f"**{product['name']} values by size (live)**")
    st.dataframe(
        [{
            "Size": label,
            "Fine content": f"{oz:.4f} oz" if oz < 1 else f"{oz:g} oz",
            "Spot value": f"R {oz * spot:,.2f}",
            f"Retail value ({premium:g}% premium)": f"R {oz * spot * premium_factor:,.2f}",
        } for label, oz in sizes.items()],
        width="stretch",
        hide_index=True,
    )

    fine_oz = sizes[size_label]
    per_unit_spot = fine_oz * spot
    per_unit_retail = per_unit_spot * premium_factor

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Spot value per item", f"R {per_unit_spot:,.2f}")
    with c2:
        st.metric("Est. retail per item", f"R {per_unit_retail:,.2f}")
    with c3:
        st.metric(f"Total ({quantity:g} × {size_label})", f"R {per_unit_retail * quantity:,.2f}")


def _holding_valuation(holding: KrugerrandHolding, spots: dict) -> tuple:
    """(cost, current_value) in ZAR; None where unknown (no price paid / no spot).

    Valued from the fine-oz content stored on the row, so catalogue edits
    never change what a recorded purchase is worth.
    """
    cost = holding.purchase_price * holding.quantity if holding.purchase_price else None
    spot = spots.get(holding.metal) or 0.0
    current_value = holding.fine_oz_per_unit * holding.quantity * spot if spot > 0 else None
    return cost, current_value


def _render_holdings(db: Session, gold_zar: float, silver_zar: float):
    """Persistent record of purchased bullion with live valuation."""
    st.subheader("My Holdings")

    holdings = db.query(KrugerrandHolding).order_by(KrugerrandHolding.purchase_date).all()
    spots = {"XAU": gold_zar, "XAG": silver_zar}

    if holdings:
        rows = []
        valued = []
        for h in holdings:
            cost, current_value = _holding_valuation(h, spots)
            valued.append((cost, current_value))
            gain = (current_value - cost) if current_value is not None and cost else None
            rows.append({
                "Product": PRODUCT_CATALOG.get(h.product, {}).get("name", h.product),
                "Size": h.label,
                "Qty": h.quantity,
                "Paid per item": h.purchase_price,
                "Current value": current_value,
                "Gain/Loss": gain,
                "Gain %": gain / cost * 100 if gain is not None else None,
                "Purchased": h.purchase_date.strftime("%Y-%m-%d") if h.purchase_date else "-",
            })

        render_holding_totals(holding_totals(valued))
        # Numeric cells (not "R x" strings) so column sorting works.
        st.dataframe(
            rows, width="stretch", hide_index=True,
            column_config={
                "Qty": st.column_config.NumberColumn(format="%g"),
                "Paid per item": st.column_config.NumberColumn(format="R %.2f"),
                "Current value": st.column_config.NumberColumn(format="R %.2f"),
                "Gain/Loss": st.column_config.NumberColumn(format="R %+.2f"),
                "Gain %": st.column_config.NumberColumn(format="%+.1f%%"),
            },
        )
    else:
        st.info("No bullion recorded yet - log a purchase below.")

    with st.expander("➕ Record a purchase"):
        col1, col2 = st.columns(2)
        with col1:
            product_key = st.selectbox(
                "Product",
                list(PRODUCT_CATALOG.keys()),
                format_func=lambda k: PRODUCT_CATALOG[k]["name"],
                key="krug_add_product",
            )
            size_label = st.selectbox(
                "Size",
                list(PRODUCT_CATALOG[product_key]["sizes"].keys()),
                key="krug_add_size",
            )
            quantity = st.number_input("Quantity", min_value=1, value=1, step=1, key="krug_add_qty")
        with col2:
            purchase_price = st.number_input(
                "Price paid per item (R)", min_value=0.0, value=0.0, step=100.0, key="krug_add_price",
                help="Leave 0 if unknown - value tracking still works without a cost basis.",
            )
            purchase_date = st.date_input(
                "Purchase date",
                value=datetime.now(timezone.utc).date(),
                key="krug_add_date",
            )
            notes = st.text_input("Notes (optional)", key="krug_add_notes")

        if st.button("Save purchase", key="krug_add_btn"):
            product = PRODUCT_CATALOG[product_key]
            db.add(KrugerrandHolding(
                product=product_key,
                metal=product["metal"],
                label=size_label,
                fine_oz_per_unit=product["sizes"][size_label],
                quantity=quantity,
                purchase_price=purchase_price or None,
                purchase_date=datetime.combine(purchase_date, datetime.min.time()),
                notes=notes or None,
            ))
            db.commit()
            st.session_state["krug_flash"] = (
                "success", f"Recorded {quantity:g} × {size_label} {product['name']}."
            )
            st.rerun()

    if holdings:
        remove_options = {
            f"{h.quantity:g} × {h.label} {PRODUCT_CATALOG.get(h.product, {}).get('name', h.product)} (#{h.id})": h.id
            for h in holdings
        }
        with st.expander("🗑️ Remove a holding"):
            to_remove = st.multiselect(
                "Select holdings to remove",
                list(remove_options.keys()),
                key="krug_remove_select",
            )
            remove_key = "krug_remove_pending"
            if st.button("Remove selected", key="krug_remove_btn", disabled=not to_remove):
                st.session_state[remove_key] = tuple(to_remove)

            pending = st.session_state.get(remove_key)
            if pending:
                st.warning(
                    f"Remove {len(pending)} holding(s) ({', '.join(pending)})? "
                    "This deletes the purchase record(s)."
                )
                confirm_col, cancel_col = st.columns(2)
                with confirm_col:
                    if st.button("Yes, remove", key="krug_confirm_remove"):
                        ids = {remove_options[k] for k in pending if k in remove_options}
                        for h in holdings:
                            if h.id in ids:
                                db.delete(h)
                        db.commit()
                        st.session_state[remove_key] = None
                        st.session_state["krug_flash"] = ("success", f"Removed {len(ids)} holding(s).")
                        st.rerun()
                with cancel_col:
                    if st.button("Cancel", key="krug_cancel_remove"):
                        st.session_state[remove_key] = None
                        st.rerun()


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _get_yahoo_year_ends(metal: str) -> dict[int, float]:
    """Year-end ZAR prices for the last 10 years from Yahoo - ONE request
    (replaced 10 GoldAPI calls per view). Cached 6h, an empty (failed)
    result included, so an outage isn't retried on every rerun."""
    from app.collectors.yfinance import YFinanceCollector
    return YFinanceCollector().get_metal_year_ends_zar(metal) or {}


@st.cache_data(ttl=2592000, show_spinner=False)
def _get_cached_av_year_end(metal: str = "XAU") -> dict[int, float]:
    """Year-end USD prices for the last 10 years from Alpha Vantage (fallback).

    Raises on fetch failure (exceptions are not cached, so an outage retries).
    """
    av = AlphaVantageCollector()
    if not av.enabled:
        return {}

    commodity = "gold" if metal == "XAU" else "silver"
    history = av.get_commodity_history(commodity)
    if not history:
        raise RuntimeError(f"Alpha Vantage returned no {commodity} history - likely an outage or bad key")

    current_year = datetime.now(timezone.utc).year
    prices: dict[int, float] = {}
    for year in range(current_year - 10, current_year):
        year_points = [p for p in history if p["timestamp"].year == year]
        if year_points:
            year_points.sort(key=lambda p: p["timestamp"])
            prices[year] = float(year_points[-1]["close"])
    return prices


def _get_year_end_prices_zar(metal: str, usd_zar: float) -> dict[int, float]:
    """Year-end ZAR price per year: Yahoo history, else Alpha Vantage USD converted."""
    prices = _get_yahoo_year_ends(metal)
    if prices:
        return prices
    if usd_zar <= 0:
        return {}
    # The USD/ZAR conversion is applied *after* the cache so the live cross
    # never forms part of the cache key (it changes on every refresh).
    try:
        av_prices = _get_cached_av_year_end(metal)
    except Exception as e:
        logger.warning(f"Alpha Vantage year-end fetch failed for {metal}: {e}")
        return {}
    return {year: price * usd_zar for year, price in av_prices.items()}


def _build_year_end_rows(prices: dict[int, float], sizes: dict) -> list[dict]:
    """Format year-end ZAR prices into the per-size value grid.

    `sizes` maps a label to fine troy ounces per unit - metal prices are per
    troy ounce, so the value of a size is a simple multiple of the oz price.
    """
    rows = []
    prev_price: float | None = None
    for year in sorted(prices):
        price = prices[year]
        row = {"Year": str(year)}
        for size, fine_oz in sizes.items():
            row[size] = f"R {price * fine_oz:,.2f}"

        row["YoY Growth (1 oz)"] = (
            f"{((price - prev_price) / prev_price) * 100:+.2f}%" if prev_price else "-"
        )
        rows.append(row)
        prev_price = price
    return rows


def _render_historical_grid(
    metal: str = "XAU", title: str = "Krugerrand", usd_zar: float = 1.0, sizes: dict | None = None
):
    """Render a 10-year grid of year-end values for a metal."""
    if sizes is None:
        sizes = PRODUCT_CATALOG["gold_krugerrand"]["sizes"]
    st.subheader(f"Historical Year-End {title} Values (ZAR)")

    with st.spinner(f"Loading 10-year {title.lower()} historical values..."):
        rows = _build_year_end_rows(_get_year_end_prices_zar(metal, usd_zar), sizes)

    if not rows:
        st.info("No historical data available right now (Yahoo Finance, with Alpha Vantage as fallback).")
        return

    st.dataframe(rows, width="stretch", hide_index=True)



