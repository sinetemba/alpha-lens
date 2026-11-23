"""Prove a backup actually restores - not just that the archive is readable.

The updater verifies every backup with `pg_restore -l` (PostgreSQL) or
`PRAGMA integrity_check` (SQLite), which proves the file parses, not that it
restores into a working database. This restores one into a SEPARATE
database and compares every table's row count with the live database.

    python scripts\\test_restore.py --target-url postgresql://user:pass@localhost:5432/alphalens_restore_test
    python scripts\\test_restore.py --target-url ... --backup data\\backups\\alphalens_20261121_232410.dump

The target database's name must contain "test" and must not be DATABASE_URL
- it is overwritten (pg_restore --clean). Run it every few months, and
before relying on a backup for a repair. Exit code 0 = restore verified.
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.config.settings import settings  # noqa: E402
from app.models import Base  # noqa: E402
from app.services import backup  # noqa: E402

# Tables that must never come back empty from a real backup.
CORE_TABLES = ("stocks", "stock_prices", "portfolio_holdings")


def _guard(target_url: str) -> None:
    target = make_url(target_url)
    if "test" not in (target.database or "").lower():
        sys.exit(f"Refusing: target database {target.database!r} must contain 'test' (it gets overwritten).")
    if target == make_url(settings.database_url):
        sys.exit("Refusing: target is the live DATABASE_URL.")


def _restore_postgres(dump: Path, target_url: str) -> None:
    pg_restore = backup._find_pg_tool("pg_restore")
    if not pg_restore:
        sys.exit("pg_restore not found - install the PostgreSQL client tools or put them on PATH.")
    url = make_url(target_url)
    # --no-privileges: the dump GRANTs to the live role (e.g. "alphalens"),
    # which a fresh server or the CI database does not have.
    cmd = [pg_restore, "--clean", "--if-exists", "--no-owner", "--no-privileges", "--exit-on-error",
           "-h", url.host or "localhost", "-p", str(url.port or 5432), "-d", url.database]
    if url.username:
        cmd += ["-U", url.username]
    env = None
    if url.password:
        import os
        env = {**os.environ, "PGPASSWORD": url.password}
    result = subprocess.run(cmd + [str(dump)], capture_output=True, text=True, timeout=1800, env=env)
    if result.returncode != 0:
        sys.exit(f"pg_restore failed ({result.returncode}): {result.stderr.strip()[:800]}")


def _counts(url: str) -> dict:
    engine = create_engine(url)
    try:
        names = set(inspect(engine).get_table_names())
        with engine.connect() as conn:
            return {t.name: (conn.execute(text(f'SELECT COUNT(*) FROM "{t.name}"')).scalar()
                             if t.name in names else None)
                    for t in Base.metadata.sorted_tables}
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target-url", required=True, help="Scratch database to restore into (name must contain 'test').")
    parser.add_argument("--backup", help="Backup file (default: the newest in data/backups).")
    args = parser.parse_args()
    _guard(args.target_url)

    dump = Path(args.backup) if args.backup else (backup.list_backups() or [None])[0]
    if dump is None or not dump.exists():
        sys.exit("No backup file found.")
    live_url = settings.database_url
    if not live_url.startswith("postgresql") or dump.suffix != ".dump":
        sys.exit("Only PostgreSQL .dump backups are restore-tested here (SQLite backups are plain "
                 "copies - open one with sqlite3 to check it).")

    print(f"Restoring {dump.name} ({dump.stat().st_size / 1e6:.1f} MB) into {make_url(args.target_url).database} ...")
    _restore_postgres(dump, args.target_url)

    restored, live = _counts(args.target_url), _counts(live_url)
    problems = []
    print(f"\n{'table':28} {'restored':>10} {'live now':>10}")
    for name in sorted(restored):
        r, l = restored[name], live.get(name)
        flag = ""
        if r is None:
            flag = "  <-- MISSING from the backup"
            # Tables added after the backup was taken are expected to be missing.
            if l:
                problems.append(f"{name} missing")
        elif name in CORE_TABLES and r == 0:
            flag = "  <-- EMPTY"
            problems.append(f"{name} empty")
        elif l is not None and r > l * 1.5 + 100:
            flag = "  <-- far more rows than live?"
        print(f"{name:28} {str(r):>10} {str(l):>10}{flag}")

    if problems:
        print(f"\nRESTORE CHECK FAILED: {', '.join(problems)}")
        return 1
    print("\nRestore verified: every table restored; core tables non-empty. "
          "Row counts differ from live only by what changed since the backup.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
