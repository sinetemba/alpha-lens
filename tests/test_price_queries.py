"""Tests for the windowed-CTE latest/previous price queries in
app/dashboard/home.py and app/dashboard/portfolio.py."""
from datetime import datetime

import pytest

from app.dashboard.utils import day_of
from app.dashboard.home import _get_latest_and_previous_prices
from app.dashboard.portfolio import _get_group_latest_and_previous_prices
from app.models.stock import Stock, StockPrice


@pytest.fixture(autouse=True)
def _clear_price_caches():
    # Both functions key their st.cache_data cache on id(db.bind); each test
    # gets a brand-new engine from the db_session fixture, so a GC'd id()
    # could collide with a previous test's cache entry without this.
    _get_latest_and_previous_prices.clear()
    _get_group_latest_and_previous_prices.clear()
    yield
    _get_latest_and_previous_prices.clear()
    _get_group_latest_and_previous_prices.clear()


def _add_stock(db, symbol):
    stock = Stock(symbol=symbol, name=symbol)
    db.add(stock)
    db.flush()
    return stock


def _add_price(db, stock, symbol, timestamp, close_price, volume=None, rsi=None, macd=None, macd_signal=None):
    price = StockPrice(
        stock_id=stock.id,
        symbol=symbol,
        timestamp=timestamp,
        close_price=close_price,
        volume=volume,
        rsi=rsi,
        macd=macd,
        macd_signal=macd_signal,
    )
    db.add(price)
    db.flush()
    return price


class FakeDialect:
    def __init__(self, name):
        self.name = name


class FakeBind:
    def __init__(self, dialect_name):
        self.dialect = FakeDialect(dialect_name)


class FakeSession:
    def __init__(self, dialect_name):
        self.bind = FakeBind(dialect_name)


def test_day_of_sqlite_uses_strftime(db_session):
    expr = day_of(db_session, StockPrice.timestamp)
    assert "strftime" in str(expr).lower()


def test_day_of_postgresql_uses_date_trunc():
    fake_db = FakeSession("postgresql")
    expr = day_of(fake_db, StockPrice.timestamp)
    assert "date_trunc" in str(expr).lower()


@pytest.mark.parametrize(
    "price_query",
    [_get_latest_and_previous_prices, _get_group_latest_and_previous_prices],
)
def test_empty_symbol_list_returns_empty_dict(db_session, price_query):
    assert price_query(db_session, []) == {}


@pytest.mark.parametrize(
    "price_query",
    [_get_latest_and_previous_prices, _get_group_latest_and_previous_prices],
)
def test_missing_symbol_is_absent_from_result(db_session, price_query):
    result = price_query(db_session, ["DOESNOTEXIST"])
    assert result == {}


@pytest.mark.parametrize(
    "price_query",
    [_get_latest_and_previous_prices, _get_group_latest_and_previous_prices],
)
def test_latest_and_previous_day_resolved(db_session, price_query):
    stock = _add_stock(db_session, "NPN")
    _add_price(db_session, stock, "NPN", datetime(2024, 1, 1), 90.0)
    _add_price(db_session, stock, "NPN", datetime(2024, 1, 2), 100.0)
    db_session.commit()

    result = price_query(db_session, ["NPN"])
    latest, prev = result["NPN"]

    assert latest.close_price == 100.0
    assert latest.timestamp == datetime(2024, 1, 2)
    assert prev.close_price == 90.0
    assert prev.timestamp == datetime(2024, 1, 1)


@pytest.mark.parametrize(
    "price_query",
    [_get_latest_and_previous_prices, _get_group_latest_and_previous_prices],
)
def test_only_one_day_of_data_has_no_previous(db_session, price_query):
    stock = _add_stock(db_session, "NPN")
    _add_price(db_session, stock, "NPN", datetime(2024, 1, 2), 100.0)
    db_session.commit()

    result = price_query(db_session, ["NPN"])
    latest, prev = result["NPN"]

    assert latest.close_price == 100.0
    assert prev is None


