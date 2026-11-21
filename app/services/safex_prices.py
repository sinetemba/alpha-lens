"""SAFEX white maize, yellow maize and wheat: front-contract daily prices.

Streamlit-free so the updater's daily historical job can call it too. One
request (Grain SA's SAFEX table, see collectors/safex.py) per refresh.

"The price" of each grain is its front contract: the nearest contract month
AFTER the trading day's month - a contract in its own delivery month thins
out and stops tracking the market (on 2026-11-20 NOV26 traded 96 lots vs
DEC26's 3,055). The series therefore steps at each roll, like the Yahoo
continuous futures. Stored closes are Rand per tonne, keyed by the trading
day's naive midnight.
"""
from datetime import datetime
from typing import Optional

from loguru import logger
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.collectors.safex import SafexCollector, contract_month
from app.models.base import utcnow_naive
from app.models.stock import Stock, StockPrice
from app.services.source_health import record_source

# CBOT contract sizes for the SA-vs-world gap: bushels per metric tonne.
BUSHELS_PER_TONNE_CORN = 39.3679   # 25.401 kg/bushel (also soybeans)
BUSHELS_PER_TONNE_WHEAT = 36.7437  # 27.216 kg/bushel

# key -> (name, stored pseudo-symbol, SAFEX instrument, CBOT db_symbol, bushels/t)
SAFEX_GRAINS = {
    "white_maize": ("White Maize", "SFX_WMAZ", "WMAZ", "YF_CORN", BUSHELS_PER_TONNE_CORN),
    "yellow_maize": ("Yellow Maize", "SFX_YMAZ", "YMAZ", "YF_CORN", BUSHELS_PER_TONNE_CORN),
    "safex_wheat": ("Wheat", "SFX_WEAT", "WEAT", "YF_WHEAT", BUSHELS_PER_TONNE_WHEAT),
}
SAFEX_DB_SYMBOLS = [meta[1] for meta in SAFEX_GRAINS.values()]


def front_contract(rows: list[dict], instrument: str) -> Optional[dict]:
    """The nearest contract whose month is after its trading day's month."""
    candidates = []
    for row in rows:
        if row["instrument"] != instrument:
            continue
        month = contract_month(row["contract"])
        traded = (row["traded_at"].year, row["traded_at"].month)
        if month and month > traded:
            candidates.append((month, row))
    return min(candidates, key=lambda c: c[0])[1] if candidates else None


def _save_bar(db: Session, db_symbol: str, name: str, row: dict, now: datetime) -> None:
    stock = db.query(Stock).filter(Stock.symbol == db_symbol).first()
    if stock is None:
        stock = Stock(symbol=db_symbol, name=name)
        db.add(stock)
        db.flush()
    day = row["traded_at"].replace(hour=0, minute=0, second=0, microsecond=0)
    bar = db.query(StockPrice).filter(
        StockPrice.symbol == db_symbol, StockPrice.timestamp == day
    ).first()
    if bar is None:
        bar = StockPrice(stock_id=stock.id, symbol=db_symbol, timestamp=day, close_price=row["last"])
        db.add(bar)
    # Overwrite in place: the page is re-published intraday and after the close.
    bar.close_price = row["last"]
    bar.adjusted_close = row["last"]
    bar.high_price = row["high"]
    bar.low_price = row["low"]
    bar.volume = row["volume"]
    bar.fetched_at = now


def refresh_safex(db: Session, collector: SafexCollector | None = None) -> Optional[int]:
    """Fetch and store each grain's front-contract price. Returns bars
    written, or None when the fetch or the save failed."""
    rows = (collector or SafexCollector()).get_contracts()
    if rows is None:
        record_source(db, "safex", False, "Grain SA page unreachable or table not found (layout changed?)")
        return None
    now = utcnow_naive()
    written = 0
    try:
        for name, db_symbol, instrument, _cbot, _bu in SAFEX_GRAINS.values():
            row = front_contract(rows, instrument)
            if row is None:
                logger.warning(f"SAFEX feed had no front contract for {instrument}")
                continue
            _save_bar(db, db_symbol, name, row, now)
            logger.debug(f"SAFEX {instrument} {row['contract']}: R{row['last']:,.2f}/t")
            written += 1
        db.commit()
    except IntegrityError:
        # A concurrent refresh wrote today's bar first - keep theirs.
        db.rollback()
        return 0
    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"Saving SAFEX prices failed: {e}")
        record_source(db, "safex", False, f"save failed: {e.__class__.__name__}")
        return None
    record_source(db, "safex", written == len(SAFEX_GRAINS),
                  None if written == len(SAFEX_GRAINS) else f"only {written}/{len(SAFEX_GRAINS)} grains found")
    return written


def cbot_rand_per_tonne(cbot_rand_per_bushel: Optional[float], bushels_per_tonne: float) -> Optional[float]:
    """A stored YF_CORN/YF_WHEAT close (R/bushel) in R/tonne."""
    if not cbot_rand_per_bushel or cbot_rand_per_bushel <= 0:
        return None
    return cbot_rand_per_bushel * bushels_per_tonne
