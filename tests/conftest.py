import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base
from app.services import process_lock


@pytest.fixture(autouse=True)
def isolated_job_lock(tmp_path, monkeypatch):
    """Point the cross-process job lock at a temp file.

    The real data/.updater.lock is held by the live daemon whenever it is
    mid-job; tests must never wait on (or take) it.
    """
    lock = process_lock.InterProcessJobLock(tmp_path / "job.lock", poll_seconds=0.05)
    monkeypatch.setattr(process_lock, "JOB_LOCK", lock)
    return lock


@pytest.fixture(scope="function")
def db_session():
    """Provide a fresh in-memory SQLite session for each test."""
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
