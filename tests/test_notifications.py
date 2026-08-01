from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest

from app.models.notification import Notification
from app.models.stock import Stock, StockPrice
from app.models.watchlist import WatchlistItem
from app.notifications.notification_service import NotificationService


def _service(db_session, monkeypatch):
    """Create a NotificationService with notifications enabled and apprise mocked."""
    monkeypatch.setattr(
        "app.notifications.notification_service.settings",
        SimpleNamespace(notification_enabled=True),
    )
    monkeypatch.setattr("app.notifications.notification_service.apprise", MagicMock())
    monkeypatch.setattr(NotificationService, "_setup_notification_channels", lambda self: None)
    return NotificationService(db_session)


def _seed_stock_and_watchlist(db_session, symbol="NPN", **watchlist_kwargs):
    stock = db_session.query(Stock).filter_by(symbol=symbol).first()
    if not stock:
        stock = Stock(symbol=symbol, name="Naspers")
        db_session.add(stock)
        db_session.flush()

    defaults = {"symbol": symbol, "stock_id": stock.id, "is_active": True}
    defaults.update(watchlist_kwargs)
    item = WatchlistItem(**defaults)
    db_session.add(item)
    db_session.commit()
    return stock, item


def test_price_target_alert(db_session, monkeypatch):
    _seed_stock_and_watchlist(db_session, target_price=150.0)
    price = StockPrice(
        stock_id=db_session.query(Stock).filter_by(symbol="NPN").first().id,
        symbol="NPN",
        timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
        close_price=155.0,
    )
    db_session.add(price)
    db_session.commit()

    service = _service(db_session, monkeypatch)
    service.check_price_alerts()

    note = db_session.query(Notification).filter_by(stock_symbol="NPN").first()
    assert note is not None
    assert "Target Price Reached" in note.title
    assert note.type == "price"


def test_stop_loss_alert(db_session, monkeypatch):
    _seed_stock_and_watchlist(db_session, stop_loss=100.0)
    price = StockPrice(
        stock_id=db_session.query(Stock).filter_by(symbol="NPN").first().id,
        symbol="NPN",
        timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
        close_price=95.0,
    )
    db_session.add(price)
    db_session.commit()

    service = _service(db_session, monkeypatch)
    service.check_price_alerts()

    note = db_session.query(Notification).filter_by(stock_symbol="NPN").first()
    assert note is not None
    assert "Stop Loss Triggered" in note.title


def test_price_increase_alert(db_session, monkeypatch):
    _seed_stock_and_watchlist(
        db_session,
        notify_price_increase=True,
        price_change_threshold=5.0,
    )
    stock_id = db_session.query(Stock).filter_by(symbol="NPN").first().id
    prev = StockPrice(
        stock_id=stock_id,
        symbol="NPN",
        timestamp=datetime(2024, 1, 14, 10, 0, 0, tzinfo=timezone.utc),
        close_price=100.0,
    )
    latest = StockPrice(
        stock_id=stock_id,
        symbol="NPN",
        timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
        close_price=106.0,
    )
    db_session.add(prev)
    db_session.add(latest)
    db_session.commit()

    service = _service(db_session, monkeypatch)
    service.check_price_alerts()

    note = db_session.query(Notification).filter_by(stock_symbol="NPN").first()
    assert note is not None
    assert "Price Increase Alert" in note.title


def test_rsi_overbought_alert(db_session, monkeypatch):
    _seed_stock_and_watchlist(db_session)
    price = StockPrice(
        stock_id=db_session.query(Stock).filter_by(symbol="NPN").first().id,
        symbol="NPN",
        timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
        close_price=200.0,
        rsi=75.0,
    )
    db_session.add(price)
    db_session.commit()

    service = _service(db_session, monkeypatch)
    service.check_technical_alerts()

    note = db_session.query(Notification).filter_by(stock_symbol="NPN").first()
    assert note is not None
    assert "RSI Overbought" in note.title
    assert note.type == "technical"


def test_macd_bullish_crossover_alert(db_session, monkeypatch):
    _seed_stock_and_watchlist(db_session)
    stock_id = db_session.query(Stock).filter_by(symbol="NPN").first().id
    prev = StockPrice(
        stock_id=stock_id,
        symbol="NPN",
        timestamp=datetime(2024, 1, 14, 10, 0, 0, tzinfo=timezone.utc),
        close_price=100.0,
        macd=-1.0,
        macd_signal=0.5,
        rsi=50.0,
    )
    latest = StockPrice(
        stock_id=stock_id,
        symbol="NPN",
        timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
        close_price=102.0,
        macd=2.0,
        macd_signal=1.0,
        rsi=50.0,
    )
    db_session.add(prev)
    db_session.add(latest)
    db_session.commit()

    service = _service(db_session, monkeypatch)
    service.check_technical_alerts()

    note = db_session.query(Notification).filter_by(stock_symbol="NPN").first()
    assert note is not None
    assert "MACD Bullish Crossover" in note.title
