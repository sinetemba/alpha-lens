"""Tests for dividend storage in DataService.update_dividends."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.models.dividend import Dividend
from app.models.stock import Stock, StockPrice
from app.services.data_service import DataService


def _yahoo_series(points):
    """Yahoo-style dividend Series: tz-aware exchange-local index -> amount."""
    index = pd.DatetimeIndex(
        [pd.Timestamp(d).tz_localize("Africa/Johannesburg") + pd.Timedelta(hours=9) for d, _ in points]
    )
    return pd.Series([a for _, a in points], index=index)


@pytest.fixture
def service(db_session):
    svc = DataService(db_session)
    svc.twelve_data = MagicMock()
    svc.twelve_data.get_dividends.return_value = None  # force the Yahoo path
    svc.yfinance = MagicMock()
    return svc


def _stock_with_close(db, symbol, close, on):
    stock = Stock(symbol=symbol, name=symbol)
    db.add(stock)
    db.flush()
    db.add(StockPrice(stock_id=stock.id, symbol=symbol, timestamp=on, close_price=close))
    db.commit()
    return stock


def _amounts(db, symbol):
    rows = db.query(Dividend).filter_by(symbol=symbol).order_by(Dividend.ex_dividend_date).all()
    return [(r.ex_dividend_date, r.amount) for r in rows]


@pytest.mark.parametrize("amount, expected", [
    (142.37, 1.4237),  # cents: > 50% of a R40 share price
    (1.42, 1.42),      # already Rand
])
def test_yahoo_amount_rescaled_only_when_implausible(service, db_session, amount, expected):
    _stock_with_close(db_session, "SBK", 40.0, datetime(2026, 3, 1))
    service.yfinance.get_dividends.return_value = _yahoo_series([("2026-03-10", amount)])

    assert service.update_dividends(["SBK"]) == 1

    [(ex_date, stored)] = _amounts(db_session, "SBK")
    assert ex_date == datetime(2026, 3, 10)  # naive midnight, not 09:00
    assert stored == pytest.approx(expected)


def test_old_dividend_not_judged_against_todays_price(service, db_session):
    """A dividend far from any stored close keeps its amount - no reference."""
    _stock_with_close(db_session, "SBK", 2.0, datetime(2026, 3, 1))
    old = datetime(2026, 3, 1) - timedelta(days=1000)
    service.yfinance.get_dividends.return_value = _yahoo_series([(old.date(), 5.0)])

    service.update_dividends(["SBK"])

    assert _amounts(db_session, "SBK")[0][1] == pytest.approx(5.0)


def test_dedup_is_per_calendar_day(service, db_session):
    stock = _stock_with_close(db_session, "SBK", 40.0, datetime(2026, 3, 1))
    # Legacy row stored at 09:00 for the same ex-date.
    db_session.add(Dividend(
        stock_id=stock.id, symbol="SBK", amount=1.42, currency="ZAR",
        ex_dividend_date=datetime(2026, 3, 10, 9, 0),
    ))
    db_session.commit()
    service.yfinance.get_dividends.return_value = _yahoo_series([("2026-03-10", 1.42)])

    assert service.update_dividends(["SBK"]) == 0
    assert len(_amounts(db_session, "SBK")) == 1


def test_twelve_data_fills_missing_dates_on_existing_row(service, db_session):
    stock = _stock_with_close(db_session, "SBK", 40.0, datetime(2026, 3, 1))
    db_session.add(Dividend(
        stock_id=stock.id, symbol="SBK", amount=1.42, currency="ZAR",
        ex_dividend_date=datetime(2026, 3, 10),
    ))
    db_session.commit()
    service.twelve_data.get_dividends.return_value = [
        {"ex_date": "2026-03-10", "amount": 1.42, "payment_date": "2026-03-17"},
    ]

    assert service.update_dividends(["SBK"]) == 0
    row = db_session.query(Dividend).filter_by(symbol="SBK").one()
    assert row.payment_date == datetime(2026, 3, 17)


def test_freshness_gate_skips_recently_fetched_symbols(service, db_session):
    """A symbol fetched within the window isn't re-fetched - the daily job
    used to spend one Yahoo call per tracked symbol every run."""
    from app.models.base import utcnow_naive

    stock = _stock_with_close(db_session, "SBK", 40.0, datetime(2026, 3, 1))
    stock.last_dividend_fetch = utcnow_naive()
    db_session.commit()

    assert service.update_dividends(["SBK"]) == 0
    service.yfinance.get_dividends.assert_not_called()

    # freshness_days=0 is the manual-fetch path - it must bypass the stamp.
    service.yfinance.get_dividends.return_value = _yahoo_series([("2026-03-10", 1.42)])
    assert service.update_dividends(["SBK"], freshness_days=0) == 1
    service.yfinance.get_dividends.assert_called_once_with("SBK")


def test_fetch_stamps_last_dividend_fetch(service, db_session):
    """Even a symbol that pays nothing gets stamped (a Stock row is created)
    so non-payers are not re-fetched every run."""
    from app.models.base import utcnow_naive

    service.yfinance.get_dividends.return_value = None
    service.update_dividends(["NPN"])

    stock = db_session.query(Stock).filter_by(symbol="NPN").one()
    assert stock.last_dividend_fetch is not None
    assert utcnow_naive() - stock.last_dividend_fetch < timedelta(minutes=5)


def test_median_history_rescales_cents_and_keeps_specials(service, db_session):
    """A cent-quoted amount ~100x the stored median is rescaled; a genuine
    larger dividend (under 50x median) is kept."""
    _stock_with_close(db_session, "EXP", 2500.0, datetime(2026, 3, 1))
    stock = db_session.query(Stock).filter_by(symbol="EXP").one()
    # Established Rand-scale dividend history (~R12).
    for i in range(1, 6):
        db_session.add(Dividend(
            stock_id=stock.id, symbol="EXP", amount=12.0, currency="ZAR",
            ex_dividend_date=datetime(2025, i, 20),
        ))
    db_session.commit()
    service.yfinance.get_dividends.return_value = _yahoo_series([
        ("2026-03-10", 1200.0),  # cents: ~100x median -> R12
        ("2026-03-15", 300.0),   # 25x median special -> kept
    ])

    assert service.update_dividends(["EXP"]) == 2
    amounts = _amounts(db_session, "EXP")
    assert amounts[-2][1] == pytest.approx(12.0)
    assert amounts[-1][1] == pytest.approx(300.0)


def test_failure_on_one_symbol_keeps_earlier_symbols(service, db_session):
    _stock_with_close(db_session, "SBK", 40.0, datetime(2026, 3, 1))
    _stock_with_close(db_session, "NPN", 40.0, datetime(2026, 3, 1))

    def _dividends(symbol):
        if symbol == "NPN":
            raise RuntimeError("feed down")
        return _yahoo_series([("2026-03-10", 1.42)])

    service.yfinance.get_dividends.side_effect = _dividends

    service.update_dividends(["SBK", "NPN"])

    assert len(_amounts(db_session, "SBK")) == 1
