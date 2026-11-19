"""Tests for the headless updater (app/services/updater.py)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config.settings import settings
from app.models import Base
from app.models.portfolio import Portfolio, PortfolioHolding
from app.models.stock import Stock, StockPrice
from app.models.updater_run import UpdaterRun
from app.models.watchlist import WatchlistItem
from app.services import updater
from app.services.updater import (
    UpdaterService,
    _estimate_api_calls,
    _get_tracked_symbols,
    _maybe_deactivate_dead_symbol,
)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def session_factory(monkeypatch):
    """In-memory DB shared across sessions, wired into the updater's SessionLocal."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(updater, "SessionLocal", factory)
    yield factory
    engine.dispose()


@pytest.fixture
def db(session_factory):
    session = session_factory()
    yield session
    session.close()


def _stock(db, symbol, active=True):
    stock = Stock(symbol=symbol, name=symbol, is_active=active)
    db.add(stock)
    db.flush()
    return stock


def _watch(db, stock, active=True):
    db.add(WatchlistItem(stock_id=stock.id, symbol=stock.symbol, is_active=active))


def _hold(db, portfolio, stock, account_type):
    db.add(PortfolioHolding(
        portfolio_id=portfolio.id, stock_id=stock.id, symbol=stock.symbol,
        account_type=account_type, quantity=1, purchase_price=1,
        purchase_date=datetime(2026, 1, 1),
    ))


# --- _get_tracked_symbols ------------------------------------------------

def test_tracked_symbols_filters_inactive_and_non_jse(db):
    portfolio = Portfolio(name="P")
    db.add(portfolio)
    db.flush()
    _watch(db, _stock(db, "NPN"))
    _watch(db, _stock(db, "OLD"), active=False)           # removed from watchlist
    _watch(db, _stock(db, "DEAD", active=False))          # deactivated stock
    _hold(db, portfolio, _stock(db, "SOL"), "ZAR")
    _hold(db, portfolio, _stock(db, "AAPL"), "USD")       # USD account
    _hold(db, portfolio, _stock(db, "DEMO"), "Demo ZAR")  # excluded account
    _hold(db, portfolio, _stock(db, "MAN"), "Manual")
    db.commit()

    assert _get_tracked_symbols(db) == sorted(
        ["NPN", "SOL", "MAN", settings.jse_index_yf_symbol]
    )


def test_tracked_symbols_falls_back_to_default_watchlist(db):
    assert _get_tracked_symbols(db) == sorted(
        set(settings.default_watchlist_symbols) | {settings.jse_index_yf_symbol}
    )


# --- _maybe_deactivate_dead_symbol ---------------------------------------

@pytest.mark.parametrize("age_days, expect_active", [(60, False), (10, True)])
def test_deactivates_only_long_stale_symbols(db, age_days, expect_active):
    stock = _stock(db, "KBO")
    db.add(StockPrice(
        stock_id=stock.id, symbol="KBO", close_price=1.0,
        timestamp=_utcnow() - timedelta(days=age_days),
    ))
    db.commit()

    _maybe_deactivate_dead_symbol(db, "KBO")

    db.refresh(stock)
    assert stock.is_active is expect_active


def test_new_symbol_with_no_bars_is_left_active(db):
    stock = _stock(db, "NEW")
    db.commit()

    _maybe_deactivate_dead_symbol(db, "NEW")

    db.refresh(stock)
    assert stock.is_active is True


def test_long_unpriced_symbol_is_deactivated(db):
    """SGP: held on EasyEquities, never on any price feed in 4 months."""
    stock = _stock(db, "SGP")
    stock.created_at = _utcnow() - timedelta(days=120)
    db.commit()

    _maybe_deactivate_dead_symbol(db, "SGP")

    db.refresh(stock)
    assert stock.is_active is False


def test_estimate_api_calls_counts_credits_per_symbol(monkeypatch):
    monkeypatch.setattr(settings, "twelve_data_enabled", True)
    symbols = [f"S{i}" for i in range(11)]
    assert _estimate_api_calls(symbols) == 11  # batching doesn't save credits
    assert _estimate_api_calls(symbols, include_historical=True) == 11 + 22
    monkeypatch.setattr(settings, "twelve_data_enabled", False)
    assert _estimate_api_calls(symbols, include_historical=True) == 0


# --- historical catch-up -------------------------------------------------

@pytest.fixture
def service(session_factory):
    svc = UpdaterService()
    svc._update_historical = MagicMock(return_value=True)
    return svc


def _heartbeat(db, success, age):
    db.add(UpdaterRun(job_name="daily_historical", success=success, finished_at=_utcnow() - age))
    db.commit()


def test_historical_catch_up_runs_when_never_run(service):
    service._historical_catch_up_if_due()
    service._update_historical.assert_called_once()


def test_historical_catch_up_skips_after_recent_success(service, db):
    _heartbeat(db, True, timedelta(hours=3))
    service._historical_catch_up_if_due()
    service._update_historical.assert_not_called()


def test_historical_catch_up_ignores_recent_failure(service, db):
    _heartbeat(db, True, timedelta(days=6))
    _heartbeat(db, False, timedelta(hours=1))
    service._historical_catch_up_if_due()
    service._update_historical.assert_called_once()


def test_historical_catch_up_is_throttled(service):
    service._update_historical.return_value = False
    service._historical_catch_up_if_due()
    service._historical_catch_up_if_due()  # within the retry window
    service._update_historical.assert_called_once()


def test_historical_job_does_not_overlap(session_factory):
    svc = UpdaterService()
    svc._historical_lock.acquire()
    try:
        assert svc._update_historical() is False
    finally:
        svc._historical_lock.release()


# --- backup scheduling ---------------------------------------------------

