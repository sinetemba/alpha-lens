from datetime import datetime, timedelta, timezone
import json
import re
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo
import pandas as pd
import streamlit as st
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.config.settings import settings
from app.models.news import NewsArticle
from app.models.stock import Stock, StockPrice
from app.collectors.yfinance import YFinanceCollector
from app.collectors.twelve_data import DEFAULT_REQUESTS_PER_DAY
from app.models.updater_run import UpdaterRun
from app.services.data_service import COMPANY_NAME_OVERRIDES, DataService
# Re-exported from the Streamlit-free module so non-UI consumers (the headless
# updater's NotificationService) share one implementation.
from app.services.price_queries import PriceSnapshot, day_of  # noqa: F401


def format_stock_label(symbol: str, name: Optional[str] = None) -> str:
    """Format a stock symbol with its company name, e.g. 'SOL - Sasol Limited'."""
    if not name or name == symbol:
        # Some feeds (e.g. Moneyweb movers) only return the share code for
        # certain symbols; use known overrides so they still render with a name.
        name = COMPANY_NAME_OVERRIDES.get(symbol)
    if name and name != symbol:
        return f"{symbol} - {name}"
    return symbol


def holding_totals(pairs: Iterable[tuple[Optional[float], Optional[float]]]) -> dict:
    """Totals over (cost, current_value) pairs; either may be None if unknown.

    Gain/loss covers only holdings with BOTH a cost and a live value - summing
    all values minus all costs counted an un-costed holding's whole value as
    gain and an unpriced holding's whole cost as loss.
    """
    total_cost = total_value = gain_cost = gain_value = 0.0
    has_cost = has_value = has_gain = False
    for cost, value in pairs:
        if cost:
            total_cost += cost
            has_cost = True
        if value is not None:
            total_value += value
            has_value = True
        if cost and value is not None:
            gain_cost += cost
            gain_value += value
            has_gain = True
    gain = gain_value - gain_cost if has_gain else None
    return {
        "total_cost": total_cost if has_cost else None,
        "total_value": total_value if has_value else None,
        "gain": gain,
        "gain_pct": gain / gain_cost * 100 if has_gain else None,
    }


def render_holding_totals(totals: dict) -> None:
    """Total cost / current value / gain-loss metric row for `holding_totals`."""
    c1, c2, c3 = st.columns(3)
    with c1:
        cost = totals["total_cost"]
        st.metric("Total cost", f"R {cost:,.2f}" if cost is not None else "N/A")
    with c2:
        value = totals["total_value"]
        st.metric("Current value", f"R {value:,.2f}" if value is not None else "N/A")
    with c3:
        if totals["gain"] is not None:
            st.metric(
                "Gain/Loss", f"R {totals['gain']:+,.2f}", delta=f"{totals['gain_pct']:+.1f}%"
            )
        else:
            st.metric("Gain/Loss", "N/A")


# Shape of a typed JSE ticker (stored without the ".JO" suffix). JSE codes
# are 3 characters, ETFs up to ~6 (e.g. STXNDQ).
_TICKER_RE = re.compile(r"^[A-Z0-9]{2,6}$")


@st.cache_data(ttl=3600, show_spinner=False)
def _search_jse(query: str) -> Optional[list[dict]]:
    """Cached Yahoo JSE search - None when the search itself failed."""
    return YFinanceCollector().search_jse(query)


def match_symbols(db: Session, query: str, remote: Optional[list[dict]]) -> dict[str, str]:
    """Ordered {symbol: name} candidates for a symbol-or-name query.

    An exact ticker comes first (typing "NPN" keeps working as before), then
    stored stocks matching the symbol or name, then Yahoo's JSE results.
    """
    q = query.strip()
    if not q:
        return {}
    candidates: dict[str, str] = {}
    exact = db.query(Stock).filter(Stock.symbol == q.upper()).first()
    if exact:
        candidates[exact.symbol] = exact.name
    for stock in db.query(Stock).filter(
        (Stock.symbol.ilike(f"{q}%")) | (Stock.name.ilike(f"%{q}%"))
    ).order_by(Stock.symbol).limit(8):
        candidates.setdefault(stock.symbol, stock.name)
    for row in remote or []:
        candidates.setdefault(row["symbol"], row["name"])
    if _TICKER_RE.match(q.upper()) and q.upper() not in candidates:
        # Unknown ticker typed verbatim - still allow it (validated on add),
        # but after real matches: "sasol" is a name search, not ticker SASOL.
        candidates[q.upper()] = "use as typed"
    return candidates


