"""Tests for init_db._enforce_unique_indexes - the destructive dedupe +
unique-index enforcement that runs on existing databases at startup."""
import pytest
from datetime import datetime
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.models.stock import Stock, StockPrice
from app.models.watchlist import WatchlistItem
import app.database.init_db as init_db


@pytest.fixture()
def legacy_db(tmp_path, monkeypatch):
    """A temp-file SQLite DB where the migration-managed unique indexes are
    absent, simulating a database created before they were enforced.

    Yields (engine, Session) with init_db.engine monkeypatched to it.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    # Simulate a legacy schema: the (symbol, timestamp) unique index is a
    # migration-created index, not present on old databases.
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS ix_stock_prices_symbol_timestamp"))
    monkeypatch.setattr(init_db, "engine", engine)
    yield engine, Session
    engine.dispose()


def test_dedupe_keeps_most_complete_row(legacy_db):
    engine, Session = legacy_db
    db = Session()
    stock = Stock(symbol="NPN", name="Naspers")
    db.add(stock)
    db.flush()
    ts = datetime(2024, 1, 15)
    # Sparse row first (older id), complete row second - dedupe must keep
    # the COMPLETE one (most non-null fields), not just the newest.
    db.add(StockPrice(stock_id=stock.id, symbol="NPN", timestamp=ts, close_price=100.0))
    db.add(StockPrice(
        stock_id=stock.id, symbol="NPN", timestamp=ts, close_price=100.5,
        open_price=99.0, high_price=101.0, low_price=98.5, volume=1500,
        adjusted_close=100.5, fetched_at=datetime(2024, 1, 15, 12),
    ))
    db.commit()
    db.close()

    init_db._enforce_unique_indexes(inspect(engine))

    db = Session()
    rows = db.query(StockPrice).filter_by(symbol="NPN").all()
    assert len(rows) == 1
    assert rows[0].volume == 1500
    assert rows[0].adjusted_close == pytest.approx(100.5)
    db.close()


def test_unique_index_enforced_after_migration(legacy_db):
    engine, Session = legacy_db
    init_db._enforce_unique_indexes(inspect(engine))
    assert "ix_stock_prices_symbol_timestamp" in {
        ix["name"] for ix in inspect(engine).get_indexes("stock_prices")
    }

    db = Session()
    stock = Stock(symbol="NPN", name="Naspers")
    db.add(stock)
    db.flush()
    ts = datetime(2024, 1, 15)
    db.add(StockPrice(stock_id=stock.id, symbol="NPN", timestamp=ts, close_price=100.0))
    db.commit()
    db.add(StockPrice(stock_id=stock.id, symbol="NPN", timestamp=ts, close_price=101.0))
    with pytest.raises(IntegrityError):
        db.commit()
    db.close()


def test_watchlist_soft_dedupe_deactivates_older_rows(legacy_db):
    """Watchlist dupes are deactivated, not deleted: history is kept but the
    partial unique index only guards active rows."""
    engine, Session = legacy_db
    db = Session()
    stock = Stock(symbol="SOL", name="Sasol")
    db.add(stock)
    db.flush()
    older = WatchlistItem(stock_id=stock.id, symbol="SOL", is_active=True)
    newer = WatchlistItem(stock_id=stock.id, symbol="SOL", is_active=True)
    db.add_all([older, newer])
    db.commit()
    stock_id, older_id, newer_id = stock.id, older.id, newer.id
    db.close()

    init_db._enforce_unique_indexes(inspect(engine))

    db = Session()
    rows = {r.id: r for r in db.query(WatchlistItem).filter_by(symbol="SOL")}
    assert rows[older_id].is_active is False or rows[older_id].is_active == 0
    assert rows[newer_id].is_active is True or rows[newer_id].is_active == 1
    # The partial index now rejects a second ACTIVE row for the symbol...
    db.add(WatchlistItem(stock_id=stock_id, symbol="SOL", is_active=True))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    # ...but an inactive one is allowed (re-add history preserved).
    db.add(WatchlistItem(stock_id=stock_id, symbol="SOL", is_active=False))
    db.commit()
    db.close()
