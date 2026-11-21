"""JSE penny stock scan - Streamlit-free so the updater can run it too.

One batched Yahoo download (1y daily bars + dividends) for the curated
candidates plus the user's tracked JSE stocks; each symbol under
PENNY_PRICE_LIMIT gets stats and a heuristic potential score. The Penny
Stocks page caches scan_penny_stocks(); the daily historical job calls
run_near_low_alerts() to raise "near 52-week low" notifications.
"""
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf
from loguru import logger
from sqlalchemy.orm import Session

from app.analytics.technical_indicators import TechnicalIndicators
from app.collectors.yfinance import JSE_PRICE_SCALE
from app.config.settings import settings
from app.models.base import utcnow_naive
from app.models.notification import Notification
from app.models.portfolio import (
    EXCLUDED_ACCOUNT_TYPES,
    EXCLUDED_PORTFOLIO_SYMBOLS,
    PortfolioHolding,
    is_jse_tracked_account,
)
from app.models.stock import Stock
from app.notifications.notification_service import NotificationService
from app.utils.formatting import format_price

PENNY_PRICE_LIMIT = 5.00
MIN_HISTORY_ROWS = 30
# Rows whose last trade is this many weekdays behind the market get the ⏸
# marker - their Price is a stale close, not a live quote.
STALE_SESSIONS = 5
# Liquidity = median daily Rand value traded over the last N sessions (a
# no-trade day counts as R0, so a mostly-dormant stock's median is ~0). Mean
# is skewed by one-off block trades (ISA: R38k mean, R26 median).
LIQUIDITY_WINDOW = 20
# (minimum median R/day, score penalty) - checked in order.
LIQUIDITY_PENALTIES = ((10_000, 30), (100_000, 15))
# "Dormant" = no trades on this many of the last LIQUIDITY_WINDOW sessions;
# the page hides these by default.
DORMANT_ZERO_DAYS = 10
# Recent (10-day) vs 60-day average volume: above RISE scores +10 and reads
# "growing interest", below FALL reads "fading interest". One constant for
# both so the score and Key Influences can't disagree.
VOLUME_RISE_RATIO = 1.2
VOLUME_FALL_RATIO = 0.8
# A trailing yield above this is a cents/Rand unit glitch, not a real payout.
DIVIDEND_YIELD_CEILING = 50.0

# Setup tag thresholds - a label naming the trade setup and its natural
# holding horizon, so "buy low / wait / avoid" reads at a glance.
SETUP_RECOVERY_OFF_LOW = 15.0  # % above the 52w low
SETUP_BOUNCE_RSI = 30.0        # deeply oversold
SETUP_EXTENDED_RSI = 70.0      # overbought - too late to chase
SETUP_RECOVERY_RSI = 45.0      # RSI this high near the lows counts as stabilising

