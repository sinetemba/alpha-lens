"""Energy / agriculture / livestock prices from Yahoo Finance futures.

Free, keyless, and one batched request for every contract plus ZAR=X, so
the Commodities page and the updater's daily historical job can both call
refresh_futures() without a quota. Streamlit-free - the updater imports it.

Stored closes are ZAR per the contract's quote unit (R/bbl, R/bushel ...),
converted with that same day's USD/ZAR close. Prices are front-month
continuous contracts, so a series steps slightly at each contract roll.
"""
from datetime import timedelta

import pandas as pd
from loguru import logger
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.collectors.yfinance import YFinanceCollector
from app.models.base import utcnow_naive
from app.models.stock import Stock, StockPrice
from app.services.source_health import record_source

_USD = 1.0
_US_CENTS = 0.01  # Yahoo currency "USX": grains, softs and livestock

# key -> (name, stored pseudo-symbol, Yahoo ticker, category, unit, USD scale)
FUTURES = {
    "wti": ("WTI Crude Oil", "YF_WTI", "CL=F", "Energy", "bbl", _USD),
    "brent": ("Brent Crude Oil", "YF_BRENT", "BZ=F", "Energy", "bbl", _USD),
    "natgas": ("Natural Gas", "YF_NATGAS", "NG=F", "Energy", "MMBtu", _USD),
    "gasoline": ("RBOB Gasoline", "YF_RBOB", "RB=F", "Energy", "gal", _USD),
    "heating_oil": ("Heating Oil / Diesel", "YF_HEATOIL", "HO=F", "Energy", "gal", _USD),
    "wheat": ("Wheat (CBOT)", "YF_WHEAT", "ZW=F", "Agriculture", "bushel", _US_CENTS),
    "corn": ("Corn (CBOT)", "YF_CORN", "ZC=F", "Agriculture", "bushel", _US_CENTS),
    "soybeans": ("Soybeans", "YF_SOYBEAN", "ZS=F", "Agriculture", "bushel", _US_CENTS),
    "coffee": ("Coffee", "YF_COFFEE", "KC=F", "Agriculture", "lb", _US_CENTS),
    "sugar": ("Sugar", "YF_SUGAR", "SB=F", "Agriculture", "lb", _US_CENTS),
    "cocoa": ("Cocoa", "YF_COCOA", "CC=F", "Agriculture", "t", _USD),
    "cotton": ("Cotton", "YF_COTTON", "CT=F", "Agriculture", "lb", _US_CENTS),
    "live_cattle": ("Live Cattle", "YF_CATTLE", "LE=F", "Livestock", "lb", _US_CENTS),
    "lean_hogs": ("Lean Hogs", "YF_HOGS", "HE=F", "Livestock", "lb", _US_CENTS),
}
FUTURES_DB_SYMBOLS = [meta[1] for meta in FUTURES.values()]
# Re-fetch window once a symbol has history: covers missed days + revisions.
_INCREMENTAL_PERIOD = "1mo"
_BACKFILL_PERIOD = "1y"


def _needs_backfill(db: Session) -> bool:
    """True when any futures symbol has no bar from the last month."""
    cutoff = utcnow_naive() - timedelta(days=30)
    with_recent = {
        s for (s,) in db.query(StockPrice.symbol).filter(
            StockPrice.symbol.in_(FUTURES_DB_SYMBOLS),
            StockPrice.timestamp >= cutoff,
        ).distinct().all()
    }
    return with_recent != set(FUTURES_DB_SYMBOLS)


def zar_closes(closes: pd.DataFrame) -> dict[str, dict]:
    """{db_symbol: {naive day-midnight: ZAR close}} from a Yahoo close frame.

    Each day's close is converted at that day's USD/ZAR (forward-filled over
    FX gaps - US and FX holidays differ); days with no close are skipped.
    """
    # bfill too: on a backfill the FX series can start later than the
    # commodity's, and a pure ffill silently drops those early bars.
    zar = closes["ZAR=X"].ffill().bfill()
    result: dict[str, dict] = {}
    for _key, (_name, db_symbol, ticker, _cat, _unit, scale) in FUTURES.items():
        if ticker not in closes.columns:
            continue
        bars = {}
        for ts, close in closes[ticker].items():
            rate = zar.get(ts)
            if pd.isna(close) or close <= 0 or rate is None or pd.isna(rate) or rate <= 0:
                continue
            day = pd.Timestamp(ts).tz_localize(None).normalize().to_pydatetime()
            bars[day] = float(close) * scale * float(rate)
        if bars:
            result[db_symbol] = bars
    return result


def save_zar_closes(db: Session, series: dict[str, dict]) -> int:
    """Upsert day-midnight bars (overwrite in place - Yahoo revises the
    latest settlement). Returns the number of bars written."""
    if not series:
        return 0
    now = utcnow_naive()
    names = {meta[1]: meta[0] for meta in FUTURES.values()}
    stocks = {
        s.symbol: s for s in db.query(Stock).filter(Stock.symbol.in_(list(series))).all()
    }
    written = 0
    for db_symbol, bars in series.items():
        stock = stocks.get(db_symbol)
        if stock is None:
            stock = Stock(symbol=db_symbol, name=names.get(db_symbol, db_symbol))
            db.add(stock)
            db.flush()
        existing = {
            p.timestamp: p for p in db.query(StockPrice).filter(
                StockPrice.symbol == db_symbol,
                StockPrice.timestamp.in_(list(bars)),
            ).all()
        }
        for day, close in bars.items():
            row = existing.get(day)
            if row is not None:
                row.close_price = close
                row.adjusted_close = close
                row.fetched_at = now
            else:
                db.add(StockPrice(
                    stock_id=stock.id, symbol=db_symbol, timestamp=day,
                    close_price=close, adjusted_close=close, fetched_at=now,
                ))
            written += 1
    try:
        db.commit()
    except IntegrityError:
        # A concurrent refresh wrote the same bars first - keep theirs.
        db.rollback()
        return 0
    return written


def refresh_futures(db: Session, collector: YFinanceCollector | None = None) -> int | None:
    """Fetch and store every futures contract. Returns bars written, or
    None when the Yahoo request or the save failed (so callers can tell
    failure from "nothing new")."""
    collector = collector or YFinanceCollector()
    period = _BACKFILL_PERIOD if _needs_backfill(db) else _INCREMENTAL_PERIOD
    closes = collector.get_futures_closes([meta[2] for meta in FUTURES.values()], period=period)
    if closes is None:
        record_source(db, "yahoo_futures", False, "Yahoo download failed (see updater/dashboard log)")
        return None
    series = zar_closes(closes)
    missing = [meta[2] for meta in FUTURES.values() if meta[1] not in series]
    if missing:
        logger.warning(f"Yahoo returned no futures closes for: {', '.join(missing)}")
    try:
        written = save_zar_closes(db, series)
    except SQLAlchemyError as e:
        # Never take the Commodities page (or the daily job) down with it -
        # roll back so the shared session stays usable for the metals.
        db.rollback()
        logger.error(f"Saving commodity futures failed: {e}")
        record_source(db, "yahoo_futures", False, f"save failed: {e.__class__.__name__}")
        return None
    record_source(db, "yahoo_futures", True, f"missing: {', '.join(missing)}" if missing else None)
    logger.info(f"Commodity futures refresh ({period}): {written} bars written")
    return written
