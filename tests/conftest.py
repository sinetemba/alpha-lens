import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.services import process_lock

# Optional: run the db_session tests on PostgreSQL too (CI does). SQLite
# doesn't enforce VARCHAR lengths - "YF_GASOLINE" (11 chars) into a
# String(10) passed every SQLite test and crashed the live Postgres page.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(autouse=True)
def isolated_job_lock(tmp_path, monkeypatch):
    """Point the cross-process job lock at a temp file.

    The real data/.updater.lock is held by the live daemon whenever it is
    mid-job; tests must never wait on (or take) it.
    """
    lock = process_lock.InterProcessJobLock(tmp_path / "job.lock", poll_seconds=0.05)
    monkeypatch.setattr(process_lock, "JOB_LOCK", lock)
    return lock


@pytest.fixture(scope="session")
def _throwaway_global_db(tmp_path_factory):
    """A per-run SQLite database standing in for the app's DATABASE_URL."""
    import importlib
    import pkgutil

    import app

    # Import every app module once so the per-test patch below reaches
    # modules that are otherwise imported lazily mid-test (dashboard pages).
    for info in pkgutil.walk_packages(app.__path__, "app."):
        try:
            importlib.import_module(info.name)
        except Exception:
            pass
    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('global_db') / 'app.db'}")
    Base.metadata.create_all(engine)
    yield engine, sessionmaker(autocommit=False, autoflush=False, bind=engine)
    engine.dispose()


def patch_global_sessions(monkeypatch, engine, factory) -> None:
    """Point every app module's SessionLocal / engine (imported from
    app.models.base) at `engine` / `factory`."""
    import sys

    from app.models import base
    real_engine, real_factory = base.engine, base.SessionLocal
    for name, module in list(sys.modules.items()):
        if not name.startswith("app") or module is None:
            continue
        if getattr(module, "SessionLocal", None) is real_factory:
            monkeypatch.setattr(module, "SessionLocal", factory)
        if getattr(module, "engine", None) is real_engine:
            monkeypatch.setattr(module, "engine", engine)


@pytest.fixture(autouse=True)
def isolated_global_db(_throwaway_global_db, monkeypatch):
    """No test may reach the real database through app.models.base.

    Code that opens its own session (cached Home/Company helpers, source-
    health recording) used the global SessionLocal - bound to the live
    DATABASE_URL. Locally that was the real PostgreSQL: page smoke tests
    wrote 26 fake "Yahoo metals / GoldAPI failed" heartbeats into it (found
    and removed 2026-11-23). In CI (no .env) it was ./data/stock_analyzer.db,
    whose folder doesn't exist - "unable to open database file".
    """
    engine, factory = _throwaway_global_db
    patch_global_sessions(monkeypatch, engine, factory)
    return factory


def _guard_test_database(url: str) -> None:
    """Never point the table-truncating fixture at a real database."""
    from app.config.settings import settings
    name = make_url(url).database or ""
    if "test" not in name.lower():
        raise RuntimeError(f"TEST_DATABASE_URL database {name!r} must contain 'test' - refusing to truncate it")
    if make_url(url) == make_url(settings.database_url):
        raise RuntimeError("TEST_DATABASE_URL is the app's DATABASE_URL - refusing to truncate it")


@pytest.fixture(scope="session")
def _postgres_engine():
    _guard_test_database(TEST_DATABASE_URL)
    engine = create_engine(TEST_DATABASE_URL, connect_args={"options": "-c timezone=UTC"})
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture(scope="function")
def db_session(request):
    """A fresh database session per test: in-memory SQLite, or PostgreSQL
    (tables emptied after each test) when TEST_DATABASE_URL is set."""
    if TEST_DATABASE_URL:
        engine = request.getfixturevalue("_postgres_engine")
        session = sessionmaker(bind=engine)()
        try:
            yield session
        finally:
            session.rollback()
            session.close()
            tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
            with engine.begin() as conn:
                conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        return

    engine = create_engine("sqlite:///:memory:")
    TestingSessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()
