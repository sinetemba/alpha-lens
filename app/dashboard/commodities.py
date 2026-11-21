import streamlit as st
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
from app.models.stock import Stock, StockPrice
from app.dashboard.utils import format_stock_label, get_latest_market_data_timestamp, is_market_data_stale, render_last_fetch_caption
from app.services.price_queries import get_latest_and_previous_prices
from app.collectors.alpha_vantage import AlphaVantageCollector
from app.collectors.metals_dev import MetalsDevCollector
from app.collectors.yfinance import YFinanceCollector
from loguru import logger


# Commodity symbols tracked on this page: name, the pseudo-symbol their
# stored bars are keyed under, and the unit their stored close_price is
# quoted in ("g" = ZAR/gram, "mt" = ZAR/metric tonne). Metals.dev applies
# the requested unit to every metal, so under unit=g the base-metal spots
# arrive per gram; they're stored per tonne instead (the market's quote
# unit - R0.05/g reads as nothing, R 49,000/t is an LME price).
COMMODITY_SYMBOLS = {
    "gold": ("Gold", "AV_GOLD", "g"),
    "silver": ("Silver", "AV_SILVER", "g"),
    "platinum": ("Platinum", "AV_PLAT", "g"),
    "palladium": ("Palladium", "AV_PALL", "g"),
    "copper": ("Copper", "AV_COPPER", "g"),
    "aluminum": ("Aluminum", "MD_ALUM", "mt"),
    "nickel": ("Nickel", "MD_NICKEL", "mt"),
    "zinc": ("Zinc", "MD_ZINC", "mt"),
    "lead": ("Lead", "MD_LEAD", "mt"),
}
# Commodities with an Alpha Vantage history backfill - the rest build
# history from the daily Metals.dev snapshots saved on each fetch.
HISTORICAL_COMMODITIES = {"gold", "silver", "copper"}
_GRAMS_PER_TONNE = 1_000_000
_TROY_OZ_GRAMS = 31.1034768
# Grams per Alpha Vantage source unit: AV quotes gold/silver per troy oz
# and copper per metric tonne, both in USD. Stored closes are ZAR/gram, so
# the backfill converts with (USD value x USD/ZAR rate) / grams-per-unit.
_AV_GRAMS_PER_UNIT = {
    "gold": _TROY_OZ_GRAMS,
    "silver": _TROY_OZ_GRAMS,
    "copper": _GRAMS_PER_TONNE,
}


def show_commodities(db: Session):
    """Display the commodities tracking page."""
    st.header("🌍 Commodities")
    st.markdown("Track global commodity prices and recent performance.")
    render_last_fetch_caption(db, [db_sym for _, db_sym, _u in COMMODITY_SYMBOLS.values()])

    _ensure_commodity_stocks(db)

    api_collector = MetalsDevCollector()
    historical_collector = AlphaVantageCollector()
    if not api_collector.enabled:
        st.warning("Metals.dev API key is not configured. Set METALS_DEV_API_KEY in .env to enable live metal prices.")
    if not historical_collector.enabled:
        st.warning("Alpha Vantage API key is not configured. Set ALPHA_VANTAGE_API_KEY in .env to enable historical metal prices.")

    # Feedback from the post-refresh rerun: messages rendered in the same
    # run as st.rerun() are discarded before the user sees them.
    flash = st.session_state.pop("commodities_refresh_flash", None)
    if flash:
        level, message = flash
        getattr(st, level)(message)

    col1, col2 = st.columns([3, 1])
    with col2:
        if st.button("🔄 Refresh Prices"):
            with st.spinner("Fetching commodity data..."):
                saved, failures = _refresh_commodity_prices(db)
            if failures:
                st.session_state["commodities_refresh_flash"] = (
                    "warning", f"Refresh finished with {failures} failed source(s) - see logs for details."
                )
            elif saved:
                st.session_state["commodities_refresh_flash"] = (
                    "success", f"Prices refreshed ({saved} new data points saved)."
                )
            else:
                st.session_state["commodities_refresh_flash"] = ("info", "No new commodity data fetched.")
            st.rerun()

    st.markdown("---")

    with st.spinner("Loading commodity prices..."):
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

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    for api_symbol, rate in latest_rates.items():
        meta = COMMODITY_SYMBOLS.get(api_symbol)
        if not meta:
            continue
        name, yf_symbol, unit = meta
        close = rate * _GRAMS_PER_TONNE if unit == "mt" else rate

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
            existing_today.close_price = close
            # Keep the bar keyed to day-midnight like every other write path;
            # fetched_at carries the actual refresh instant.
            existing_today.timestamp = today_start
            existing_today.fetched_at = now
        else:
            db.add(StockPrice(stock_id=stock.id, symbol=yf_symbol, timestamp=today_start, close_price=close))

    try:
        db.commit()
    except IntegrityError:
        # A concurrent render wrote today's bar first - keep theirs.
        db.rollback()