# JSE equities that traded below R10 at some point in the year to 2026-11-20
# (R10, not R5, leaves headroom for names drifting down towards the
# threshold). Built by probing ~290 JSE tickers on Yahoo: 202 had data, 82
# met the price test. The live scan still filters by the actual price, so
# a listing above R5 today just doesn't show. Re-probe once a year or so -
# the scan logs symbols that stop returning data.
JSE_PENNY_CANDIDATES = {
    "ACL": "ArcelorMittal South Africa",
    "ACT": "AfroCentric Investment Corporation",
    "ADR": "Adcorp Holdings",
    "AEG": "Aveng",
    "AFH": "Alexander Forbes Group Holdings",
    "AON": "African and Overseas Enterprises",
    "APF": "Accelerate Property Fund",
    "AXX": "Araxi",
    "BAT": "Brait",
    "BIK": "Brikor",
    "BLU": "Blue Label Telecoms",
    "BRN": "Brimstone Investment Corporation",
    "BRT": "Brimstone Investment Corporation (N)",
    "BTN": "Burstone Group",
    "CCD": "Cell C Holdings",
    "CGR": "Calgro M3 Holdings",
    "CHP": "Choppies Enterprises",
    "CKS": "Crookes Brothers",
    "CLH": "City Lodge Hotels",
    "CPR": "Copper 360",
    "CVW": "Castleview Property Fund",
    "DIB": "Dipula Properties",
    "DLT": "Delta Property Fund",
    "DNB": "Deneb Investments",
    "EEL": "Efora Energy",
    "EMH": "eMedia Holdings",
    "EMN": "eMedia Holdings (N)",
    "ENX": "enX Group",
    "EPE": "EPE Capital Partners",
    "EUZ": "Europa Metals",
    "EXP": "Exemplar REITail",
    "FGL": "Finbond Group",
    "FTB": "Fairvest",
    "GML": "Gemfields Group",
    "GPL": "Grand Parade Investments",
    "GTC": "Globe Trade Centre",
    "HET": "Heriot REIT",
    "HLM": "Hulamin",
    "HUG": "Huge Group",
    "ISA": "ISA Holdings",
    "ISB": "Insimbi Industrial Holdings",
    "ITE": "Italtile",
    "KAP": "KAP",
    "LAB": "Labat Africa",
    "LBR": "Libstar",
    "LTE": "Lighthouse Properties",
    "MRF": "Merafe Resources",
    "MTA": "Metair Investments",
    "NVS": "Niveus Investments",
    "OAO": "Oando",
    "PMV": "Primeserv Group",
    "PPC": "PPC",
    "PPE": "Purple Group",
    "PPR": "Putprop",
    "QFH": "Quantum Foods Holdings",
    "RCL": "RCL Foods",
    "RDF": "Redefine Properties",
    "RMH": "RMB Holdings",
    "RNG": "Renergen",
    "RTO": "Rex Trueform Group",
    "SAC": "SA Corporate Real Estate",
    "SAP": "Sappi",
    "SEB": "Sebata Holdings",
    "SEP": "Sephaku Holdings",
    "SHC": "Shaftesbury Capital",
    "SKA": "Shuka Minerals",
    "SNV": "Santova",
    "SOH": "South Ocean Holdings",
    "SSK": "Stefanutti Stocks",
    "SSU": "Southern Sun",
    "TEX": "Texton Property Fund",
    "TLM": "TeleMasters Holdings",
    "TMT": "Trematon Capital Investments",
    "TRL": "Trellidor",
    "TSG": "Tsogo Sun",
    "TTO": "Trustco Group Holdings",
    "UPL": "Universal Partners",
    "VIS": "Visual International Holdings",
    "VUN": "Vunani",
    "WEZ": "Wesizwe Platinum",
    "YRK": "York Timber",
    "ZED": "Zeder Investments",
}


# One alert per stock per episode: a share can sit near its low for weeks,
# and create_notification's own dedup only spans ~20 hours.
NEAR_LOW_COOLDOWN = timedelta(days=30)


def scannable_db_stocks(db: Session) -> dict:
    """Active stored JSE stocks to scan alongside the curated list: symbol -> name.

    Index/forex symbols (containing '.' or '^'), Yahoo futures (NG=F, GC=F)
    and synthetic pseudo-symbols (commodity rows like AV_*/XAU_*) would
    resolve to nothing (a 404) under a ".JO" suffix, so they are skipped. Symbols held only under non-JSE accounts (e.g.
    a bare "AAPL" from a USD holding) are not JSE tickers - scanning
    "<symbol>.JO" wastes a slot or worse, resolves a different instrument
    that shares the ticker.
    """
    non_jse_symbols = {
        h.symbol
        for h in db.query(PortfolioHolding.symbol, PortfolioHolding.account_type).all()
        if not is_jse_tracked_account(h.account_type)
    }
    return {
        s.symbol: s.name
        for s in db.query(Stock).filter(Stock.is_active == True).all()
        if s.symbol and not any(c in s.symbol for c in ".^_=")
        and s.symbol not in non_jse_symbols
    }


def held_jse_symbols(db: Session) -> set:
    """JSE symbols actually held in real accounts - the page's ★, and exempt
    from the dormant filter on both the page and the near-low alerts."""
    return {
        h.symbol
        for h in db.query(PortfolioHolding.symbol, PortfolioHolding.account_type).all()
        if is_jse_tracked_account(h.account_type)
        and h.account_type not in EXCLUDED_ACCOUNT_TYPES
        and h.symbol not in EXCLUDED_PORTFOLIO_SYMBOLS
    }


def extra_scan_symbols(db_stocks: dict) -> tuple:
    """Stored stocks not already in the curated list (sorted, hashable for caching)."""
    return tuple(sorted(s for s in db_stocks if s not in JSE_PENNY_CANDIDATES))