@pytest.mark.parametrize(
    "price_query",
    [_get_latest_and_previous_prices, _get_group_latest_and_previous_prices],
)
def test_intraday_bars_pick_most_recent_per_day(db_session, price_query):
    """Multiple bars on the same calendar day must collapse to the
    most-recent (highest timestamp) bar for both the latest and prior day,
    exercising the intra_rank window in the CTE."""
    stock = _add_stock(db_session, "NPN")
    # Prior day: two intraday bars, latest one (15:00) should win.
    _add_price(db_session, stock, "NPN", datetime(2024, 1, 1, 9, 0), 88.0)
    _add_price(db_session, stock, "NPN", datetime(2024, 1, 1, 15, 0), 90.0)
    # Latest day: two intraday bars, latest one (16, 0) should win.
    _add_price(db_session, stock, "NPN", datetime(2024, 1, 2, 9, 0), 99.0)
    _add_price(db_session, stock, "NPN", datetime(2024, 1, 2, 16, 0), 100.0)
    db_session.commit()

    result = price_query(db_session, ["NPN"])
    latest, prev = result["NPN"]

    assert latest.close_price == 100.0
    assert latest.timestamp == datetime(2024, 1, 2, 16, 0)
    assert prev.close_price == 90.0
    assert prev.timestamp == datetime(2024, 1, 1, 15, 0)


@pytest.mark.parametrize(
    "price_query",
    [_get_latest_and_previous_prices, _get_group_latest_and_previous_prices],
)
def test_gap_days_skip_to_most_recent_prior_day(db_session, price_query):
    """A symbol with no bar yesterday should still resolve 'previous' to the
    most recent earlier day that does have data, not require exactly -1 day."""
    stock = _add_stock(db_session, "NPN")
    _add_price(db_session, stock, "NPN", datetime(2023, 12, 1), 50.0)
    _add_price(db_session, stock, "NPN", datetime(2024, 1, 10), 100.0)
    db_session.commit()

    result = price_query(db_session, ["NPN"])
    latest, prev = result["NPN"]

    assert latest.close_price == 100.0
    assert prev.close_price == 50.0


@pytest.mark.parametrize(
    "price_query",
    [_get_latest_and_previous_prices, _get_group_latest_and_previous_prices],
)
def test_multiple_symbols_batched_independently(db_session, price_query):
    npn = _add_stock(db_session, "NPN")
    cfr = _add_stock(db_session, "CFR")
    _add_price(db_session, npn, "NPN", datetime(2024, 1, 1), 90.0)
    _add_price(db_session, npn, "NPN", datetime(2024, 1, 2), 100.0)
    _add_price(db_session, cfr, "CFR", datetime(2024, 1, 2), 200.0)
    db_session.commit()

    result = price_query(db_session, ["NPN", "CFR"])

    npn_latest, npn_prev = result["NPN"]
    cfr_latest, cfr_prev = result["CFR"]
    assert npn_latest.close_price == 100.0 and npn_prev.close_price == 90.0
    assert cfr_latest.close_price == 200.0 and cfr_prev is None


def test_home_and_portfolio_cte_implementations_agree(db_session):
    """home.py and portfolio.py each define their own copy of this CTE query -
    they must stay in lockstep, so assert they return identical results."""
    stock = _add_stock(db_session, "NPN")
    _add_price(db_session, stock, "NPN", datetime(2024, 1, 1, 9, 0), 88.0)
    _add_price(db_session, stock, "NPN", datetime(2024, 1, 1, 15, 0), 90.0)
    _add_price(db_session, stock, "NPN", datetime(2024, 1, 2), 100.0)
    db_session.commit()

    home_result = _get_latest_and_previous_prices(db_session, ["NPN"])
    portfolio_result = _get_group_latest_and_previous_prices(db_session, ["NPN"])

    assert home_result == portfolio_result
