"""A fresh checkout must be able to create its default SQLite database."""
from app.models.base import _ensure_sqlite_dir


def test_missing_sqlite_folder_is_created(tmp_path):
    target = tmp_path / "data" / "nested" / "stock_analyzer.db"
    _ensure_sqlite_dir(f"sqlite:///{target}")
    assert target.parent.is_dir()


def test_non_file_urls_are_left_alone(tmp_path):
    _ensure_sqlite_dir("sqlite:///:memory:")
    _ensure_sqlite_dir("postgresql://u:p@localhost:5432/alphalens")   # no-op, no error
