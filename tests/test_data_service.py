import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from app.services.data_service import DataService
from app.models.stock import Stock, StockPrice


def test_save_price_from_quote_deduplicates(db_session):
    stock = Stock(symbol="NPN", name="Naspers")
    db_session.add(stock)
    db_session.commit()

    data_service = DataService(db_session)
    quote = {
        "timestamp": "2024-01-15 10:00:00",
        "open": "100.00",
        "high": "105.00",
        "low": "99.00",
        "close": "102.50",
        "volume": "1000",
    }

    data_service._save_price_from_quote("NPN", quote)
    data_service._save_price_from_quote("NPN", quote)

    prices = db_session.query(StockPrice).filter_by(symbol="NPN").all()
    assert len(prices) == 1
    assert prices[0].close_price == pytest.approx(102.50)


def test_save_historical_data_backfills_and_deduplicates(db_session):
    stock = Stock(symbol="CFR", name="Richemont")
    db_session.add(stock)
    db_session.commit()

    data_service = DataService(db_session)
    points = [
        {
            "datetime": "2024-01-10",
            "open": "100.0",
            "high": "105.0",
            "low": "99.0",
            "close": "102.0",
            "volume": "1000",
        },
        {
            "datetime": "2024-01-11",
            "open": "102.0",
            "high": "107.0",
            "low": "101.0",
            "close": "106.0",
            "volume": "1500",
        },
        {
            "datetime": "2024-01-12",
            "open": "106.0",
            "high": "108.0",
            "low": "104.0",
            "close": "107.0",
            "volume": "1200",
        },
    ]

    saved = data_service._save_historical_data("CFR", points)
    assert saved == 3

    saved_again = data_service._save_historical_data("CFR", points)
    assert saved_again == 0

    prices = db_session.query(StockPrice).filter_by(symbol="CFR").order_by(StockPrice.timestamp).all()
    assert len(prices) == 3
    assert prices[0].close_price == pytest.approx(102.0)
    assert prices[-1].close_price == pytest.approx(107.0)


def test_update_historical_data_calls_twelve_data(db_session, monkeypatch):
    stock = Stock(symbol="AGL", name="Anglo American")
    db_session.add(stock)
    db_session.commit()

    data_service = DataService(db_session)
    data_service.twelve_data = MagicMock()
    data_service.yfinance = MagicMock()

    points = [
        {
            "datetime": "2024-01-10",
            "open": "100.0",
            "high": "105.0",
            "low": "99.0",
            "close": "102.0",
            "volume": "1000",
        },
        {
            "datetime": "2024-01-11",
            "open": "102.0",
            "high": "107.0",
            "low": "101.0",
            "close": "106.0",
            "volume": "1500",
        },
    ]
    data_service.twelve_data.get_historical_data.return_value = points

    saved = data_service.update_historical_data("AGL")

    assert saved == 2
    data_service.twelve_data.get_historical_data.assert_called_once()
    data_service.yfinance.get_history_date_range.assert_not_called()


def test_update_technical_indicators_populates_columns(db_session):
    stock = Stock(symbol="SOL", name="Sasol")
    db_session.add(stock)
    db_session.flush()

    base = datetime(2023, 1, 1, tzinfo=timezone.utc)
    for i in range(250):
        price = StockPrice(
            stock_id=stock.id,
            symbol="SOL",
            timestamp=base + timedelta(days=i),
            open_price=100.0 + i,
            high_price=101.0 + i,
            low_price=99.0 + i,
            close_price=100.0 + i,
            volume=1000,
        )
        db_session.add(price)
    db_session.commit()

    data_service = DataService(db_session)
    data_service._update_technical_indicators("SOL")

    latest = db_session.query(StockPrice).filter_by(symbol="SOL").order_by(StockPrice.timestamp.desc()).first()
    assert latest.sma_20 is not None
    assert latest.rsi is not None
