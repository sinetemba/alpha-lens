r"""One-time fix for timestamps stored in the wrong timezone on PostgreSQL.

Background: the database session timezone used to be Africa/Johannesburg, so
tz-aware values written into naive ``timestamp`` columns were stored as SAST
wall-clock (e.g. 11:41 for a 09:41 UTC fetch). The dashboard then assumed those
naive values were UTC and displayed them 2 hours ahead.

This script:
  1. Sets the database timezone to UTC so future sessions stop converting
     (the app also passes ``-c timezone=UTC`` in connect_args).
  2. Shifts the event-time columns that were stored as SAST wall-clock back to
     UTC wall-clock.

Run ONCE, from the repo root:
    python scripts\fix_postgres_timezone.py            # dry-run (shows counts)
    python scripts\fix_postgres_timezone.py --apply    # applies the fix

Re-running with --apply will shift the data again, so only run it once.
Columns keyed by trading date (stock_prices.timestamp at midnight, dividend
dates) are intentionally untouched - they already store naive local-midnight.

Only values WRITTEN via Postgres (after the SQLite migration, ~Sep 15 2026)
were stored shifted; SQLite-era values were already correct UTC wall-clock.
Write-time columns self-identify their era (stored value >= cutoff means
Postgres); insert-time columns (published_at, purchase_date) use created_at.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.config.settings import settings

if "sqlite" in settings.database_url:
    print("DATABASE_URL is SQLite; this fix only applies to PostgreSQL.")
    sys.exit(0)

# The wall-clock zone the bad values were stored in (the old session timezone).
STORED_AS = "Africa/Johannesburg"

# Values stored below this instant were written via SQLite (already UTC).
CUTOFF = "2026-09-15 22:00:00"

# Event-time columns written with tz-aware UTC datetimes (and therefore stored
# as SAST wall-clock). timestamp/date-keyed columns are excluded on purpose.
# ORDER MATTERS: published_at uses created_at as its era discriminator, so it
# must be shifted before created_at changes.
EVENT_TIME_COLUMNS = [
    ("news_articles", "published_at"),
    ("stocks", "created_at"),
    ("stocks", "updated_at"),
    ("stock_prices", "created_at"),
    ("stock_prices", "fetched_at"),
    ("news_articles", "created_at"),
    ("watchlist_items", "added_at"),
    ("watchlist_items", "updated_at"),
    ("portfolios", "created_at"),
    ("portfolios", "updated_at"),
    ("portfolios", "last_easyequities_sync"),
    ("portfolio_holdings", "created_at"),
    ("portfolio_holdings", "updated_at"),
    ("dividends", "created_at"),
    ("moneyweb_dividend_watch", "fetched_at"),
    ("moneyweb_dividend_watch", "created_at"),
    ("notifications", "sent_at"),
    ("notifications", "read_at"),
    ("notifications", "created_at"),
]

CONVERT = f"{{col}} AT TIME ZONE '{STORED_AS}' AT TIME ZONE 'UTC'"


def main() -> None:
    apply = "--apply" in sys.argv
    url = make_url(settings.database_url)
    engine = create_engine(settings.database_url)

    # ALTER DATABASE cannot run inside a transaction - use autocommit.
    if apply:
        with engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as conn:
            conn.execute(
                text(f"ALTER DATABASE {url.database} SET timezone = 'UTC'")
            )
        print(f"ALTER DATABASE {url.database} SET timezone = 'UTC'")
    else:
        print(f"Would run: ALTER DATABASE {url.database} SET timezone = 'UTC'")

    with engine.connect() as conn:
        print("Session timezone:", conn.execute(text("SHOW timezone")).scalar())

    def shift(table: str, col: str, where: str, era_col: str = None) -> None:
        # One transaction per column so a missing table only skips itself.
        # era_col identifies Postgres-written rows (SQLite-era values were
        # already UTC). Defaults to the column itself (write-time fields).
        era = era_col or col
        where = f"({where}) AND {era} >= '{CUTOFF}'"
        with engine.begin() as conn:
            count = conn.execute(
                text(f"SELECT count(*) FROM {table} WHERE {where}")
            ).scalar()
            if not count:
                return
            if apply:
                conn.execute(
                    text(
                        f"UPDATE {table} SET {col} = {CONVERT.format(col=col)} "
                        f"WHERE {where}"
                    )
                )
            print(f"  {table}.{col}: {count} rows {'shifted to UTC' if apply else 'to shift'}")

    # purchase_date mixes user-entered dates (naive midnight, correct as-is)
    # with sync-time writes (SAST wall-clock) - only touch non-midnight rows.
    # Its era discriminator is created_at, so run before created_at shifts.
    try:
        shift(
            "portfolio_holdings",
            "purchase_date",
            "purchase_date IS NOT NULL AND purchase_date::time <> '00:00:00'",
            era_col="created_at",
        )
    except Exception:
        pass

    for table, col in EVENT_TIME_COLUMNS:
        try:
            # published_at/purchase_date are insert-time values: their era is
            # the row's created_at, not the value itself.
            era = "created_at" if col == "published_at" else None
            shift(table, col, f"{col} IS NOT NULL", era_col=era)
        except Exception:
            continue

    # Same for stock_prices.timestamp: daily bars are naive local-midnight
    # (correct as-is), but live spot snapshots (commodities/krugerrands) were
    # written aware and stored as SAST wall-clock - those have a time part.
    try:
        shift(
            "stock_prices",
            "timestamp",
            "timestamp::time <> '00:00:00'",
        )
    except Exception:
        pass

    print("Done." if apply else "Dry-run. Re-run with --apply to apply.")


if __name__ == "__main__":
    main()
