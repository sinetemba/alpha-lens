"""Home page movers: honest failure text and the tracked-shares fallback."""
from datetime import datetime

from app.dashboard.home import _movers_failure_text, _tracked_movers
from app.models.stock import Stock, StockPrice
from app.models.watchlist import WatchlistItem


def test_server_errors_are_not_blamed_on_the_users_connection():
    text = _movers_failure_text("502 Server Error: Bad Gateway for url: https://cache.moneyweb.co.za/mny-mover-tables.php")
    assert "HTTP 502" in text and "their side" in text and "connection" not in text


def test_network_failures_point_at_the_connection():
    assert "internet connection" in _movers_failure_text("ConnectionError: Max retries exceeded")
    assert "HTTP 403" in _movers_failure_text("403 Client Error: Forbidden")


def _stock(db, symbol, closes):
    stock = Stock(symbol=symbol, name=f"{symbol} Ltd")
    db.add(stock)
    db.flush()
    db.add(WatchlistItem(symbol=symbol, stock_id=stock.id, is_active=True))
    for day, close in zip((19, 20), closes):
        db.add(StockPrice(stock_id=stock.id, symbol=symbol, timestamp=datetime(2026, 11, day), close_price=close))
    db.commit()


def test_fallback_ranks_tracked_shares_by_last_session_move(db_session):
    _stock(db_session, "UPP", (10.0, 11.0))     # +10%
    _stock(db_session, "DWN", (10.0, 9.0))      # -10%
    _stock(db_session, "FLT", (5.0, 5.0))       # unchanged - left out
    _stock(db_session, "SML", (10.0, 10.2))     # +2%
    winners, losers = _tracked_movers(db_session)
    assert [w["Symbol"] for w in winners] == ["UPP - UPP Ltd", "SML - SML Ltd"]
    assert [l["Symbol"] for l in losers] == ["DWN - DWN Ltd"]
    assert round(losers[0]["Change"], 2) == -10.0
