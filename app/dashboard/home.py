import pandas as pd
import streamlit as st
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.models.base import SessionLocal
from app.models.stock import Stock, StockPrice
from app.models.news import NewsArticle
from app.models.portfolio import Portfolio, PortfolioHolding, is_jse_tracked_account
from app.models.watchlist import WatchlistItem
from app.models.notification import Notification
from app.config.settings import settings
from app.collectors.moneyweb import MoneywebCollector
from app.dashboard.utils import (
    format_stock_label,
    style_price_columns,
    PriceSnapshot,
    get_latest_news_timestamp,
    render_market_data_refresh_control,
    render_updater_status_banner,
    format_news_age,
    _to_display_tz,
)
from app.dashboard.portfolio import EXCLUDED_ACCOUNT_TYPES, EXCLUDED_PORTFOLIO_SYMBOLS
from app.services.net_worth import compute_net_worth
from app.services.price_queries import get_latest_and_previous_prices



# Market-data providers behind JSE prices/history; Twelve Data only when enabled.
_MARKET_DATA = "Twelve Data, Yahoo Finance" if settings.twelve_data_enabled else "Yahoo Finance"

# Data sources shown per page in the top-right header.
PAGE_DATA_SOURCES = {
    "Home": f"Moneyweb (top movers), {_MARKET_DATA}, Luno",
    "Watchlist": _MARKET_DATA,
    "Portfolio": f"EasyEquities, {_MARKET_DATA}",
    "Dividends": "Yahoo Finance, Moneyweb",
    "IPOs": "Moneyweb SENS",
    "Notifications": "Updater alerts",
    "Company View": _MARKET_DATA,
    "Penny Stocks": "Yahoo Finance, RSS feeds",
    "Commodities": "Metals.dev, Yahoo Finance",
    "Krugerrands": "GoldAPI",
    "Crypto": "Luno, CoinGecko",
    "Settings": "Local configuration",
}


def _should_fetch_news(db: Session) -> bool:
    """Only fetch news if it has not already been fetched today."""
    latest = get_latest_news_timestamp(db)
    if latest is None:
        return True
    latest = latest if latest.tzinfo else latest.replace(tzinfo=timezone.utc)
    return _to_display_tz(latest).date() < _to_display_tz(datetime.now(timezone.utc)).date()


def _lazy(module: str, function: str):
    """Page renderer imported on first use (keeps app start-up light)."""
    def render(db: Session):
        from importlib import import_module
        getattr(import_module(f"app.dashboard.{module}"), function)(db)
    return render


# (title, icon, url path, renderer(db)) - order is the sidebar order.
_PAGES = [
    ("Home", "🏠", "home", lambda db: _render_home(db)),
    ("Watchlist", "📋", "watchlist", _lazy("watchlist", "show_watchlist")),
    ("Portfolio", "💼", "portfolio", _lazy("portfolio", "show_portfolio")),
    ("Dividends", "💰", "dividends", _lazy("dividends", "show_dividends")),
    ("IPOs", "🚀", "ipos", _lazy("upcoming_ipos", "show_upcoming_ipos")),
    ("Notifications", "🔔", "notifications", _lazy("notifications", "show_notifications")),
    ("Company View", "🏢", "company", _lazy("company", "show_company")),
    ("Penny Stocks", "🪙", "penny-stocks", _lazy("penny_stocks", "show_penny_stocks")),
    ("Commodities", "🛢️", "commodities", _lazy("commodities", "show_commodities")),
    ("Krugerrands", "🥇", "krugerrands", _lazy("krugerrands", "show_krugerrands")),
    ("Crypto", "💎", "crypto", _lazy("crypto", "show_crypto")),
    ("Settings", "⚙️", "settings", _lazy("settings_status", "show_settings")),
]


def _page(db: Session, title: str, icon: str, url_path: str, render, default: bool = False):
    def run():
        render(db)
    return st.Page(run, title=title, icon=icon, url_path=url_path, default=default)


