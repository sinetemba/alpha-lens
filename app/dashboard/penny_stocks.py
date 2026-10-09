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
from app.collectors.yfinance import JSE_PRICE_SCALE
from app.dashboard.company import _get_company_news
from app.dashboard.utils import format_stock_label, _to_display_tz
from app.models.news import NewsArticle
from app.models.stock import Stock
from app.models.watchlist import WatchlistItem

PENNY_PRICE_LIMIT = 5.00
TOP_N = 20
SCAN_TTL_SECONDS = 1800
PROJECTION_DAYS = 30
MIN_HISTORY_ROWS = 30

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
    "PEM": "Pembury Lifestyle Group",
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

    # Merge the curated list with active DB stocks so user-tracked small caps
    # are scanned too. Index/forex symbols (containing '.' or '^') are skipped.
    db_stocks = {
        s.symbol: s.name
        for s in db.query(Stock).filter(Stock.is_active == True).all()
        if s.symbol and "." not in s.symbol and "^" not in s.symbol
    }
    extra_symbols = tuple(sorted(s for s in db_stocks if s not in JSE_PENNY_CANDIDATES))

    _, rescan_col = st.columns([3, 1])
    with rescan_col:
        if st.button("🔄 Rescan", key="penny_rescan"):
            _scan_penny_stocks.clear()
            st.rerun()

    with st.spinner("Scanning JSE penny stocks via Yahoo Finance..."):
        results, scanned_at = _scan_penny_stocks(extra_symbols)

    for row in results:
        row["name"] = db_stocks.get(row["symbol"]) or JSE_PENNY_CANDIDATES.get(row["symbol"]) or row["symbol"]

    st.caption(f"Scanned at {_to_display_tz(scanned_at).strftime('%d %b %Y %H:%M')} SAST")

    if not results:
        st.info("No JSE shares currently trading below the threshold were found.")
        return

    top = results[:TOP_N]
    grid_df = pd.DataFrame([{
        "Stock": format_stock_label(r["symbol"], r["name"]),
        "Price": r["price"],
        "Day %": r["day_change"],
        "52w Low": r["low_52w"],
        "52w High": r["high_52w"],
        "Above Low %": r["off_low"],
        "RSI": r["rsi"],
        "3m %": r["mom_3m"],
        "Potential": r["score"],
    } for r in top])

    event = st.dataframe(
        grid_df,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="penny_stock_grid",
        column_config={
            "Price": st.column_config.NumberColumn("Price", format="R %.2f"),
            "Day %": st.column_config.NumberColumn("Day %", format="%+.2f%%"),
            "52w Low": st.column_config.NumberColumn("52w Low", format="R %.2f"),
            "52w High": st.column_config.NumberColumn("52w High", format="R %.2f"),
            "Above Low %": st.column_config.NumberColumn("Above Low", format="%+.1f%%"),
            "RSI": st.column_config.NumberColumn("RSI", format="%.0f"),
            "3m %": st.column_config.NumberColumn("3m %", format="%+.1f%%"),
            "Potential": st.column_config.ProgressColumn(
                "Potential", min_value=0, max_value=100, format="%d"
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
    """Batch-download 1y daily history for all candidates and score those below the threshold."""
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
        )
    except Exception as e:
        logger.error(f"Penny stock scan failed: {e}")
        return [], scanned_at

    if data is None or data.empty:
        return [], scanned_at

    results = []
    for symbol in symbols:
        stats = _summarise_symbol(data, f"{symbol}.JO", symbol)
        if stats:
            results.append(stats)

    results.sort(key=lambda r: r["score"], reverse=True)
    return results, scanned_at


def _summarise_symbol(data: pd.DataFrame, yf_symbol: str, symbol: str) -> Optional[dict]:
    """Compute stats and a potential score for one ticker from the batch download."""
    try:
        df = data[yf_symbol].dropna(subset=["Close"]) if isinstance(data.columns, pd.MultiIndex) else data.dropna(subset=["Close"])
    except (KeyError, TypeError):
        return None

    if df is None or len(df) < MIN_HISTORY_ROWS:
        return None

    # Yahoo reports JSE prices in cents (ZAc); convert to Rand.
    close = df["Close"] / JSE_PRICE_SCALE
    high = df["High"] / JSE_PRICE_SCALE
    low = df["Low"] / JSE_PRICE_SCALE
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
        "score": score,
        "dates": [d.strftime("%Y-%m-%d") for d in close.index],
        "closes": [float(c) for c in close],
    }


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
    col1.metric("Current", f"R {row['price']:.2f}", f"{row['day_change']:+.2f}% today")
    col2.metric("52w Low", f"R {row['low_52w']:.2f}")
    col3.metric("52w High", f"R {row['high_52w']:.2f}")
    col4.metric("3m Momentum", f"{row['mom_3m']:+.1f}%")
    col5.metric("Potential Score", f"{row['score']}/100")

    watched = db.query(WatchlistItem).filter(
        WatchlistItem.symbol == symbol, WatchlistItem.is_active == True
    ).first()
    if watched:
        st.caption("Already on your watchlist.")
    elif st.button("➕ Add to Watchlist", key=f"penny_watch_{symbol}"):
        from app.dashboard.watchlist import _add_to_watchlist
        _add_to_watchlist(db, symbol, 0.0, 0.0, 0.0, "Added from Penny Stocks screen")
        st.success(f"Added {symbol} to watchlist.")
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
        if len(closes) >= MIN_HISTORY_ROWS:
            slope, intercept = np.polyfit(np.arange(len(closes)), closes, 1)
            projected = intercept + slope * (len(closes) - 1 + PROJECTION_DAYS)
            upside = (projected - row["price"]) / row["price"] * 100
            st.metric(
                f"{PROJECTION_DAYS}-day trend projection",
                f"R {projected:.2f}",
                f"{upside:+.1f}% vs current",
            )
            st.caption(f"Linear trend ≈ R {slope:+.3f}/day over the last {len(closes)} trading days.")
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
        influences.append(f"Price above SMA-20 (R {row['sma_20']:.2f}) — short-term uptrend")
    elif row["sma_20"] is not None:
        influences.append(f"Price below SMA-20 (R {row['sma_20']:.2f}) — short-term downtrend")

    if row["sma_50"] is not None and row["price"] > row["sma_50"]:
        influences.append(f"Price above SMA-50 (R {row['sma_50']:.2f}) — medium-term uptrend")

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
        news = _get_company_news(symbol, name)[:3]

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
