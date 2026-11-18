"""Tests for app/services/backup.py (SQLite path; pg_dump is not mocked)."""
import os
import sqlite3
import time
from datetime import datetime, timedelta

import pytest

from app.config.settings import settings
from app.services import backup


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    """A small SQLite database with backups directed into tmp_path."""
    db_file = tmp_path / "source.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE holdings (symbol TEXT, quantity REAL)")
    conn.execute("INSERT INTO holdings VALUES ('NPN', 3.5)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_file}")
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "backups"))
    monkeypatch.setattr(settings, "backup_keep", 3)
    monkeypatch.setattr(settings, "backup_interval_days", 7)
    return db_file


def test_sqlite_backup_copies_data(sqlite_db):
    path = backup.backup_database()

    assert path.suffix == ".db"
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT symbol, quantity FROM holdings").fetchall() == [("NPN", 3.5)]
    finally:
        conn.close()


def test_prune_keeps_only_newest(sqlite_db, tmp_path):
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    # Five pre-existing backups with distinct, increasing mtimes.
    base = time.time() - 10_000
    for i in range(5):
        old = backups_dir / f"alphalens_2026010{i}_000000.db"
        old.write_bytes(b"x")
        os.utime(old, (base + i, base + i))

    newest = backup.backup_database()

    remaining = backup.list_backups()
    assert len(remaining) == 3
    assert remaining[0] == newest
    # The two newest pre-existing files survive, the older three are gone.
    assert [p.name for p in remaining[1:]] == [
        "alphalens_20260104_000000.db",
        "alphalens_20260103_000000.db",
    ]


def test_backup_due(sqlite_db):
    assert backup.backup_due() is True  # no backups yet

    backup.backup_database()

    assert backup.backup_due() is False
    assert backup.backup_due(now=datetime.now() + timedelta(days=8)) is True


def test_unsupported_backend_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "database_url", "mysql://u:p@localhost/db")
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "backups"))

    with pytest.raises(RuntimeError):
        backup.backup_database()
    assert backup.list_backups() == []
