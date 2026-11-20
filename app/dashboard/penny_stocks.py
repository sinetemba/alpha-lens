import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from datetime import datetime
from sqlalchemy.orm import Session
from typing import Optional
from loguru import logger

from app.collectors.yfinance import YFinanceCollector
from app.dashboard.company import _get_company_news
from app.dashboard.utils import format_price, format_stock_label, style_price_columns, _to_display_tz, render_last_fetch_caption
from app.models.news import NewsArticle
from app.models.portfolio import (
    EXCLUDED_ACCOUNT_TYPES,
    EXCLUDED_PORTFOLIO_SYMBOLS,
    PortfolioHolding,
    is_jse_tracked_account,
)
from app.models.stock import Stock
from app.models.watchlist import WatchlistItem
# The scan itself is Streamlit-free (the updater's near-52w-low alerts run it
# too); this page caches it and renders the results.
from app.services.penny_scan import (
    JSE_PENNY_CANDIDATES,
    LIQUIDITY_WINDOW,
    MIN_HISTORY_ROWS,
    PENNY_PRICE_LIMIT,
    STALE_SESSIONS,
    extra_scan_symbols,
    is_stale,
    scan_penny_stocks,
    scannable_db_stocks,
)

TOP_N = 20
SCAN_TTL_SECONDS = 1800
PROJECTION_DAYS = 30
FUNDAMENTALS_TTL_SECONDS = 6 * 3600
# Daily % move that alerts for stocks added to the watchlist from this page.
# Penny stocks routinely move 5% a day; 10% keeps the alerts meaningful.
WATCHLIST_MOVE_THRESHOLD = 10.0