def show_home():
    """Display the home dashboard.

    st.set_page_config lives in main.py (the entry point) - it must be the
    first Streamlit command and the cache_resource init call runs first here.
    """
    # Initialize database session
    db = SessionLocal()

    try:
        # One URL per page (e.g. /portfolio), so pages can be bookmarked and
        # the browser back button works. Each page renders against this run's
        # session; url_path doubles as the page identity.
        pages = [
            _page(db, title, icon, url_path, render, default=(url_path == "home"))
            for title, icon, url_path, render in _PAGES
        ]
        current = st.navigation(pages)

        unread_count = db.query(Notification).filter(Notification.is_read == False).count()
        if unread_count:
            st.sidebar.caption(f"🔔 {unread_count} unread notification{'s' if unread_count != 1 else ''}")

        # Page-specific header with data sources
        data_source = PAGE_DATA_SOURCES.get(current.title, "Multiple sources")
        header_col, source_col = st.columns([4, 1])
        with header_col:
            st.title("JSE Stock Analysis Platform")
        with source_col:
            st.markdown(
                f"<p style='text-align: right; font-size: small; color: #888; margin: 0;'>"
                f"Data: {data_source}"
                f"</p>",
                unsafe_allow_html=True,
            )
        st.markdown("---")

        with st.spinner("Loading page..."):
            current.run()

    finally:
        db.close()


@st.cache_data(ttl=300, show_spinner=False)
def _net_worth(jse_value: float):
    """JSE + crypto + bullion (services/net_worth), cached 5 min - the crypto
    and metal prices are network calls, and the JSE value keys the cache."""
    db = SessionLocal()
    try:
        return compute_net_worth(db, jse_value=jse_value)
    finally:
        db.close()


def _render_home(db: Session):
    """Render the home page content."""
    # Ensure JSE index symbol exists in the database
    if not db.query(Stock).filter(Stock.symbol == settings.jse_index_yf_symbol).first():
        db.add(Stock(symbol=settings.jse_index_yf_symbol, name="JSE All Share Index"))
        try:
            db.commit()
        except IntegrityError:
            # The updater's _ensure_stock inserted it concurrently - keep theirs.
            db.rollback()

    watchlist_symbols = [
        item.symbol for item in db.query(WatchlistItem).filter(WatchlistItem.is_active == True).all()
    ]
    # Queried once and shared by the refresh control and every metric helper -
    # previously each helper ran the same holdings query again.
    holdings = _portfolio_holdings(db)
    refresh_symbols = sorted(set(
        watchlist_symbols + [h.symbol for h in holdings] + [settings.jse_index_yf_symbol]
    ))

    render_updater_status_banner(db)
    _render_home_refresh_control(db, refresh_symbols)

    # Get latest market data
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric(
            label="JSE Index",
            value=_get_jse_index(db),
            delta=_get_jse_change(db)
        )

    with col2:
        if holdings:
            st.metric(
                label="Portfolio Value",
                value=f"R {_holdings_market_value(db, holdings):,.2f}",
                delta=_get_portfolio_change(db, holdings)
            )
        else:
            # "R 0.00" looked like a real value; make the empty state explicit.
            st.metric(label="Portfolio Value", value="—")
            st.caption("Add holdings on the Portfolio page.")

    with col3:
        if holdings:
            st.metric(
                label="Daily Gain",
                value=f"R {_get_daily_gain(db, holdings):,.2f}",
                delta=_get_daily_gain_percentage(db, holdings)
            )
        else:
            st.metric(label="Daily Gain", value="—")

    with col4:
        jse_value = _holdings_market_value(db, holdings) if holdings else 0.0
        worth = _net_worth(round(jse_value, 2))
        parts = [("JSE", worth.jse), ("Crypto", worth.crypto), ("Bullion", worth.bullion)]
        st.metric(
            label="Net Worth",
            value=f"R {worth.total:,.2f}" if worth.total else "—",
            help=" · ".join(f"{name} R {v:,.2f}" for name, v in parts if v)
            or "Add holdings on the Portfolio, Crypto or Krugerrands pages.",
        )
        if worth.missing:
            st.caption(f"Excludes {' and '.join(worth.missing)} (prices unavailable right now).")

    st.markdown("---")

    # Top movers for the day
    st.subheader("🚀 Top Movers Today")
    _render_top_movers(db)

    st.markdown("---")

    # Market news
    st.subheader("📰 Market News")
    _render_news_feed(db)


