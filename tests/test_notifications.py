from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest

from app.models.notification import Notification
from app.models.news import NewsArticle
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


def test_penny_price_alert_keeps_sub_rand_decimals(db_session, monkeypatch):
    """A R0.0085 move used to read "Previous: R 0.01, Current: R 0.01"."""
    _seed_stock_and_watchlist(
        db_session, symbol="SKA",
        notify_price_increase=True, price_change_threshold=10.0, target_price=0.009,
    )
    stock_id = db_session.query(Stock).filter_by(symbol="SKA").first().id
    for day, close in ((14, 0.0075), (15, 0.0095)):
        db_session.add(StockPrice(
            stock_id=stock_id, symbol="SKA", close_price=close,
            timestamp=datetime(2024, 1, day, 10, 0, 0, tzinfo=timezone.utc),
        ))
    db_session.commit()

    _service(db_session, monkeypatch).check_price_alerts()

    notes = {n.title.split(":")[0]: n.message
             for n in db_session.query(Notification).filter_by(stock_symbol="SKA")}
    assert "Previous: R 0.0075, Current: R 0.0095" in notes["Price Increase Alert"]
    assert "target price of R 0.0090. Current price: R 0.0095" in notes["Target Price Reached"]


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


def _seed_article(db_session, symbol="NPN", title="Naspers posts strong results"):
    article = NewsArticle(
        title=title,
        summary="Naspers beat expectations.",
        url=f"https://example.com/{title.replace(' ', '-')}",
        source="Moneyweb",
        published_at=datetime(2024, 1, 15, 9, 0, 0),
        stock_symbol=symbol,
    )
    db_session.add(article)
    db_session.commit()
    return article


def test_news_alert_fires_for_watchlisted_symbol(db_session, monkeypatch):
    _seed_stock_and_watchlist(db_session, notify_news=True)
    _seed_article(db_session)

    service = _service(db_session, monkeypatch)
    service.check_news_alerts()

    note = db_session.query(Notification).filter_by(type="news").one()
    assert note.stock_symbol == "NPN"
    assert "strong results" in note.title


def test_news_alert_respects_notify_news_flag(db_session, monkeypatch):
    _seed_stock_and_watchlist(db_session, notify_news=False)
    _seed_article(db_session)

    service = _service(db_session, monkeypatch)
    service.check_news_alerts()

    assert db_session.query(Notification).filter_by(type="news").count() == 0


def test_news_alert_ignores_non_watchlisted_symbol(db_session, monkeypatch):
    _seed_stock_and_watchlist(db_session, notify_news=True)
    _seed_article(db_session, symbol="SOL", title="Sasol rallies")

    service = _service(db_session, monkeypatch)
    service.check_news_alerts()

    assert db_session.query(Notification).filter_by(type="news").count() == 0


def test_news_alert_dedupes_repeat_checks(db_session, monkeypatch):
    _seed_stock_and_watchlist(db_session, notify_news=True)
    _seed_article(db_session)

    service = _service(db_session, monkeypatch)
    service.check_news_alerts()
    service.check_news_alerts()

    assert db_session.query(Notification).filter_by(type="news").count() == 1


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


def test_disabled_delivery_marks_notifications_in_app(db_session, monkeypatch):
    """With NOTIFICATION_ENABLED=false alerts are in-app only: stamp them
    sent at creation so they never queue for a later backlog flush."""
    monkeypatch.setattr(
        "app.notifications.notification_service.settings",
        SimpleNamespace(notification_enabled=False),
    )
    service = NotificationService(db_session)
    note = service.create_notification("system", "T", "body", "NPN")
    assert note.is_sent is True
    assert note.delivery_channel == "in-app"


def test_enabled_delivery_leaves_notifications_pending(db_session, monkeypatch):
    note = _service(db_session, monkeypatch).create_notification(
        "system", "T", "body", "NPN"
    )
    assert note.is_sent is False


def test_send_pending_skips_stale_backlog(db_session, monkeypatch):
    """Rows older than 24h are never attempted - a stale backlog must not
    flood delivery when it's enabled later."""
    old = Notification(
        type="system", title="old", message="m", is_sent=False,
        created_at=datetime(2024, 1, 1),
    )
    db_session.add(old)
    db_session.commit()

    service = _service(db_session, monkeypatch)
    service.send_pending_notifications()

    db_session.refresh(old)
    assert old.is_sent is False
    assert (old.attempts or 0) == 0


def test_send_pending_delivers_fresh_rows(db_session, monkeypatch):
    note = Notification(type="system", title="fresh", message="m", is_sent=False)
    db_session.add(note)
    db_session.commit()

    service = _service(db_session, monkeypatch)
    service.send_pending_notifications()

    db_session.refresh(note)
    assert note.is_sent is True