def _ensure_commodity_stocks(db: Session) -> None:
    """Ensure Yahoo Finance fallback symbols exist as Stock records."""
    existing = {
        row[0] for row in db.query(Stock.symbol).filter(
            Stock.symbol.in_([db_sym for _, db_sym, _u in COMMODITY_SYMBOLS.values()])
        ).all()
    }
    for api_symbol, (name, yf_symbol, _u) in COMMODITY_SYMBOLS.items():
        if yf_symbol not in existing:
            db.add(Stock(symbol=yf_symbol, name=name))
    try:
        db.commit()
    except IntegrityError:
        # A concurrent render inserted the same symbols - keep theirs.
        db.rollback()


def _refresh_commodity_prices(db: Session) -> tuple[int, int]:
    """Fetch latest rates from Metals.dev and historical prices via Alpha Vantage.

    Returns (saved_points, failed_sources) so the UI can report honestly.
    """
    saved = 0
    failures = 0

    api_collector = MetalsDevCollector()
    if api_collector.enabled:
        try:
            latest_rates = api_collector.get_latest_rates()
            _save_latest_rates(db, latest_rates)
            if latest_rates:
                saved += len(latest_rates)
        except Exception as e:
            failures += 1
            logger.error(f"Failed to fetch latest commodity rates: {e}")

    historical_collector = AlphaVantageCollector()
    if historical_collector.enabled:
        # AV returns USD values - one rate converts every metal's backfill.
        usd_zar = YFinanceCollector().get_usd_zar()
        if not usd_zar:
            failures += 1
            logger.error("Alpha Vantage backfill skipped - USD/ZAR rate unavailable")
        else:
            for api_symbol in HISTORICAL_COMMODITIES:
                try:
                    historical_prices = historical_collector.get_commodity_history(api_symbol)
                    if historical_prices:
                        # point["timestamp"] is naive; comparing it with an aware
                        # cutoff raises TypeError and silently failed every run.
                        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=365)
                        saved += _save_historical_prices(
                            db,
                            api_symbol,
                            [point for point in historical_prices if point["timestamp"] >= cutoff],
                            usd_zar,
                        )
                except Exception as e:
                    failures += 1
                    logger.error(f"Failed to refresh historical prices for {api_symbol}: {e}")

    return saved, failures


def _save_historical_prices(db: Session, api_symbol: str, historical_prices: list[dict], usd_zar: float) -> int:
    """Save AV history converted into the stored unit (ZAR/gram).

    AV's USD-per-oz / USD-per-tonne closes share this series with Metals.dev
    ZAR-per-gram snapshots; storing them raw mixed units ~57x apart.
    """
    _, database_symbol, _unit = COMMODITY_SYMBOLS[api_symbol]
    stock = db.query(Stock).filter(Stock.symbol == database_symbol).first()
    if not stock:
        stock = Stock(symbol=database_symbol, name=COMMODITY_SYMBOLS[api_symbol][0])
        db.add(stock)
        db.flush()

    def _norm(ts):
        return ts.astimezone(timezone.utc).replace(tzinfo=None) if ts.tzinfo else ts

    # One set-membership check against the DB replaces a SELECT per data point.
    timestamps = [_norm(p["timestamp"]) for p in historical_prices if p.get("timestamp")]
    if not timestamps:
        return 0
    existing = {
        t[0] for t in db.query(StockPrice.timestamp).filter(
            StockPrice.symbol == database_symbol,
            StockPrice.timestamp.in_(timestamps),
        ).all()
    }

    saved = 0
    for point in historical_prices:
        timestamp = point.get("timestamp")
        if not timestamp:
            continue
        timestamp = _norm(timestamp)
        if timestamp in existing:
            continue
        close = point.get("close")
        if close is None or close <= 0:
            continue
        db.add(StockPrice(
            stock_id=stock.id,
            symbol=database_symbol,
            timestamp=timestamp,
            close_price=close * usd_zar / _AV_GRAMS_PER_UNIT[api_symbol],
        ))
        saved += 1

    db.commit()
    return saved