def test_backup_if_due_is_throttled(session_factory, monkeypatch):
    monkeypatch.setattr(updater.backup, "backup_due", lambda: True)
    svc = UpdaterService()
    svc._run_backup = MagicMock(return_value=False)

    svc._backup_if_due()
    svc._backup_if_due()

    svc._run_backup.assert_called_once()


def test_backup_not_taken_when_not_due(session_factory, monkeypatch):
    monkeypatch.setattr(updater.backup, "backup_due", lambda: False)
    svc = UpdaterService()
    svc._run_backup = MagicMock()

    svc._backup_if_due()

    svc._run_backup.assert_not_called()


# --- EasyEquities sync gate -----------------------------------------------

def _stub_ee(monkeypatch, enabled=True, synced=2):
    collector = SimpleNamespace(enabled=enabled, last_error=None)
    monkeypatch.setattr(updater, "EasyEquitiesCollector", lambda: collector)
    sync = MagicMock(return_value=(synced, []))
    monkeypatch.setattr(updater, "sync_portfolio_from_easyequities", sync)
    return sync


def test_ee_sync_skipped_when_disabled(db, monkeypatch):
    sync = _stub_ee(monkeypatch, enabled=False)
    db.add(Portfolio(name="P"))
    db.commit()

    updater._sync_easyequities_if_due(db)

    sync.assert_not_called()


def test_ee_sync_skipped_when_fresh(db, monkeypatch):
    sync = _stub_ee(monkeypatch)
    db.add(Portfolio(name="P", last_easyequities_sync=_utcnow()))
    db.commit()

    updater._sync_easyequities_if_due(db)

    sync.assert_not_called()


def test_ee_sync_runs_when_stale_and_stamps(db, monkeypatch):
    sync = _stub_ee(monkeypatch, synced=3)
    portfolio = Portfolio(name="P", last_easyequities_sync=_utcnow() - timedelta(days=3))
    db.add(portfolio)
    db.commit()

    updater._sync_easyequities_if_due(db)

    sync.assert_called_once()
    db.refresh(portfolio)
    assert _utcnow() - portfolio.last_easyequities_sync < timedelta(minutes=5)


def test_ee_sync_failure_leaves_timestamp(db, monkeypatch):
    sync = _stub_ee(monkeypatch, synced=None)
    portfolio = Portfolio(name="P")
    db.add(portfolio)
    db.commit()

    updater._sync_easyequities_if_due(db)

    sync.assert_called_once()
    db.refresh(portfolio)
    assert portfolio.last_easyequities_sync is None


# --- cross-process job lock ------------------------------------------------

from app.services import process_lock  # noqa: E402


def test_os_lock_excludes_second_holder(tmp_path):
    path = tmp_path / "updater.lock"
    fh = process_lock.acquire_instance_lock(path)
    try:
        assert fh is not None
        assert process_lock.acquire_instance_lock(path) is None
    finally:
        process_lock.release_instance_lock(fh)
    # Released: a new holder can take it.
    fh2 = process_lock.acquire_instance_lock(path)
    assert fh2 is not None
    process_lock.release_instance_lock(fh2)


def test_job_lock_is_reentrant_and_released_per_job(tmp_path):
    path = tmp_path / "job.lock"
    mine = process_lock.InterProcessJobLock(path, poll_seconds=0.01)
    other = process_lock.InterProcessJobLock(path, poll_seconds=0.01)  # "another process"

    assert mine.acquire()
    assert mine.acquire()            # nested job in the same process
    assert not other.acquire(timeout=0.05)
    mine.release()
    assert not other.acquire(timeout=0)  # still held by the outer job
    mine.release()
    # Released after the job, not held for the life of the process.
    assert other.acquire(timeout=0)
    other.release()


def test_job_skips_when_another_process_holds_lock(isolated_job_lock, session_factory, db, monkeypatch):
    other = process_lock.InterProcessJobLock(isolated_job_lock.path)
    assert other.acquire()
    try:
        data_service = MagicMock()
        monkeypatch.setattr(updater, "DataService", lambda _db: data_service)
        svc = UpdaterService()
        svc.process_lock_timeout = 0.1

        assert svc._update_prices_and_news() is False
        data_service.update_stock_prices.assert_not_called()
        assert db.query(UpdaterRun).count() == 0
    finally:
        other.release()


def test_job_releases_lock_afterwards(isolated_job_lock, session_factory, monkeypatch):
    data_service = MagicMock()
    data_service.update_stock_prices.return_value = 0
    monkeypatch.setattr(updater, "DataService", lambda _db: data_service)
    monkeypatch.setattr(updater, "NewsScraper", MagicMock())
    monkeypatch.setattr(updater, "EasyEquitiesCollector",
                        lambda: SimpleNamespace(enabled=False))

    UpdaterService()._update_prices_and_news()

    assert not isolated_job_lock.held
    other = process_lock.InterProcessJobLock(isolated_job_lock.path)
    assert other.acquire(timeout=0)
    other.release()


# --- price + news job ----------------------------------------------------

def test_price_job_with_zero_updates_fails_and_records_heartbeat(session_factory, db, monkeypatch):
    data_service = MagicMock()
    data_service.update_stock_prices.return_value = 0
    monkeypatch.setattr(updater, "DataService", lambda _db: data_service)
    monkeypatch.setattr(updater, "NewsScraper", MagicMock())
    # _snapshot_portfolio's broker syncs must not hit the network in tests.
    monkeypatch.setattr(updater, "EasyEquitiesCollector",
                        lambda: SimpleNamespace(enabled=False))

    assert UpdaterService()._update_prices_and_news() is False

    run = db.query(UpdaterRun).filter_by(job_name="price_news_update").one()
    assert run.success is False
    assert run.detail == "0 symbols updated"