def show_penny_stocks(db: Session):
    """Display the JSE penny stock screener page."""
    st.header("💰 Penny Stocks")
    st.markdown(
        f"Top {TOP_N} JSE shares trading below **R {PENNY_PRICE_LIMIT:.2f}**, ranked by a "
        "heuristic potential score (momentum, trend, RSI, volume), penalised for thin trading. "
        "Select a row to see details below the grid."
    )
    st.caption("⚠️ Screening is heuristic and speculative — not financial advice.")
    render_last_fetch_caption(db)

    # Feedback from the post-add rerun (Add to Watchlist below): messages
    # rendered in the same run as st.rerun() are discarded.
    flash = st.session_state.pop("penny_flash", None)
    if flash:
        level, message = flash
        getattr(st, level)(message)

    # Symbols actually held (JSE-tracked, real accounts only) get a ★ in the
    # Stock column below.
    held_symbols = {
        h.symbol
        for h in db.query(PortfolioHolding.symbol, PortfolioHolding.account_type).all()
        if is_jse_tracked_account(h.account_type)
        and h.account_type not in EXCLUDED_ACCOUNT_TYPES
        and h.symbol not in EXCLUDED_PORTFOLIO_SYMBOLS
    }
    db_stocks = scannable_db_stocks(db)
    extra_symbols = extra_scan_symbols(db_stocks)

    _, rescan_col = st.columns([3, 1])
    with rescan_col:
        if st.button("🔄 Rescan", key="penny_rescan"):
            _scan_penny_stocks.clear()
            st.rerun()

    with st.spinner("Scanning JSE penny stocks via Yahoo Finance..."):
        results, scanned_at, scan_error = _scan_penny_stocks(extra_symbols)

    for row in results:
        row["name"] = db_stocks.get(row["symbol"]) or JSE_PENNY_CANDIDATES.get(row["symbol"]) or row["symbol"]

    st.caption(f"Scanned at {_to_display_tz(scanned_at).strftime('%d %b %Y %H:%M')} SAST")

    if scan_error:
        st.warning(f"The penny-stock scan failed ({scan_error}) - try Rescan later.")
        return

    if not results:
        st.info("No JSE shares currently trading below the threshold were found.")
        return

    top = results[:TOP_N]
    grid_df = pd.DataFrame([{
        "Stock": ("★ " if r["symbol"] in held_symbols else "")
        + ("⏸ " if is_stale(r) else "")
        + format_stock_label(r["symbol"], r["name"]),
        "Price": r["price"],
        "Day %": r["day_change"],
        "52w Low": r["low_52w"],
        "52w High": r["high_52w"],
        "Above Low %": r["off_low"],
        "RSI": r["rsi"],
        "3m %": r["mom_3m"],
        "Traded/day": r.get("traded_value"),
        "Potential": r["score"],
    } for r in top])

    styled_grid = style_price_columns(
        grid_df.style, grid_df, "Price", price_cols=("Price", "52w Low", "52w High"),
    )
    event = st.dataframe(
        styled_grid,
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="penny_stock_grid",
        column_config={
            "Stock": st.column_config.TextColumn(
                "Stock",
                help=(
                    "Ticker and company name of the scanned share (★ = held in your portfolio, "
                    f"⏸ = no trade in the last {STALE_SESSIONS}+ sessions - Price is an old close)"
                ),
            ),
            "Price": st.column_config.NumberColumn(
                "Price",
                help="Last traded price in Rand (close from Yahoo Finance) - may be days old for ⏸ rows",
            ),
            "Day %": st.column_config.NumberColumn(
                "Day %", format="%+.2f%%",
                help="Price change vs the previous trading day's close. Blank = no trade on the latest session",
            ),
            "52w Low": st.column_config.NumberColumn(
                "52w Low",
                help="Lowest price over the past 52 weeks",
            ),
            "52w High": st.column_config.NumberColumn(
                "52w High",
                help="Highest price over the past 52 weeks",
            ),
            "Above Low %": st.column_config.NumberColumn(
                "Above Low", format="%+.1f%%",
                help="How far the current price sits above its 52-week low",
            ),
            "RSI": st.column_config.NumberColumn(
                "RSI", format="%.0f",
                help="Relative Strength Index (14-day): below 30 = oversold, above 70 = overbought",
            ),
            "3m %": st.column_config.NumberColumn(
                "3m %", format="%+.1f%%",
                help="3-month price momentum (change vs ~63 trading days ago)",
            ),
            "Traded/day": st.column_config.NumberColumn(
                "Traded R/day", format="compact",
                help=(
                    f"Median Rand value traded per day over the last {LIQUIDITY_WINDOW} sessions "
                    "(no-trade days count as R0). Low = hard to buy or sell without moving the price."
                ),
            ),
            "Potential": st.column_config.ProgressColumn(
                "Potential", min_value=0, max_value=100, format="%d",
                help=(
                    "Heuristic 0-100 upside score: MACD, RSI, price vs SMA-20/50, 3-month momentum, "
                    "recent volume - minus up to 30 points for thin trading"
                ),
            ),
        },
    )

    if any(is_stale(r) for r in top):
        st.caption(
            f"⏸ = no trade in the last {STALE_SESSIONS}+ sessions - the price shown is "
            "the last close, not a live quote."
        )

    selected_rows = event.selection.rows if event else []
    if selected_rows:
        _render_penny_detail(db, top[selected_rows[0]])
    else:
        st.caption("Click a row to view projections, influences and news.")




@st.cache_data(ttl=SCAN_TTL_SECONDS, show_spinner=False)
def _scan_penny_stocks(extra_symbols: tuple) -> tuple:
    """Cached scan_penny_stocks - Rescan clears it."""
    return scan_penny_stocks(extra_symbols)