def render_symbol_search(db: Session, key: str) -> str:
    """Symbol-or-company-name input with a matches picker; returns the chosen ticker."""
    query = st.text_input(
        "Symbol or company name", placeholder="e.g. NPN or Naspers", key=f"{key}_query"
    )
    q = query.strip()
    if not q:
        return ""
    # Every keystroke reruns this function, and each new query is a fresh
    # cache key = a live Yahoo call. Below 3 chars the local prefix/name
    # matches are enough; JSE tickers are 3+ chars anyway.
    remote = _search_jse(q) if len(q) >= 3 else None
    candidates = match_symbols(db, q, remote)
    if len(q) >= 3 and remote is None:
        st.caption("Company search is unavailable right now - enter the exact ticker.")
    if not candidates:
        st.caption(f"No JSE listings match '{query.strip()}'.")
        return ""
    return st.selectbox(
        "Matches",
        list(candidates),
        format_func=lambda s: f"{s} - {candidates[s]}" if candidates[s] else s,
        key=f"{key}_match",
    )


def render_csv_download(table: pd.DataFrame, name: str, key: str) -> None:
    """'Download CSV' button for a table; `_`-prefixed helper columns are dropped.

    The file name carries today's date (display timezone) so repeated exports
    don't overwrite each other.
    """
    export = table[[c for c in table.columns if not str(c).startswith("_")]]
    stamp = _to_display_tz(datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    st.download_button(
        "⬇️ Download CSV",
        data=export.to_csv(index=False).encode("utf-8-sig"),  # BOM: Excel reads UTF-8
        file_name=f"alphalens_{name}_{stamp}.csv",
        mime="text/csv",
        key=key,
    )


def style_gain_loss_row(row: pd.Series, gain_loss=None) -> list[str]:
    """Colour a table row green/red/yellow based on its gain/loss value.

    Reads the "Gain/Loss" column by default; callers whose table lets the
    user hide that column pass the value from the full frame explicitly.
    Shared styling used by both the Portfolio and Watchlist pages so
    gain/loss rows are colour-coded consistently across the app.
    """
    if gain_loss is None:
        gain_loss = row.get("Gain/Loss")
    if gain_loss is None:
        color = "#fef3c7"
    elif gain_loss > 0:
        color = "#dcfce7"
    elif gain_loss < 0:
        color = "#fee2e2"
    else:
        color = "#fef3c7"
    # Explicit dark text: the dark theme's default white text is unreadable
    # on these pastel backgrounds.
    return [f"background-color: {color}; color: #1f2937"] * len(row)


# Defined in the Streamlit-free app.utils.formatting so alert messages built
# by the headless updater use the same rules; re-exported for dashboard code.
from app.utils.formatting import SUB_RAND, format_price  # noqa: E402,F401


def format_price_cell(
    value: Optional[float], reference: Optional[float] = None, signed: bool = False,
) -> str:
    """Per-row Rand price text for Styler display (numbers stay sortable).

    2 decimals, unless the row's reference price (its current price; the
    value itself when there is none) is under R1 - then up to 4 decimals
    with trailing zeros trimmed (R 0.0085, R 0.62, not R 0.6200).
    """
    if value is None or pd.isna(value):
        return ""
    ref = value if reference is None or pd.isna(reference) else reference
    sign = "+" if signed else ""
    if 0 < abs(ref) < SUB_RAND:
        whole, frac = f"{value:{sign},.4f}".split(".")
        return f"R {whole}.{frac.rstrip('0').ljust(2, '0')}"
    return f"R {value:{sign},.2f}"


def style_price_columns(
    styler,
    df: pd.DataFrame,
    reference=None,
    price_cols: Iterable[str] = (),
    signed_cols: Iterable[str] = (),
):
    """Format price columns row-by-row on a pandas Styler.

    Unlike `price_column_format` (one printf format per column), one penny
    stock in the table doesn't put every row on 4 decimals. `reference` is
    the per-row price that decides the decimals - a column name in `df`, a
    Series aligned to `df`'s index (e.g. from a fuller table when the user
    has hidden the column), or None to judge each value by itself. Leave
    `format` off these columns' NumberColumn config so the Styler text is
    shown; the underlying values stay numeric, so sorting still works.
    """
    if isinstance(reference, str):
        refs = df[reference] if reference in df.columns else None
    else:
        refs = reference
    if refs is not None:
        refs = pd.to_numeric(refs.reindex(df.index), errors="coerce")
        penny = (refs.abs() > 0) & (refs.abs() < SUB_RAND)
        groups = (
            (df.index[penny], SUB_RAND / 2),     # under R1 -> up to 4 decimals
            (df.index[refs.notna() & ~penny], SUB_RAND * 2),  # R1+ -> 2 decimals
            (df.index[refs.isna()], None),       # no reference -> by own value
        )
    else:
        groups = ((df.index, None),)
    for cols, signed in ((price_cols, False), (signed_cols, True)):
        for col in cols:
            if col not in df.columns:
                continue
            for rows, ref in groups:
                if len(rows):
                    styler = styler.format(
                        lambda v, ref=ref, signed=signed: format_price_cell(v, ref, signed),
                        subset=pd.IndexSlice[rows, [col]],
                    )
    return styler


def price_column_format(*columns: Iterable[Optional[float]], signed: bool = False) -> str:
    """NumberColumn printf format for per-share price columns.

    Column formats apply to every row, so one sub-Rand price anywhere in the
    given columns switches the whole set to 4 decimals.
    """
    sub_rand = any(
        v is not None and not pd.isna(v) and 0 < abs(v) < SUB_RAND
        for column in columns for v in column
    )
    sign = "+" if signed else ""
    return f"R %{sign}.4f" if sub_rand else f"R %{sign}.2f"


def get_latest_market_data_timestamp(
    db: Session, symbols: Optional[Iterable[str]] = None
) -> Optional[datetime]:
    # fetched_at tracks when data was last pulled from a provider; fall back to
    # the bar timestamp for rows predating the column. Two indexed MAX()s beat
    # ORDER BY coalesce(...) which cannot use either index.
    query = db.query(
        func.max(StockPrice.fetched_at),
        func.max(StockPrice.timestamp),
    )
    if symbols is not None:
        symbols = list(symbols)
        if not symbols:
            return None
        query = query.filter(StockPrice.symbol.in_(symbols))
    latest_fetched, latest_bar = query.first()
    if latest_bar is not None:
        # Unlike fetched_at (a UTC instant), `timestamp` is naive exchange-local
        # midnight. Comparing it raw against fetched_at can make it look up to
        # a UTC-offset's worth "newer" than it really is, so convert to a UTC
        # instant first - then strip tzinfo back to naive-UTC to match
        # fetched_at's convention (callers treat naive timestamps as UTC).
        latest_bar = (
            latest_bar.replace(tzinfo=ZoneInfo(settings.scheduler_timezone))
            .astimezone(timezone.utc)
            .replace(tzinfo=None)
        )
    candidates = [t for t in (latest_fetched, latest_bar) if t is not None]
    return max(candidates) if candidates else None


def _ensure_aware(timestamp: datetime) -> datetime:
    """Existing DB rows may store naive (tz-unaware) timestamps."""
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp


def _to_display_tz(timestamp: datetime) -> datetime:
    """Convert an aware UTC timestamp to the configured display timezone."""
    timestamp = _ensure_aware(timestamp)
    try:
        return timestamp.astimezone(ZoneInfo(settings.scheduler_timezone))
    except Exception:
        return timestamp


def is_market_data_stale(timestamp: Optional[datetime]) -> bool:
    if timestamp is None:
        return True
    age = datetime.now(timezone.utc) - _ensure_aware(timestamp)
    return age > timedelta(seconds=settings.cache_ttl_seconds)


def format_market_data_age(timestamp: Optional[datetime]) -> str:
    if timestamp is None:
        return "No market data has been synced yet."

    timestamp = _ensure_aware(timestamp)
    display_timestamp = _to_display_tz(timestamp)
    age = max(datetime.now(timezone.utc) - timestamp, timedelta(0))
    if age.days:
        age_label = f"{age.days} day{'s' if age.days != 1 else ''} ago"
    elif age.seconds >= 3600:
        hours = age.seconds // 3600
        age_label = f"{hours} hour{'s' if hours != 1 else ''} ago"
    else:
        minutes = age.seconds // 60
        age_label = f"{minutes} minute{'s' if minutes != 1 else ''} ago"

    return f"Last market data: {display_timestamp.strftime('%d %b %Y %H:%M')} ({age_label})"


def get_latest_news_timestamp(db: Session) -> Optional[datetime]:
    """Return the most recent news fetch time (from created_at)."""
    latest = (
        db.query(NewsArticle.created_at)
        .order_by(NewsArticle.created_at.desc())
        .first()
    )
    return latest[0] if latest else None


def format_news_age(timestamp: Optional[datetime]) -> str:
    """Format the age of the latest news fetch for display."""
    if timestamp is None:
        return "No news has been fetched yet."

    timestamp = _ensure_aware(timestamp)
    display_timestamp = _to_display_tz(timestamp)
    age = max(datetime.now(timezone.utc) - timestamp, timedelta(0))
    if age.days:
        age_label = f"{age.days} day{'s' if age.days != 1 else ''} ago"
    elif age.seconds >= 3600:
        hours = age.seconds // 3600
        age_label = f"{hours} hour{'s' if hours != 1 else ''} ago"
    else:
        minutes = age.seconds // 60
        age_label = f"{minutes} minute{'s' if minutes != 1 else ''} ago"

    return f"Last news fetch: {display_timestamp.strftime('%d %b %Y %H:%M')} ({age_label})"


# The price+news job runs 3x/day; the longest normal gap is ~16h (17:00 ->
# 09:00 next morning). Beyond this the data is almost certainly stale.
UPDATER_STALE_AGE = timedelta(hours=20)


# Same on-disk counter the TwelveDataCollector shares across processes -
# read it directly rather than instantiating a collector just to display it.
_TWELVE_DATA_USAGE_FILE = Path(__file__).resolve().parents[2] / "data" / ".twelve_data_usage"


def format_twelve_data_quota() -> Optional[str]:
    """'Twelve Data today: 123/800' from the shared counter, or None if unused."""
    if not settings.twelve_data_enabled:
        return None
    try:
        data = json.loads(_TWELVE_DATA_USAGE_FILE.read_text(encoding="utf-8"))
        if data.get("date") != str(datetime.now(timezone.utc).date()):
            return None
        return f"Twelve Data today: {int(data.get('count', 0))}/{DEFAULT_REQUESTS_PER_DAY}"
    except (OSError, ValueError, TypeError):
        return None


def render_updater_status_banner(db: Session) -> None:
    """Warn when the headless updater's heartbeat says prices aren't refreshing.

    Reads the updater_runs table written by scripts/run_updater.py. When the
    table is empty the updater has never run (or the dashboard points at a
    different DB): a once-per-session info note is shown so a fresh install
    knows where prices come from instead of silently showing stale data.
    """
    latest = (
        db.query(UpdaterRun)
        .filter(UpdaterRun.job_name == "price_news_update")
        .order_by(UpdaterRun.finished_at.desc())
        .first()
    )
    if latest is None:
        if not st.session_state.get("_updater_never_ran_noted"):
            st.session_state["_updater_never_ran_noted"] = True
            st.info(
                "No automatic price updates have run yet - start the "
                "AlphaLensUpdater task (scripts\\install_updater_task.ps1) or "
                "use Refresh Prices below."
            )
        return

    # A failed backup used to be invisible outside the Settings page -
    # surface it here too since holdings history is hand-entered data.
    latest_backup = (
        db.query(UpdaterRun)
        .filter(UpdaterRun.job_name == "database_backup")
        .order_by(UpdaterRun.finished_at.desc())
        .first()
    )
    if latest_backup is not None and not latest_backup.success:
        backup_display = _to_display_tz(
            _ensure_aware(latest_backup.finished_at)
        ).strftime("%d %b %H:%M")
        st.warning(
            f"⚠️ The last database backup ({backup_display} SAST) failed"
            f"{': ' + latest_backup.detail if latest_backup.detail else ''} - "
            "check data/logs/updater.log."
        )

    finished = _ensure_aware(latest.finished_at)
    display = _to_display_tz(finished).strftime("%d %b %H:%M")
    if not latest.success:
        st.warning(
            f"⚠️ The last price update ({display} SAST) failed"
            f"{': ' + latest.detail if latest.detail else ''} - "
            "data may be stale. Check data/logs/updater.log."
        )
        return

    age = datetime.now(timezone.utc) - finished
    if age > UPDATER_STALE_AGE:
        hours = age.days * 24 + age.seconds // 3600
        st.warning(
            f"⚠️ Market data may be stale - the price updater last ran {hours}h ago "
            f"({display} SAST; it normally runs 3x/day). "
            "Check that the AlphaLensUpdater task/service is running."
        )


def render_last_fetch_caption(db: Session, symbols: Optional[Iterable[str]] = None) -> None:
    """Render a caption showing the last market data fetch for the given symbols."""
    render_updater_status_banner(db)
    timestamp = get_latest_market_data_timestamp(db, symbols)
    st.caption(format_market_data_age(timestamp))


def render_market_data_refresh_control(
    db: Session,
    key: str,
    symbols: Optional[Iterable[str]] = None,
    include_news: bool = False,
    auto_refresh: bool = False,
) -> None:
    """Render the manual "Refresh Prices" control.

    If `auto_refresh` is set, market data older than `settings.cache_ttl_seconds`
    is refreshed automatically (no button click needed) before the rest of the
    page renders, so callers reading prices afterwards see current data.
    """
    symbol_list = list(symbols) if symbols is not None else None
    latest_timestamp = get_latest_market_data_timestamp(db, symbol_list)
    # Auto-refresh at most once per session per key - re-running the fetch on
    # every page load/navigation makes the dashboard feel broken when the
    # provider is slow or unreachable.
    auto_key = f"{key}_auto_refreshed"
    needs_auto_refresh = (
        auto_refresh
        and is_market_data_stale(latest_timestamp)
        and not st.session_state.get(auto_key)
    )

    _, refresh_column = st.columns([3, 1])
    with refresh_column:
        # st.rerun() discards messages rendered in the same run - the manual
        # refresh result is carried across the rerun as a flash.
        flash = st.session_state.pop(f"{key}_flash", None)
        if flash:
            getattr(st, flash[0])(flash[1])
        manual_refresh = st.button("🔄 Refresh Prices", key=key)
        if manual_refresh or needs_auto_refresh:
            if needs_auto_refresh:
                st.session_state[auto_key] = True
            # Share the updater's cross-process lock: if the background
            # updater is mid-fetch, don't double-spend quota racing it.
            from app.services import process_lock
            if not process_lock.JOB_LOCK.acquire(timeout=5):
                st.info(
                    "The background updater is fetching market data right now - "
                    "prices will update when it finishes."
                )
            else:
                try:
                    spinner_text = (
                        "Fetching latest market data..." if manual_refresh
                        else "Auto-refreshing market data..."
                    )
                    with st.spinner(spinner_text):
                        data_service = DataService(db)
                        updated_prices = data_service.update_stock_prices(symbol_list or [])
                        new_articles = data_service.collect_news() if include_news else 0
                finally:
                    process_lock.JOB_LOCK.release()
                if data_service.quota_message:
                    st.warning(
                        f"{data_service.quota_message} "
                        f"{format_market_data_age(latest_timestamp)}"
                    )
                if manual_refresh:
                    status = f"Updated prices for {updated_prices} symbol{'s' if updated_prices != 1 else ''}."
                    if include_news:
                        status += f" Collected {new_articles} new article{'s' if new_articles != 1 else ''}."
                    st.session_state[f"{key}_flash"] = ("success", status)
                    st.rerun()
                latest_timestamp = get_latest_market_data_timestamp(db, symbol_list)
        quota = format_twelve_data_quota()
        age_caption = format_market_data_age(latest_timestamp)
        st.caption(f"{age_caption} | {quota}" if quota else age_caption)