def _render_home_refresh_control(db: Session, symbols: list[str]) -> None:
    """Render the shared blocking refresh control plus the news-fetch age.

    Uses the same spinner/result/quota control as the other data pages - a
    fire-and-forget background refresh gave no feedback and left the user
    guessing whether anything happened.
    """
    fetch_news = _should_fetch_news(db)
    render_market_data_refresh_control(
        db, key="home_market_data_refresh", symbols=symbols, include_news=fetch_news
    )
    latest_news_ts = get_latest_news_timestamp(db)
    st.caption(format_news_age(latest_news_ts))


@st.cache_data(ttl=60, show_spinner=False, hash_funcs={Session: lambda db: id(db.bind)})
def _get_jse_index(db: Session) -> str:
    """Get current JSE index value from the database."""
    latest = db.query(StockPrice).filter(
        StockPrice.symbol == settings.jse_index_yf_symbol
    ).order_by(StockPrice.timestamp.desc()).first()

    if latest and latest.close_price:
        return f"{latest.close_price:,.0f}"
    return "N/A"


@st.cache_data(ttl=60, show_spinner=False, hash_funcs={Session: lambda db: id(db.bind)})
def _get_jse_change(db: Session) -> str:
    """Get JSE index daily percentage change."""
    prices = _get_latest_and_previous_prices(db, [settings.jse_index_yf_symbol])
    latest, prev = prices.get(settings.jse_index_yf_symbol, (None, None))

    if not latest:
        return "N/A"

    if prev and prev.close_price and prev.close_price > 0:
        change_pct = ((latest.close_price - prev.close_price) / prev.close_price) * 100
        return f"{change_pct:+.2f}%"
    return "N/A"


def _portfolio_holdings(db: Session) -> list:
    """JSE-tracked holdings in the primary portfolio, minus exclusions.

    The Home metrics are Rand-denominated like the updater's tracked set, so
    USD-account holdings (bare US symbols priced in dollars) and excluded
    pseudo-symbols must not leak into them - a dollar `current_value` summed
    into an "R" total, or a bare US ticker colliding with a JSE bar, both
    produce quietly wrong metrics.
    """
    portfolio = db.query(Portfolio).first()
    if not portfolio:
        return []
    return [
        h for h in db.query(PortfolioHolding).filter(
            PortfolioHolding.portfolio_id == portfolio.id,
            ~PortfolioHolding.account_type.in_(EXCLUDED_ACCOUNT_TYPES),
            ~PortfolioHolding.symbol.in_(EXCLUDED_PORTFOLIO_SYMBOLS),
        ).all()
        if is_jse_tracked_account(h.account_type)
    ]


def _holdings_market_value(db: Session, holdings) -> float:
    """Value holdings at their latest stored market close.

    holding.current_value is only refreshed by an EasyEquities sync or a
    Portfolio-page visit; the bars the updater writes are fresher, so the Home
    metrics stay current without requiring either.
    """
    prices = _get_latest_and_previous_prices(db, [h.symbol for h in holdings])
    total = 0.0
    for h in holdings:
        latest, _ = prices.get(h.symbol, (None, None))
        if latest and latest.close_price is not None and h.quantity:
            total += latest.close_price * h.quantity
        else:
            total += h.current_value or 0.0
    return total


def _get_portfolio_change(db: Session, holdings: list) -> str:
    """Get portfolio gain/loss percentage, excluding demo account types."""
    if not holdings:
        return "0.0%"
    total_value = _holdings_market_value(db, holdings)
    total_cost = sum(h.quantity * h.purchase_price for h in holdings if h.quantity and h.purchase_price)
    pct = (
        ((total_value - total_cost) / total_cost) * 100
        if total_cost else 0.0
    )
    return f"{pct:.2f}%"


