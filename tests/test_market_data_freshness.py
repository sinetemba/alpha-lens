"""Tests for get_latest_market_data_timestamp's fetched_at/timestamp mixing
in app/dashboard/utils.py."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import update

from app.config.settings import settings
from app.dashboard.utils import get_latest_market_data_timestamp
from app.models.stock import Stock, StockPrice


def test_bar_timestamp_is_converted_from_exchange_local_to_utc(db_session):
    """`timestamp` is naive exchange-local midnight, not a UTC instant like
    fetched_at. A bar with no fetched_at must be read back as the UTC instant
    for that exchange-local midnight, not as a naive-UTC literal of the same
    clock digits (which would look up to a UTC-offset's worth newer)."""
    stock = Stock(symbol="NPN", name="NPN")
    db_session.add(stock)
    db_session.flush()
    bar_date = datetime(2024, 6, 1)
    db_session.add(StockPrice(stock_id=stock.id, symbol="NPN", timestamp=bar_date, close_price=100.0))
    db_session.commit()
    # Column default fills fetched_at even for an explicit None, so null it
    # out at the DB level to simulate a pre-fetched_at legacy row.
    db_session.execute(update(StockPrice).where(StockPrice.symbol == "NPN").values(fetched_at=None))
    db_session.commit()

    result = get_latest_market_data_timestamp(db_session, ["NPN"])

    expected = (
        bar_date.replace(tzinfo=ZoneInfo(settings.scheduler_timezone))
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )
    assert result == expected
    assert result != bar_date


def test_fetched_at_still_wins_when_more_recent(db_session):
    stock = Stock(symbol="NPN", name="NPN")
    db_session.add(stock)
    db_session.flush()
    fetched = datetime(2024, 6, 2, 10, 0)
    db_session.add(StockPrice(
        stock_id=stock.id, symbol="NPN",
        timestamp=datetime(2024, 6, 1), close_price=100.0, fetched_at=fetched,
    ))
    db_session.commit()

    result = get_latest_market_data_timestamp(db_session, ["NPN"])

    assert result == fetched


def test_no_rows_returns_none(db_session):
    assert get_latest_market_data_timestamp(db_session, ["NPN"]) is None


def test_empty_symbols_list_returns_none(db_session):
    assert get_latest_market_data_timestamp(db_session, []) is None
