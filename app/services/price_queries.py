"""Shared price-query helpers.

Kept free of Streamlit imports so both the dashboard and the headless updater
(app/services/updater.py via NotificationService) can use them. Callers that
want caching wrap these in their own @st.cache_data functions.
"""

from datetime import datetime, timedelta
from typing import NamedTuple, Optional

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.models.stock import StockPrice


class PriceSnapshot(NamedTuple):
    """Lightweight, hashable price row for cache-friendly lookups."""
    close_price: float
    timestamp: datetime
    volume: Optional[int] = None
    rsi: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    high_price: Optional[float] = None
    low_price: Optional[float] = None


def day_of(db: Session, col):
    """Return a cross-DB expression for the date (day) of a timestamp column.

    SQLite uses strftime; PostgreSQL uses date_trunc. The choice is made from
    the session's engine dialect, not settings, so tests running on SQLite
    still work when .env points at PostgreSQL.
    """
    if db.bind.dialect.name == "sqlite":
        return func.strftime('%Y-%m-%d', col)
    return func.date_trunc('day', col)


# How far back from each symbol's own latest bar to look for the "previous
# trading day". 92 days covers ~3 months of suspended trading; symbols idle
# longer than that simply show no day-change, which is honest anyway.
_PREV_DAY_WINDOW = timedelta(days=92)


def get_latest_and_previous_prices(
    db: Session, symbols: list[str]
) -> dict[str, tuple[Optional[PriceSnapshot], Optional[PriceSnapshot]]]:
    """Batch-fetch each symbol's latest price and its most recent prior-day price.

    A per-symbol MAX() (index-backed) finds each symbol's latest bar first;
    the windowed CTE then only ranks rows inside _PREV_DAY_WINDOW of that
    bar instead of scanning each symbol's entire history, which grows
    linearly as daily bars accumulate.
    """
    if not symbols:
        return {}

    latest_per_symbol = db.query(
        StockPrice.symbol,
        func.max(StockPrice.timestamp),
    ).filter(
        StockPrice.symbol.in_(symbols)
    ).group_by(StockPrice.symbol).all()

    if not latest_per_symbol:
        return {}

    scoped = or_(*[
        and_(
            StockPrice.symbol == symbol,
            StockPrice.timestamp >= max_ts - _PREV_DAY_WINDOW,
        )
        for symbol, max_ts in latest_per_symbol if max_ts is not None
    ])

    ranked = db.query(
        StockPrice.symbol,
        StockPrice.timestamp,
        StockPrice.close_price,
        StockPrice.volume,
        StockPrice.rsi,
        StockPrice.macd,
        StockPrice.macd_signal,
        StockPrice.high_price,
        StockPrice.low_price,
        func.dense_rank().over(
            partition_by=StockPrice.symbol,
            order_by=day_of(db, StockPrice.timestamp).desc(),
        ).label('day_rank'),
        func.row_number().over(
            partition_by=[StockPrice.symbol, day_of(db, StockPrice.timestamp)],
            order_by=StockPrice.timestamp.desc(),
        ).label('intra_rank'),
    ).filter(scoped).cte('ranked')

    rows = db.query(
        ranked.c.symbol,
        ranked.c.timestamp,
        ranked.c.close_price,
        ranked.c.volume,
        ranked.c.rsi,
        ranked.c.macd,
        ranked.c.macd_signal,
        ranked.c.high_price,
        ranked.c.low_price,
        ranked.c.day_rank,
    ).filter(or_(
        and_(ranked.c.day_rank == 1, ranked.c.intra_rank == 1),
        and_(ranked.c.day_rank == 2, ranked.c.intra_rank == 1),
    )).order_by(ranked.c.symbol, ranked.c.day_rank).all()

    result: dict[str, tuple[Optional[PriceSnapshot], Optional[PriceSnapshot]]] = {}
    for row in rows:
        sym = row.symbol
        latest, prev = result.get(sym, (None, None))
        snapshot = PriceSnapshot(
            close_price=row.close_price,
            timestamp=row.timestamp,
            volume=row.volume,
            rsi=row.rsi,
            macd=row.macd,
            macd_signal=row.macd_signal,
            high_price=row.high_price,
            low_price=row.low_price,
        )
        if row.day_rank == 1:
            latest = snapshot
        else:
            prev = snapshot
        result[sym] = (latest, prev)
    return result