def _render_commodity_summary(db: Session, api_collector: MetalsDevCollector):
    """Render a summary table of current commodity prices."""
    st.subheader("Current Prices")

    yf_symbols = [db_sym for _, db_sym, _u in COMMODITY_SYMBOLS.values()]

    fetch_attempted = False
    latest_rates = {}
    if api_collector.enabled:
        latest_timestamp = get_latest_market_data_timestamp(db, yf_symbols)
        # Live-fetch at most once per session: every widget interaction
        # reruns this page, and a failing API would be hit on each rerun
        # (failures stay stale -> refetched forever).
        if is_market_data_stale(latest_timestamp) and not st.session_state.get("commodities_live_fetched"):
            st.session_state["commodities_live_fetched"] = True
            fetch_attempted = True
            latest_rates = api_collector.get_latest_rates() or {}
            if latest_rates:
                _save_latest_rates(db, latest_rates)

    # Queried after any just-saved rates so `latest` reflects today's write.
    # One batched lookup replaces a latest-bar query plus a previous-day
    # query per commodity.
    snapshots = get_latest_and_previous_prices(db, yf_symbols)

    if api_collector.enabled and not fetch_attempted:
        # Reuse today's already-persisted snapshot instead of hitting the
        # live API again on every page visit/navigation. Only "g" metals:
        # their stored closes share the live rate's per-gram unit, while an
        # "mt" stored close is already per tonne - reusing it as a "live"
        # rate would make the per-tonne conversion below apply twice.
        for api_symbol, (_, yf_symbol, unit) in COMMODITY_SYMBOLS.items():
            if unit == "mt":
                continue
            latest, _ = snapshots.get(yf_symbol, (None, None))
            if latest:
                latest_rates[api_symbol] = latest.close_price

    latest_prices = []
    for api_symbol, (name, yf_symbol, unit) in COMMODITY_SYMBOLS.items():
        api_rate = latest_rates.get(api_symbol)
        latest_snap, prev_snap = snapshots.get(yf_symbol, (None, None))

        price = api_rate if api_rate is not None else (latest_snap.close_price if latest_snap else None)
        if price is None:
            continue
        # Metals.dev serves every metal per gram under unit=g - convert the
        # LIVE "mt" rate to per tonne; a stored "mt" close is already per
        # tonne (converted at save time).
        if unit == "mt" and api_rate is not None:
            price *= _GRAMS_PER_TONNE

        # Compare against the last prior calendar day's stored price, whether
        # today's price came from the live Metals.dev rate or the DB fallback.
        change_pct = None
        if prev_snap and prev_snap.close_price and prev_snap.close_price > 0:
            change_pct = ((price - prev_snap.close_price) / prev_snap.close_price) * 100

        latest_prices.append({
            "Commodity": format_stock_label(api_symbol, name),
            # Stored closes are already in the stored unit (ZAR/g or ZAR/mt)
            # - the fallback belongs in the ZAR column, never USD.
            "Price (ZAR/g)": price if unit == "g" else None,
            "Price (ZAR/mt)": price if unit == "mt" else None,
            "Change %": change_pct,
            # No High/Low columns: every stored bar for these pseudo-symbols
            # carries a close only - neither source serves OHLC, so the
            # columns could never populate.
            "Source": "Metals.dev" if api_rate is not None else "Stored close",
        })

    if latest_prices:
        st.dataframe(
            latest_prices, width="stretch", hide_index=True,
            column_config={
                "Price (ZAR/g)": st.column_config.NumberColumn(format="R %.2f"),
                "Price (ZAR/mt)": st.column_config.NumberColumn(format="compact"),
                "Change %": st.column_config.NumberColumn(format="%+.2f%%"),
            },
        )
    else:
        st.info("No commodity price data available. Click Refresh Prices to fetch data.")


def _render_commodity_charts(db: Session):
    """Render historical price charts for commodities using Yahoo Finance data."""
    st.subheader("Price Charts")

    import plotly.graph_objects as go

    database_symbols = {api_symbol: db_sym for api_symbol, (_, db_sym, _u) in COMMODITY_SYMBOLS.items()}
    # Every tracked symbol is chartable: AV-backed ones have backfilled
    # history, the rest chart the accumulating Metals.dev daily snapshots.
    selected = st.multiselect(
        "Select commodities to compare",
        sorted(COMMODITY_SYMBOLS),
        default=sorted(COMMODITY_SYMBOLS),
        format_func=lambda s: format_stock_label(s, COMMODITY_SYMBOLS[s][0])
    )

    if not selected:
        st.info("Select at least one commodity to view the chart.")
        return

    fig = go.Figure()
    # StockPrice.timestamp is naive UTC - keep the filter naive to match.
    start_date = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=90)

    for api_symbol in selected:
        database_symbol = database_symbols[api_symbol]
        prices = db.query(StockPrice).filter(
            StockPrice.symbol == database_symbol,
            StockPrice.timestamp >= start_date
        ).order_by(StockPrice.timestamp.asc()).all()

        if prices:
            # Rebase each series to % change from its first visible point -
            # the sources report different units (ZAR/g, USD/oz, USD/lb) so
            # raw closes on one axis are not comparable.
            base = prices[0].close_price
            if base:
                fig.add_trace(go.Scatter(
                    x=[p.timestamp for p in prices],
                    y=[(p.close_price / base - 1) * 100 for p in prices],
                    name=format_stock_label(api_symbol, COMMODITY_SYMBOLS[api_symbol][0]),
                    mode="lines"
                ))

    fig.update_layout(
        title="90-Day Commodity Price Performance (% change)",
        xaxis_title="Date",
        yaxis_title="Change since first day (%)",
        hovermode="x unified",
        height=500
    )

    st.plotly_chart(fig, width="stretch")
