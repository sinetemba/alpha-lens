import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from typing import Optional
from loguru import logger

from app.analytics.technical_indicators import TechnicalIndicators
from app.collectors.yfinance import JSE_PRICE_SCALE, YFinanceCollector
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

PENNY_PRICE_LIMIT = 5.00
TOP_N = 20
SCAN_TTL_SECONDS = 1800
PROJECTION_DAYS = 30
MIN_HISTORY_ROWS = 30
FUNDAMENTALS_TTL_SECONDS = 6 * 3600
# A trailing yield above this is a cents/Rand unit glitch, not a real payout.
DIVIDEND_YIELD_CEILING = 50.0

# JSE-listed small caps and penny stocks that frequently trade below R5.
# The live scan still filters by actual price, so listings that have moved
# above the threshold simply drop off the grid. Symbols that no longer
# resolve on Yahoo Finance are skipped silently.
JSE_PENNY_CANDIDATES = {
    "ACL": "ArcelorMittal South Africa",
    "ADR": "Adcorp Holdings",
    "AEG": "Aveng",
    "APF": "Accelerate Property Fund",
    "ART": "Argent Industrial",
    "BAT": "Brait",
    "BLU": "Blue Label Telecoms",
    "CLH": "City Lodge Hotels",
    "EMH": "eMedia Holdings",
    "EMI": "Emira Property Fund",
    "EQU": "Equites Property Fund",
    "FGL": "Finbond Group",
    "GPL": "Grand Parade Investments",
    "HMN": "Hammerson",
    "HUG": "Huge Group",
    "ISA": "ISA Holdings",
    "LBR": "Libstar",
    "LTE": "Lighthouse Properties",
    "MRF": "Merafe Resources",
    "MST": "Mustek",
    "MTA": "Metair Investments",
    "NPK": "Nampak",
    "NVS": "Niveus Investments",
    "OCT": "Octodec Investments",
    "PAN": "Pan African Resources",
    "PMV": "Primeserv Group",
    "PPE": "Purple Group",
    "RDF": "Redefine Properties",
    "RMH": "RMB Holdings",
    "RNG": "Renergen",
    "SAC": "SA Corporate Real Estate",
    "SEP": "Sephaku Holdings",
    "SNV": "Santova",
    "SSK": "Stefanutti Stocks",
    "TRL": "Trellidor",
    "TSG": "Tsogo Sun",
    "VUN": "Vunani",
    "YRK": "York Timber",
    "ZED": "Zeder Investments",
}