def scan_penny_stocks(extra_symbols: tuple = ()) -> tuple:
    """Batch-download 1y daily history for all candidates and score those below the threshold.

    Returns (results, scanned_at, error, missing): error distinguishes "the
    scan itself failed" from "no candidates"; missing lists the requested
    symbols Yahoo returned no usable history for (absent, all-NaN or under
    MIN_HISTORY_ROWS bars) - yf.download drops those silently, so without
    this a dead ticker just vanishes.
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
        return [], scanned_at, str(e), []

    if data is None or data.empty:
        return [], scanned_at, "Yahoo Finance returned no data", []

    results = []
    missing = []
    for symbol in symbols:
        if not _has_history(data, f"{symbol}.JO"):
            missing.append(symbol)
            continue
        stats = _summarise_symbol(data, f"{symbol}.JO", symbol)
        if stats:
            results.append(stats)

    if missing:
        logger.warning(
            f"Penny scan: no usable Yahoo data for {len(missing)}/{len(symbols)} "
            f"symbols: {', '.join(missing)}"
        )
    _apply_staleness(results)
    results.sort(key=lambda r: r["score"], reverse=True)
    return results, scanned_at, None, missing


def _has_history(data: pd.DataFrame, yf_symbol: str) -> bool:
    """True when the batch download holds MIN_HISTORY_ROWS+ closes for the symbol."""
    try:
        frame = data[yf_symbol] if isinstance(data.columns, pd.MultiIndex) else data
        return int(frame["Close"].notna().sum()) >= MIN_HISTORY_ROWS
    except (KeyError, TypeError):
        return False


def _last_trade_date(volume: pd.Series) -> Optional[str]:
    """Date (YYYY-MM-DD) of the last bar with volume, or None if none traded."""
    traded = volume[volume > 0]
    return traded.index[-1].strftime("%Y-%m-%d") if len(traded) else None


def _apply_staleness(results: list) -> None:
    """Stamp sessions_since_trade and blank Day % for rows that didn't trade.

    The market's latest session is the newest bar across the whole scan, so
    a symbol Yahoo stopped serving bars for is caught too, not just one with
    zero-volume bars. A row that didn't trade on that session has no real
    day change - its 0% (or a move from an older session) would read as a
    live quote, so day_change becomes None. sessions_since_trade counts
    JSE trading days (weekdays minus SA public holidays); None = no trade
    in the year.
    """
    if not results:
        return
    market_day = max(r["dates"][-1] for r in results)
    for r in results:
        last = r.get("last_trade")
        r["sessions_since_trade"] = (
            int(np.busday_count(last, market_day, holidays=_holidays_between(last, market_day)))
            if last else None
        )
        if last != market_day:
            r["day_change"] = None


# Fixed-date South African public holidays (Public Holidays Act 36 of 1994);
# the JSE is closed on all of them. Good Friday and Family Day move with
# Easter. One-off proclaimed days (election days, e.g.) aren't modelled -
# they can only over-count by one session.
_SA_FIXED_HOLIDAYS = (
    (1, 1), (3, 21), (4, 27), (5, 1), (6, 16), (8, 9), (9, 24), (12, 16), (12, 25), (12, 26),
)


def _easter_sunday(year: int) -> date:
    """Gregorian Easter (anonymous Gregorian / Meeus-Jones-Butcher algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


@lru_cache(maxsize=None)
def sa_public_holidays(year: int) -> frozenset:
    """JSE-closed weekdays from South African public holidays in `year`.

    A holiday on a Sunday makes the following Monday a holiday (s2(1) of
    the Act) - unless that Monday already is one (Christmas on a Sunday:
    the 26th is Day of Goodwill either way, no third day).
    """
    days = {date(year, m, d) for m, d in _SA_FIXED_HOLIDAYS}
    easter = _easter_sunday(year)
    days |= {easter - timedelta(days=2), easter + timedelta(days=1)}  # Good Friday, Family Day
    for day in sorted(days):
        if day.weekday() == 6:
            days.add(day + timedelta(days=1))
    return frozenset(d for d in days if d.weekday() < 5)


def _holidays_between(start: str, end: str) -> list:
    """Holiday dates for every year spanned by two YYYY-MM-DD strings."""
    first, last = int(start[:4]), int(end[:4])
    return sorted(d for y in range(first, last + 1) for d in sa_public_holidays(y))


def is_stale(row: dict) -> bool:
    """No trade in the last STALE_SESSIONS weekdays (or none all year)."""
    if "sessions_since_trade" not in row:
        return False  # pre-staleness cached scan result
    sessions = row["sessions_since_trade"]
    return sessions is None or sessions >= STALE_SESSIONS


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
    traded_value = float((volume * close).tail(LIQUIDITY_WINDOW).median())
    zero_volume_days = int((volume.tail(LIQUIDITY_WINDOW) == 0).sum())
    liquidity_penalty = _liquidity_penalty(traded_value)

    score = _potential_score(
        rsi=rsi, macd=macd_val, macd_signal=macd_sig, macd_hist=macd_hist,
        close=current, sma_20=sma_20, sma_50=sma_50,
        mom_3m=((current - close_3m) / close_3m * 100) if close_3m else 0.0,
        vol_recent=vol_recent, vol_base=vol_base,
    )
    # A great chart is worthless if you can't get out of the position.
    score = max(0, score - liquidity_penalty)

    off_low = ((current - low_52w) / low_52w * 100) if low_52w else 0.0
    off_high = ((current - high_52w) / high_52w * 100) if high_52w else 0.0
    mom_3m = ((current - close_3m) / close_3m * 100) if close_3m else 0.0

    return {
        "symbol": symbol,
        "price": current,
        "day_change": ((current - prev) / prev * 100) if prev else 0.0,
        "low_52w": low_52w,
        "high_52w": high_52w,
        "off_low": off_low,
        "off_high": off_high,
        "rsi": rsi,
        "macd": macd_val,
        "macd_signal": macd_sig,
        "macd_hist": macd_hist,
        "sma_20": sma_20,
        "sma_50": sma_50,
        "mom_3m": mom_3m,
        "setup": _setup_tag(rsi, macd_hist, off_low, current, sma_20, sma_50, mom_3m),
        "vol_recent": vol_recent,
        "vol_base": vol_base,
        # Yahoo forward-fills no-trade days as zero-volume bars, so the last
        # bar's date isn't the last trade - the last bar WITH volume is.
        "last_trade": _last_trade_date(volume),
        "traded_value": traded_value,
        "zero_volume_days": zero_volume_days,
        "liquidity_penalty": liquidity_penalty,
        "div_yield": _trailing_dividend_yield(df, current),
        "score": score,
        "dates": [d.strftime("%Y-%m-%d") for d in close.index],
        "closes": [float(c) for c in close],
    }


GLITCH_RATIO = 20.0


def is_dormant(row: dict) -> bool:
    """No trades on DORMANT_ZERO_DAYS+ of the last LIQUIDITY_WINDOW sessions."""
    return row.get("zero_volume_days", 0) >= DORMANT_ZERO_DAYS


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

    Yahoo reports .JO OHLC in cents (ZAc) but dividends in cents for some
    tickers and Rand for others, so a blanket conversion is wrong - each
    payment is unit-checked against its own day's close, the same rule as
    DataService._dividend_amount_rand: a dividend larger than half the share
    price was served in cents and is rescaled. None = unknown (no Dividends
    column, or an implausible yield that still signals a unit mismatch);
    0.0 = paid nothing.
    """
    if "Dividends" not in df.columns or not price or df.empty:
        return None
    cutoff = df.index[-1] - pd.Timedelta(days=365)
    window = df.loc[df.index > cutoff, ["Close", "Dividends"]].dropna(subset=["Close"])
    close_rand = window["Close"] / JSE_PRICE_SCALE
    divs = window["Dividends"].fillna(0)
    paid = float(divs.where(divs <= close_rand * 0.5, divs / JSE_PRICE_SCALE).sum())
    if paid <= 0:
        return 0.0
    yield_pct = paid / price * 100
    return yield_pct if yield_pct <= DIVIDEND_YIELD_CEILING else None


def _last(series: pd.Series) -> Optional[float]:
    """Last non-NaN value of a series, or None."""
    value = series.iloc[-1]
    return None if pd.isna(value) else float(value)


def _liquidity_penalty(traded_value: float) -> int:
    """Score points removed for thin trading (median Rand value per day)."""
    for minimum, penalty in LIQUIDITY_PENALTIES:
        if traded_value < minimum:
            return penalty
    return 0


def _setup_tag(rsi, macd_hist, off_low, close, sma_20, sma_50, mom_3m) -> str:
    """Classify the trade setup; the suffix is the natural holding horizon.

    First match wins: Extended (overbought - wait for a pullback) >
    Recovery (near the 52w low AND momentum stabilising - the long-term
    "buy low" play) > Bounce (deeply oversold snap-back, short-term) >
    Trend (established uptrend, mid-term) > Weak (still falling - no
    entry) > Watch (no setup). Recovery needs a turn signal, not just a low
    price - a stock at its low with momentum still falling is a knife, not
    a bargain.
    """
    if rsi is not None and rsi >= SETUP_EXTENDED_RSI:
        return "Extended"
    stabilising = (macd_hist is not None and macd_hist > 0) or (
        rsi is not None and rsi >= SETUP_RECOVERY_RSI
    )
    if off_low is not None and off_low <= SETUP_RECOVERY_OFF_LOW and stabilising:
        return "Recovery · long"
    if rsi is not None and rsi < SETUP_BOUNCE_RSI:
        return "Bounce · short"
    if (
        sma_20 is not None and sma_50 is not None
        and close > sma_20 and close > sma_50 and mom_3m > 0
    ):
        return "Trend · mid"
    if sma_20 is not None and close < sma_20 and mom_3m < 0:
        return "Weak"
    return "Watch"


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
    if vol_base > 0 and vol_recent > vol_base * VOLUME_RISE_RATIO:
        score += 10

    return min(score, 100)


def near_low_title(symbol: str) -> str:
    return f"Near 52-week low: {symbol}"


def near_low_candidates(results: list, threshold_pct: float, held: frozenset = frozenset()) -> list:
    """Scanned rows closing within threshold_pct of their 52-week low.

    Stale rows are skipped - an old close sitting near the low says nothing
    about today's price. Dormant rows are skipped too (same rule as the
    page's default filter: a cheap share you can't trade isn't an
    opportunity), except for symbols in `held` - you'd want to know about
    a position you already own.
    """
    return [
        r for r in results
        if not is_stale(r) and r.get("low_52w") and r.get("off_low") is not None
        and r["off_low"] <= threshold_pct
        and (r["symbol"] in held or not is_dormant(r))
    ]


def _near_low_message(row: dict, name: str) -> str:
    off_low = row["off_low"]
    where = (
        "at its 52-week low" if off_low < 0.5
        else f"{off_low:.1f}% above its 52-week low"
    )
    when = f" on {row['last_trade']}" if row.get("last_trade") else ""
    liquidity = ""
    if row.get("traded_value") is not None:
        liquidity = f" Typical day: R {row['traded_value']:,.0f} traded"
        liquidity += " - thinly traded, may be hard to exit." if row.get("liquidity_penalty") else "."
    return (
        f"{name} ({row['symbol']}) closed at {format_price(row['price'])}{when}, {where} "
        f"of {format_price(row['low_52w'])} (52-week high {format_price(row['high_52w'])})."
        f"{liquidity} See the Penny Stocks page for the full picture."
    )


def check_near_low_alerts(
    db: Session, results: list, threshold_pct: float,
    names: Optional[dict] = None, held: frozenset = frozenset(),
) -> int:
    """Create type="price" notifications for near-low rows; returns how many.

    Skips a symbol that already got this alert within NEAR_LOW_COOLDOWN.
    External delivery is left to send_pending_notifications (gated on
    notification_enabled, like every other alert).
    """
    names = names or {}
    notifier = NotificationService(db)
    cutoff = utcnow_naive() - NEAR_LOW_COOLDOWN
    candidates = near_low_candidates(results, threshold_pct, held)
    if not candidates:
        return 0
    # One grouped query for every candidate's cooldown check, not one each.
    alerted_titles = {
        title for (title,) in db.query(Notification.title).filter(
            Notification.type == "price",
            Notification.stock_symbol.in_([r["symbol"] for r in candidates]),
            Notification.title.like("Near 52-week low:%"),
            Notification.created_at >= cutoff,
        ).all()
    }
    created = 0
    for row in candidates:
        symbol = row["symbol"]
        title = near_low_title(symbol)
        if title in alerted_titles:
            continue
        notifier.create_notification(
            notification_type="price",
            title=title,
            message=_near_low_message(row, names.get(symbol) or symbol),
            stock_symbol=symbol,
            metadata={
                "alert": "near_52w_low",
                "price": row["price"],
                "low_52w": row["low_52w"],
                "off_low_pct": round(row["off_low"], 2),
                "traded_value": row.get("traded_value"),
            },
        )
        created += 1
    if created:
        notifier.send_pending_notifications()
    return created


def run_near_low_alerts(db: Session) -> int:
    """Scan and alert - called by the updater's daily historical job.

    Raises when the scan itself fails so the caller can log it; returns 0
    when disabled (PENNY_LOW_ALERT_ENABLED / YFINANCE_ENABLED).
    """
    if not settings.penny_low_alert_enabled or not settings.yfinance_enabled:
        return 0
    db_stocks = scannable_db_stocks(db)
    results, _, error, _missing = scan_penny_stocks(extra_scan_symbols(db_stocks))
    if error:
        raise RuntimeError(f"penny scan failed: {error}")
    names = {**JSE_PENNY_CANDIDATES, **db_stocks}
    created = check_near_low_alerts(
        db, results, settings.penny_low_alert_pct, names, frozenset(held_jse_symbols(db)),
    )
    logger.info(
        f"Penny near-52w-low check: {len(results)} scanned, {created} new alert(s) "
        f"(threshold {settings.penny_low_alert_pct:g}%)"
    )
    return created
