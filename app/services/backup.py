"""
Database backups.

Prices and news can always be re-fetched, but holdings, crypto/bullion
purchases and watchlist settings are typed in by hand and exist nowhere
else. The updater daemon calls `backup_if_due()` from its once-a-minute loop,
so a backup is taken whenever the newest one is older than
`backup_interval_days` - a fixed cron slot would be missed while the machine
sleeps. `scripts\\run_updater.py --backup` forces one.

- PostgreSQL: `pg_dump -Fc` (custom format). Restore with:
    pg_restore --clean --if-exists -h localhost -U <user> -d <db> <file>.dump
- SQLite: online copy via the sqlite3 backup API (safe while the app runs).
  Restore by stopping the app and copying the file over the database.

Only the newest `backup_keep` files are kept.
"""

import glob
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from loguru import logger
from sqlalchemy.engine import make_url

from app.config.settings import settings

BACKUP_PREFIX = "alphalens_"
BACKUP_PATTERNS = (f"{BACKUP_PREFIX}*.dump", f"{BACKUP_PREFIX}*.db")


def _backup_dir() -> Path:
    path = Path(settings.backup_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_backups() -> list[Path]:
    """Existing backup files, newest first."""
    directory = _backup_dir()
    files = [Path(p) for pattern in BACKUP_PATTERNS for p in glob.glob(str(directory / pattern))]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def latest_backup_time() -> Optional[datetime]:
    """Local mtime of the newest backup, or None if there are none."""
    backups = list_backups()
    if not backups:
        return None
    return datetime.fromtimestamp(backups[0].stat().st_mtime)


def _find_pg_dump() -> Optional[str]:
    if settings.pg_dump_path:
        return settings.pg_dump_path
    on_path = shutil.which("pg_dump")
    if on_path:
        return on_path
    # Windows installs don't put the PostgreSQL bin dir on PATH; prefer the
    # newest version so the client is never older than the server.
    candidates = glob.glob(r"C:\Program Files\PostgreSQL\*\bin\pg_dump.exe")

    def _version(path: str) -> float:
        try:
            return float(Path(path).parents[1].name)
        except ValueError:
            return 0.0

    return max(candidates, key=_version) if candidates else None


def _backup_postgres(url, target: Path) -> None:
    pg_dump = _find_pg_dump()
    if not pg_dump:
        raise RuntimeError("pg_dump not found - set PG_DUMP_PATH in .env")
    cmd = [pg_dump, "-Fc", "--no-owner", "-f", str(target)]
    if url.host:
        cmd += ["-h", url.host]
    if url.port:
        cmd += ["-p", str(url.port)]
    if url.username:
        cmd += ["-U", url.username]
    cmd.append(url.database)
    env = os.environ.copy()
    if url.password:
        # Via the environment, never the command line (visible in process lists).
        env["PGPASSWORD"] = url.password
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump exited {result.returncode}: {result.stderr.strip()[:300]}")


def _backup_sqlite(url, target: Path) -> None:
    source = sqlite3.connect(url.database)
    try:
        dest = sqlite3.connect(str(target))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def _prune(keep: int) -> int:
    removed = 0
    for old in list_backups()[max(keep, 1):]:
        try:
            old.unlink()
            removed += 1
        except OSError as e:
            logger.warning(f"Could not remove old backup {old}: {e}")
    return removed


def backup_database() -> Path:
    """Take a backup now and prune old ones. Returns the new file's path.

    Raises on failure; a partial file is removed so it never counts as the
    newest backup.
    """
    url = make_url(settings.database_url)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if url.get_backend_name() == "postgresql":
        target = _backup_dir() / f"{BACKUP_PREFIX}{stamp}.dump"
        backup_fn = _backup_postgres
    elif url.get_backend_name() == "sqlite":
        target = _backup_dir() / f"{BACKUP_PREFIX}{stamp}.db"
        backup_fn = _backup_sqlite
    else:
        raise RuntimeError(f"Backups not supported for {url.get_backend_name()}")

    try:
        backup_fn(url, target)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    if not target.exists() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise RuntimeError("Backup produced an empty file")

    removed = _prune(settings.backup_keep)
    logger.info(
        f"Database backup written: {target} ({target.stat().st_size / 1024:.0f} KB)"
        + (f", pruned {removed} old backup(s)" if removed else "")
    )
    return target


def backup_due(now: Optional[datetime] = None) -> bool:
    """Whether the newest backup is older than the configured interval."""
    latest = latest_backup_time()
    if latest is None:
        return True
    now = now or datetime.now()
    return now - latest >= timedelta(days=settings.backup_interval_days)