def show_penny_stocks(db: Session):
    """Display the JSE penny stock screener page."""
    st.header("💰 Penny Stocks")
    st.markdown(
        f"Top {TOP_N} JSE shares trading below **R {PENNY_PRICE_LIMIT:.2f}**, ranked by a "
        "heuristic potential score (momentum, trend, RSI, volume). "
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

    # Merge the curated list with active DB stocks so user-tracked small caps
    # are scanned too. Index/forex symbols (containing '.' or '^') and
    # synthetic pseudo-symbols (commodity rows like AV_*/XAU_*, which would
    # resolve to nothing under a ".JO" suffix) are skipped.
    # Symbols held only under non-JSE accounts (e.g. a bare "AAPL" from a USD
    # holding) are not JSE tickers - scanning "<symbol>.JO" wastes a slot or
    # worse, resolves a different instrument that shares the ticker.
    holding_rows = db.query(PortfolioHolding.symbol, PortfolioHolding.account_type).all()
    non_jse_symbols = {
        h.symbol
        for h in holding_rows
        if not is_jse_tracked_account(h.account_type)
    }
    # Symbols actually held (JSE-tracked, real accounts only) get a ★ in the
    # Stock column below.
    held_symbols = {
        h.symbol
        for h in holding_rows
        if is_jse_tracked_account(h.account_type)
        and h.account_type not in EXCLUDED_ACCOUNT_TYPES
        and h.symbol not in EXCLUDED_PORTFOLIO_SYMBOLS
    }
    db_stocks = {
        s.symbol: s.name
        for s in db.query(Stock).filter(Stock.is_active == True).all()
        if s.symbol and "." not in s.symbol and "^" not in s.symbol and "_" not in s.symbol
        and s.symbol not in non_jse_symbols
    }
    extra_symbols = tuple(sorted(s for s in db_stocks if s not in JSE_PENNY_CANDIDATES))

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
        + format_stock_label(r["symbol"], r["name"]),
        "Price": r["price"],
        "Day %": r["day_change"],
        "52w Low": r["low_52w"],
        "52w High": r["high_52w"],
        "Above Low %": r["off_low"],
        "RSI": r["rsi"],
        "3m %": r["mom_3m"],
        "Potential": r["score"],
    } for r in top])

    styled_grid = style_price_columns(
        grid_df.style, grid_df, "Price", price_cols=("Price", "52w Low", "52w High"),
    )
    event = st.dataframe(
        styled_grid,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="penny_stock_grid",
        column_config={
            "Stock": st.column_config.TextColumn(
                "Stock",
                help="Ticker and company name of the scanned share (★ = held in your portfolio)",
            ),
            "Price": st.column_config.NumberColumn(
                "Price",
                help="Latest share price in Rand (last close from Yahoo Finance)",
            ),
            "Day %": st.column_config.NumberColumn(
                "Day %", format="%+.2f%%",
                help="Price change vs the previous trading day's close",
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
            "Potential": st.column_config.ProgressColumn(
                "Potential", min_value=0, max_value=100, format="%d",
                help="Heuristic 0-100 upside score: MACD, RSI, price vs SMA-20/50, 3-month momentum, recent volume",
            ),
        },
    )

    selected_rows = event.selection.rows if event else []
    if selected_rows:
        _render_penny_detail(db, top[selected_rows[0]])
    else:
        st.caption("Click a row to view projections, influences and news.")


@st.cache_data(ttl=SCAN_TTL_SECONDS, show_spinner=False)
def _scan_penny_stocks(extra_symbols: tuple) -> tuple:
    """Batch-download 1y daily history for all candidates and score those below the threshold.

    Returns (results, scanned_at, error) so the page can distinguish "no
    candidates" from "the scan itself failed".
    """
    symbols = sorted(set(JSE_PENNY_CANDIDATES) | set(extra_symbols))
    scanned_at = datetime.now(timezone.utc)

    try:
        data = yf.download(
            [f"{s}.JO" for s in symbols],
            period="1y",
            interval="1d",
            group_by="ticker",
            threads=True,
            progress=False,
            auto_adjust=True,
            # Dividends ride along in the same request - used for the
            # trailing yield (Yahoo's own dividendYield field is unreliable
            # for JSE listings).
            actions=True,
        )
    except Exception as e:
        logger.error(f"Penny stock scan failed: {e}")
        return [], scanned_at, str(e)

    if data is None or data.empty:
        return [], scanned_at, "Yahoo Finance returned no data"

    results = []
    for symbol in symbols:
        stats = _summarise_symbol(data, f"{symbol}.JO", symbol)
        if stats:
            results.append(stats)

    results.sort(key=lambda r: r["score"], reverse=True)
    return results, scanned_at, None


def _summarise_symbol(data: pd.DataFrame, yf_symbol: str, symbol: str) -> Optional[dict]:
    """Compute stats and a potential score for one ticker from the batch download."""
    try:
        df = data[yf_symbol].dropna(subset=["Close"]) if isinstance(data.columns, pd.MultiIndex) else data.dropna(subset=["Close"])
    except (KeyError, TypeError):
        return None

    if df is None:
        return None
    df = _drop_unit_glitches(df)
    if len(df) < MIN_HISTORY_ROWS:
        return None

    # Yahoo reports JSE prices in cents (ZAc); convert to Rand.
    close = df["Close"] / JSE_PRICE_SCALE
    high, low = _sane_high_low(df)
    high, low = high / JSE_PRICE_SCALE, low / JSE_PRICE_SCALE
    volume = df["Volume"].fillna(0)

    current = float(close.iloc[-1])
    if not (0 < current < PENNY_PRICE_LIMIT):
        return None

    prev = float(close.iloc[-2]) if len(close) > 1 else current
    low_52w = float(low.min())
    high_52w = float(high.max())
    close_3m = float(close.iloc[-63]) if len(close) >= 63 else float(close.iloc[0])

    rsi = _last(TechnicalIndicators.calculate_rsi(close))
    macd_data = TechnicalIndicators.calculate_macd(close)
    macd_val = _last(macd_data["macd"])
    macd_sig = _last(macd_data["signal"])
    macd_hist = _last(macd_data["histogram"])
    sma_20 = _last(close.rolling(20).mean())
    sma_50 = _last(close.rolling(50).mean())
    vol_recent = float(volume.tail(10).mean())
    vol_base = float(volume.tail(60).mean())

    score = _potential_score(
        rsi=rsi, macd=macd_val, macd_signal=macd_sig, macd_hist=macd_hist,
        close=current, sma_20=sma_20, sma_50=sma_50,
        mom_3m=((current - close_3m) / close_3m * 100) if close_3m else 0.0,
        vol_recent=vol_recent, vol_base=vol_base,
    )

    return {
        "symbol": symbol,
        "price": current,
        "day_change": ((current - prev) / prev * 100) if prev else 0.0,
        "low_52w": low_52w,
        "high_52w": high_52w,
        "off_low": ((current - low_52w) / low_52w * 100) if low_52w else 0.0,
        "off_high": ((current - high_52w) / high_52w * 100) if high_52w else 0.0,
        "rsi": rsi,
        "macd": macd_val,
        "macd_signal": macd_sig,
        "macd_hist": macd_hist,
        "sma_20": sma_20,
        "sma_50": sma_50,
        "mom_3m": ((current - close_3m) / close_3m * 100) if close_3m else 0.0,
        "vol_recent": vol_recent,
        "vol_base": vol_base,
        "div_yield": _trailing_dividend_yield(df, current),
        "score": score,
        "dates": [d.strftime("%Y-%m-%d") for d in close.index],
        "closes": [float(c) for c in close],
    }


GLITCH_RATIO = 20.0


def _drop_unit_glitches(df: pd.DataFrame) -> pd.DataFrame:
    """Drop bars whose close is >20x off the local median.

    Yahoo sometimes serves thin JSE shares' zero-volume days in Rand instead
    of cents (PMV: 3.10 among ~300 closes) - one such bar wrecks the 52w low,
    RSI and the projection band. The window is centred, so a genuine
    permanent re-rating flips the median within a few bars and survives.
    """
    median = df["Close"].rolling(11, center=True, min_periods=3).median()
    ratio = df["Close"] / median
    keep = ratio.isna() | ((ratio > 1 / GLITCH_RATIO) & (ratio < GLITCH_RATIO))
    return df[keep]


def _sane_high_low(df: pd.DataFrame) -> tuple:
    """Intraday High/Low with impossible values replaced by the bar's close.

    _drop_unit_glitches only checks closes, so a bar with a good close can
    still carry a Rand-vs-cents High/Low that becomes the 52w extreme. A Low
    above the close (or High below it), a missing value, or one >20x off the
    close is not a real trade - fall back to the close for that bar.
    """
    close = df["Close"]
    high = df["High"].where(
        (df["High"] >= close) & (df["High"] < close * GLITCH_RATIO), close
    )
    low = df["Low"].where(
        (df["Low"] <= close) & (df["Low"] > close / GLITCH_RATIO), close
    )
    return high, low


def _trailing_dividend_yield(df: pd.DataFrame, price: float) -> Optional[float]:
    """Dividends paid over the last 365 days as a % of the current price.

    Yahoo reports JSE dividends in the same unit as the close (ZAc), so they
    share JSE_PRICE_SCALE. None = unknown (no Dividends column, or an
    implausible yield that signals a unit mismatch); 0.0 = paid nothing.
    """
    if "Dividends" not in df.columns or not price:
        return None
    cutoff = df.index[-1] - pd.Timedelta(days=365)
    paid = float(df.loc[df.index > cutoff, "Dividends"].fillna(0).sum()) / JSE_PRICE_SCALE
    if paid <= 0:
        return 0.0
    yield_pct = paid / price * 100
    return yield_pct if yield_pct <= DIVIDEND_YIELD_CEILING else None


def _last(series: pd.Series) -> Optional[float]:
    """Last non-NaN value of a series, or None."""
    value = series.iloc[-1]
    return None if pd.isna(value) else float(value)


def _potential_score(rsi, macd, macd_signal, macd_hist, close, sma_20, sma_50, mom_3m, vol_recent, vol_base) -> int:
    """Heuristic 0-100 upside score for a penny stock."""
    score = 0

    if macd_hist is not None and macd_hist > 0:
        score += 20
    if macd is not None and macd_signal is not None and macd > macd_signal:
        score += 15

    if rsi is not None:
        if rsi < 30:
            score += 20  # oversold — bounce potential
        elif rsi <= 45:
            score += 25  # low but not collapsing
        elif rsi <= 60:
            score += 15

    if sma_20 is not None and close > sma_20:
        score += 10
    if sma_50 is not None and close > sma_50:
        score += 10
    if mom_3m > 0:
        score += 10
    if vol_base > 0 and vol_recent > vol_base:
        score += 10

    return min(score, 100)


def _render_penny_detail(db: Session, row: dict):
    """Render detail panel for the selected penny stock."""
    symbol = row["symbol"]
    label = format_stock_label(symbol, row.get("name"))

    st.markdown("---")
    st.subheader(f"🔍 {label}")

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Current", format_price(row["price"]), f"{row['day_change']:+.2f}% today")
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
        _add_to_watchlist(db, symbol, 0.0, 0.0, 0.0, "Added from Penny Stocks screen")
        st.session_state["penny_flash"] = ("success", f"Added {symbol} to watchlist.")
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
        st.plotly_chart(fig, use_container_width=True)

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