def _render_penny_detail(db: Session, row: dict):
    """Render detail panel for the selected penny stock."""
    symbol = row["symbol"]
    label = format_stock_label(symbol, row.get("name"))

    st.markdown("---")
    st.subheader(f"🔍 {label}")

    col1, col2, col3, col4, col5 = st.columns(5)
    if row.get("day_change") is not None:
        col1.metric("Current", format_price(row["price"]), f"{row['day_change']:+.2f}% today")
    else:
        last = row.get("last_trade")
        last_text = datetime.strptime(last, "%Y-%m-%d").strftime("%d %b") if last else "over a year ago"
        col1.metric("Last Traded", format_price(row["price"]), f"on {last_text}", delta_color="off")
    col2.metric("52w Low", format_price(row["low_52w"]))
    col3.metric("52w High", format_price(row["high_52w"]))
    col4.metric("3m Momentum", f"{row['mom_3m']:+.1f}%")
    col5.metric("Potential Score", f"{row['score']}/100")

    _render_fundamentals(db, row)

    watched = db.query(WatchlistItem).filter(
        WatchlistItem.symbol == symbol, WatchlistItem.is_active == True
    ).first()
    if watched:
        st.caption("Already on your watchlist.")
    elif st.button("➕ Add to Watchlist", key=f"penny_watch_{symbol}"):
        from app.dashboard.watchlist import _add_to_watchlist
        # target/stop 0 = off, and notify_move defaults to False - without
        # these the stock was watched with no price alert of any kind.
        _add_to_watchlist(
            db, symbol, 0.0, 0.0, 0.0, "Added from Penny Stocks screen",
            notify_move=True, move_threshold=WATCHLIST_MOVE_THRESHOLD,
        )
        st.session_state["penny_flash"] = (
            "success",
            f"Added {symbol} to watchlist - alerts on a ±{WATCHLIST_MOVE_THRESHOLD:.0f}% daily move "
            "(change it in Watchlist → Edit).",
        )
        st.rerun()

    tab_projection, tab_chart, tab_news = st.tabs(["Outlook & Influences", "Price Chart", "News"])

    with tab_projection:
        _render_outlook(row)

    with tab_chart:
        fig = go.Figure(go.Scatter(
            x=row["dates"][-90:], y=row["closes"][-90:],
            mode="lines", name=symbol, line=dict(color="cornflowerblue"),
        ))
        fig.update_layout(height=350, yaxis_title="Price (ZAR)", showlegend=False, margin=dict(t=20))
        st.plotly_chart(fig, width="stretch")

    with tab_news:
        _render_penny_news(db, symbol, row.get("name"))


def _render_outlook(row: dict):
    """Render the trend projection and influence bullets for a selected stock."""
    closes = np.array(row["closes"][-90:])
    col1, col2 = st.columns([1, 2])

    with col1:
        st.markdown("**📈 Projection**")
        projection = _trend_projection(closes)
        if projection:
            upside = (projection["projected"] - row["price"]) / row["price"] * 100
            st.metric(
                f"{PROJECTION_DAYS}-day trend projection",
                format_price(projection["projected"]),
                f"{upside:+.1f}% vs current",
            )
            st.caption(
                f"±1σ range: {format_price(projection['low'])} – "
                f"{format_price(projection['high'])}"
            )
            st.caption(
                f"Trend ≈ {projection['daily_pct']:+.2f}%/day over the last "
                f"{projection['n']} trading days."
            )
        else:
            st.info("Not enough history for a projection.")
        st.caption("Simple trend extrapolation — highly speculative for penny stocks.")
        st.link_button(
            "MoneyWeb watch",
            f"https://www.moneyweb.co.za/tools-and-data/click-a-company/{row['symbol']}/",
        )

    with col2:
        st.markdown("**⚖️ Key Influences**")
        for influence in _influences(row):
            st.markdown(f"- {influence}")


def _trend_projection(closes, days: int = PROJECTION_DAYS) -> Optional[dict]:
    """Log-linear trend extrapolation with a ±1σ band.

    Fitting log(price) instead of price means a falling stock decays towards
    zero rather than projecting a negative price, and the trend is a % rate,
    which suits penny stocks better than Rand/day. sigma is the std dev of
    the fit residuals, so the band is "typical deviation from the trend" -
    not a confidence interval for the forecast.
    """
    closes = np.asarray(closes, dtype=float)
    closes = closes[np.isfinite(closes) & (closes > 0)]
    if len(closes) < MIN_HISTORY_ROWS:
        return None

    x = np.arange(len(closes))
    log_closes = np.log(closes)
    slope, intercept = np.polyfit(x, log_closes, 1)
    sigma = float(np.std(log_closes - (intercept + slope * x), ddof=2))
    centre = intercept + slope * (len(closes) - 1 + days)
    return {
        "projected": float(np.exp(centre)),
        "low": float(np.exp(centre - sigma)),
        "high": float(np.exp(centre + sigma)),
        "daily_pct": float(np.expm1(slope) * 100),
        "n": len(closes),
    }


