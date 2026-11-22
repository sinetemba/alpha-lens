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
