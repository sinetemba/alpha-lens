"""Portfolio Action column: shares that don't trade get no trend signal."""
from datetime import datetime, timedelta

from app.dashboard.portfolio import _holding_price_stats
from app.models.stock import Stock, StockPrice


def _series(db, symbol, closes, volumes):
    stock = Stock(symbol=symbol, name=symbol)
    db.add(stock)
    db.flush()
    start = datetime.now() - timedelta(days=len(closes))
    for i, (c, v) in enumerate(zip(closes, volumes)):
        db.add(StockPrice(stock_id=stock.id, symbol=symbol, timestamp=start + timedelta(days=i),
                          close_price=c, volume=v))
    db.commit()


def test_flat_zero_volume_share_is_not_trading(db_session):
    """EEL: R0.03 with zero volume on 20 of 20 sessions."""
    _series(db_session, "EEL", [0.05] * 40 + [0.03] * 20, [100] * 40 + [0] * 20)
    assert _holding_price_stats(db_session, ["EEL"])["EEL"]["not_trading"]


def test_thin_but_moving_share_keeps_its_signals(db_session):
    """PMV: 14 zero-volume days but 5 different prices - it does trade."""
    closes = [3.0] * 40 + [3.1, 3.1, 3.2, 3.2, 3.3] * 4
    volumes = [500] * 40 + [0, 0, 0, 900, 0] * 3 + [900, 0, 900, 900, 900]
    _series(db_session, "PMV", closes, volumes)
    assert not _holding_price_stats(db_session, ["PMV"])["PMV"]["not_trading"]


def test_an_actively_traded_share_is_trading(db_session):
    _series(db_session, "AXX", [1.4 + i * 0.01 for i in range(60)], [1000] * 60)
    assert not _holding_price_stats(db_session, ["AXX"])["AXX"]["not_trading"]