def _get_daily_gain(db: Session, holdings: list) -> float:
    """Get daily portfolio gain/loss by comparing current prices to previous day's close."""
    if not holdings:
        return 0.0

    prices = _get_latest_and_previous_prices(db, [h.symbol for h in holdings])
    daily_gain = 0.0

    for h in holdings:
        if not h.quantity:
            continue
        latest, prev = prices.get(h.symbol, (None, None))
        if not latest:
            continue
        if prev and prev.close_price:
            daily_gain += (latest.close_price - prev.close_price) * h.quantity

    return daily_gain


def _get_daily_gain_percentage(db: Session, holdings: list) -> str:
    """Get daily gain as a percentage of portfolio value."""
    total_value = _holdings_market_value(db, holdings)
    if total_value > 0:
        daily_gain = _get_daily_gain(db, holdings)
        pct = (daily_gain / total_value) * 100
        return f"{pct:+.2f}%"
    return "0.0%"


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
    """Cached wrapper around the shared latest/previous-day price query."""
    return get_latest_and_previous_prices(db, symbols)


@st.cache_data(ttl=60, show_spinner=False, hash_funcs={Session: lambda db: id(db.bind)})
def _get_daily_movers(db: Session, limit: int = 10) -> Optional[tuple[list, list]]:
    """Return the top daily winners and losers from Moneyweb, or None on failure."""
    collector = MoneywebCollector()
    result = collector.get_winners_and_losers()

    if result is None:
        return None

    winners, losers = result

    # Prefer full company names from the stocks table over Moneyweb's
    # short names (which are sometimes just the share code, e.g. MTU/PHP).
    db_names = dict(
        db.query(Stock.symbol, Stock.name)
        .filter(Stock.symbol.in_({m["Symbol"] for m in winners + losers}))
        .all()
    )

    def _mover_rows(movers: list) -> list:
        return [{
            "Symbol": format_stock_label(m["Symbol"], db_names.get(m["Symbol"]) or m.get("Name")),
            "Price": m["Price"],
            "Move": m["Move"],
            "Change": m["Change"],
        } for m in movers[:limit]]

    return _mover_rows(winners), _mover_rows(losers)


def _render_top_movers(db: Session):
    """Render the top 10 winners and losers of the day side by side."""
    movers = _get_daily_movers(db)

    if movers is None:
        st.warning(
            "Couldn't fetch today's movers from Moneyweb (public feed - no "
            "login needed). Check connectivity or try again later."
        )
        return

    winners, losers = movers

    col_winners, col_losers = st.columns(2)

    column_config = {
        "Price": st.column_config.NumberColumn("Price"),
        "Move": st.column_config.NumberColumn("Move"),
        "Change": st.column_config.NumberColumn("Change", format="%.2f%%"),
    }

    def _movers_table(movers: list):
        # Per-row decimals: one R0.12 share must not put R210 shares on 4.
        df = pd.DataFrame(
            [{"Symbol": m["Symbol"], "Price": m["Price"], "Move": m["Move"], "Change": m["Change"]}
             for m in movers]
        )
        return style_price_columns(
            df.style, df, "Price", price_cols=("Price",), signed_cols=("Move",),
        )

    with col_winners:
        st.markdown("**🟢 Top 10 Winners**")
        if winners:
            st.dataframe(
                _movers_table(winners),
                use_container_width=True,
                hide_index=True,
                column_config=column_config,
            )
        else:
            st.info("No winners reported by Moneyweb today.")

    with col_losers:
        st.markdown("**🔴 Top 10 Losers**")
        if losers:
            st.dataframe(
                _movers_table(losers),
                use_container_width=True,
                hide_index=True,
                column_config=column_config,
            )
        else:
            st.info("No losers reported by Moneyweb today.")


def _render_news_feed(db: Session):
    """Render recent news feed."""
    recent_news = db.query(NewsArticle).order_by(
        NewsArticle.published_at.desc()
    ).limit(10).all()

    if not recent_news:
        st.info("No recent news available.")
        return

    for article in recent_news:
        published = (
            _to_display_tz(article.published_at).strftime("%Y-%m-%d %H:%M")
            if article.published_at else "Unknown"
        )
        with st.expander(f"**{article.title}** - {article.source}"):
            st.markdown(f"*Published: {published}*")
            st.markdown(article.summary)
            st.markdown(f"[Read more]({article.url})")