@st.cache_data(ttl=FUNDAMENTALS_TTL_SECONDS, show_spinner=False)
def _fetch_yahoo_fundamentals(symbol: str) -> dict:
    """Yahoo profile fields for one symbol (one request, on demand).

    Raises on failure - st.cache_data does not cache exceptions, so an outage
    isn't pinned for the full TTL.
    """
    info = YFinanceCollector().get_info(symbol)
    if not isinstance(info, dict) or not info:
        raise LookupError(f"No Yahoo profile for {symbol}")
    return {
        key: info.get(key)
        for key in ("marketCap", "trailingPE", "trailingEps", "priceToBook", "sector", "industry")
    }


def _positive(value) -> Optional[float]:
    """value as a float if it is a real positive number, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if np.isfinite(value) and value > 0 else None


def _build_fundamentals(yahoo: dict, stock: Optional[Stock], price: float, div_yield: Optional[float]) -> dict:
    """Merge Yahoo's profile with the stored Stock row; missing values stay None.

    Market cap prefers Yahoo's live figure, then stored shares x current price
    (fresh without a request), then the stored cap (a one-time snapshot).
    P/E is None for loss-makers - `loss_making` says so explicitly, since a
    blank P/E would hide the most important fact about a value trap.
    """
    eps = yahoo.get("trailingEps")
    eps = float(eps) if isinstance(eps, (int, float)) and not isinstance(eps, bool) else None

    market_cap = _positive(yahoo.get("marketCap"))
    if market_cap is None and stock is not None:
        shares = _positive(stock.shares_outstanding)
        market_cap = shares * price if shares and price else _positive(stock.market_cap)

    sector = yahoo.get("sector") or (stock.sector if stock is not None else None)
    industry = yahoo.get("industry") or (stock.industry if stock is not None else None)

    return {
        "market_cap": market_cap,
        "pe": _positive(yahoo.get("trailingPE")),
        "loss_making": eps is not None and eps < 0,
        "pb": _positive(yahoo.get("priceToBook")),
        "div_yield": div_yield,
        "sector": sector if isinstance(sector, str) else None,
        "industry": industry if isinstance(industry, str) else None,
    }


def _format_rand_compact(value: Optional[float]) -> str:
    """R 438.7m / R 3.35bn style for market caps."""
    if value is None:
        return "n/a"
    if value >= 1e9:
        return f"R {value / 1e9:,.2f}bn"
    if value >= 1e6:
        return f"R {value / 1e6:,.1f}m"
    return f"R {value:,.0f}"


def _render_fundamentals(db: Session, row: dict):
    """Market cap / P/E / P/B / dividend yield for the selected stock."""
    symbol = row["symbol"]
    failed = False
    with st.spinner("Loading fundamentals..."):
        try:
            yahoo = _fetch_yahoo_fundamentals(symbol)
        except Exception as e:
            logger.warning(f"Penny stock fundamentals unavailable for {symbol}: {e}")
            yahoo, failed = {}, True
    stock = db.query(Stock).filter(Stock.symbol == symbol).first()
    f = _build_fundamentals(yahoo, stock, row["price"], row.get("div_yield"))

    if f["pe"] is not None:
        pe_text = f"{f['pe']:.1f}"
    elif f["loss_making"]:
        pe_text = "Loss-making"
    else:
        pe_text = "n/a"

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Market Cap", _format_rand_compact(f["market_cap"]),
                help="Company size. Very small caps are thinly traded and more volatile.")
    col2.metric("P/E", pe_text,
                help="Price / trailing 12-month earnings per share. Loss-making = negative earnings.")
    col3.metric("P/B", f"{f['pb']:.2f}" if f["pb"] is not None else "n/a",
                help="Price / book value per share. Below 1 = priced under its net assets.")
    col4.metric("Div Yield (12m)",
                f"{f['div_yield']:.1f}%" if f["div_yield"] is not None else "n/a",
                help="Dividends paid over the last 12 months as a % of the current price.")

    details = " · ".join(x for x in (f["sector"], f["industry"]) if x)
    if failed:
        st.caption("Yahoo profile unavailable right now - showing stored values only.")
    elif details:
        st.caption(details)


def _influences(row: dict) -> list:
    """Build human-readable signal bullets from the scanned stats."""
    influences = []
    rsi, macd, macd_sig = row["rsi"], row["macd"], row["macd_signal"]

    if rsi is not None:
        if rsi < 30:
            influences.append(f"RSI {rsi:.0f} — oversold, potential bounce setup")
        elif rsi > 70:
            influences.append(f"RSI {rsi:.0f} — overbought, pullback risk")
        else:
            influences.append(f"RSI {rsi:.0f} — neutral momentum")

    if macd is not None and macd_sig is not None:
        if macd > macd_sig:
            influences.append("MACD above signal — momentum turning positive")
        else:
            influences.append("MACD below signal — momentum still negative")

    if row["sma_20"] is not None and row["price"] > row["sma_20"]:
        influences.append(f"Price above SMA-20 ({format_price(row['sma_20'])}) — short-term uptrend")
    elif row["sma_20"] is not None:
        influences.append(f"Price below SMA-20 ({format_price(row['sma_20'])}) — short-term downtrend")

    if row["sma_50"] is not None and row["price"] > row["sma_50"]:
        influences.append(f"Price above SMA-50 ({format_price(row['sma_50'])}) — medium-term uptrend")

    influences.append(
        f"Trading {abs(row['off_high']):.0f}% below its 52-week high and "
        f"{row['off_low']:.0f}% above its 52-week low"
    )

    if row["mom_3m"] > 0:
        influences.append(f"Positive 3-month momentum ({row['mom_3m']:+.1f}%)")
    else:
        influences.append(f"Negative 3-month momentum ({row['mom_3m']:+.1f}%)")

    sessions = row.get("sessions_since_trade", 0)
    if sessions is None:
        influences.append("No trade in the past year — the price is a stale close")
    elif sessions > 0:
        last = datetime.strptime(row["last_trade"], "%Y-%m-%d").strftime("%d %b %Y")
        influences.append(
            f"Last traded {last} ({sessions} session{'s' if sessions != 1 else ''} ago) — "
            "price is not a live quote"
        )

    traded_value = row.get("traded_value")
    if traded_value is not None:
        zero_days = row.get("zero_volume_days", 0)
        no_trades = f", no trades on {zero_days} of the last {LIQUIDITY_WINDOW} days" if zero_days else ""
        penalty = row.get("liquidity_penalty", 0)
        if penalty:
            influences.append(
                f"Thinly traded — typical day R {traded_value:,.0f}{no_trades}; "
                f"hard to exit (−{penalty} score)"
            )
        else:
            influences.append(f"Liquid — typical day R {traded_value:,.0f} traded{no_trades}")

    if row["vol_base"] > 0 and row["vol_recent"] > row["vol_base"] * 1.2:
        influences.append("Recent volume rising vs 60-day average — growing interest")
    elif row["vol_base"] > 0 and row["vol_recent"] < row["vol_base"] * 0.8:
        influences.append("Recent volume declining vs 60-day average — fading interest")

    return influences


def _render_penny_news(db: Session, symbol: str, name: Optional[str]):
    """Render up to 3 latest news articles for the selected stock."""
    with st.spinner("Loading company news..."):
        news = _get_company_news(db, symbol, name)[:3]

    if not news:
        archived = db.query(NewsArticle).filter(
            NewsArticle.stock_symbol == symbol
        ).order_by(NewsArticle.published_at.desc()).limit(3).all()

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
