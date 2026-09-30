r"""
One-off migration from the old SQLite database to the configured PostgreSQL database.

Run from the repo root (e.g. PowerShell as Administrator is not required):
    python scripts\migrate_sqlite_to_postgres.py

This truncates the Postgres tables and copies all rows from data/stock_analyzer.db,
preserving primary keys and relationships. It then resets the id sequences so new
inserts continue from the highest migrated id.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.config.settings import settings
from app.models.base import Base
from app.models.stock import Stock, StockPrice
from app.models.watchlist import WatchlistItem
from app.models.portfolio import Portfolio, PortfolioHolding
from app.models.dividend import Dividend
from app.models.news import NewsArticle
from app.models.notification import Notification


SQLITE_URL = f"sqlite:///{os.path.join(ROOT, 'data', 'stock_analyzer.db')}"
POSTGRES_URL = settings.database_url

if "sqlite" in POSTGRES_URL:
    print("DATABASE_URL is still SQLite. Please set it to PostgreSQL first.")
    sys.exit(1)

if not os.path.exists(os.path.join(ROOT, 'data', 'stock_analyzer.db')):
    print("SQLite file not found: data/stock_analyzer.db")
    sys.exit(1)

source_engine = create_engine(SQLITE_URL)
target_engine = create_engine(POSTGRES_URL)

# Order matters: parents before children.
MODELS = [
    Stock,
    Portfolio,
    WatchlistItem,
    PortfolioHolding,
    StockPrice,
    Dividend,
    NewsArticle,
    Notification,
]


def _reset_sequences() -> None:
    """Reset Postgres serial sequences to the max existing id for each table."""
    with Session(target_engine) as session:
        for model in MODELS:
            table = model.__tablename__
            seq = f"{table}_id_seq"
            try:
                session.execute(text(f"SELECT setval(:seq, (SELECT MAX(id) FROM {table}), true)"), {"seq": seq})
            except Exception as exc:
                print(f"  Could not reset {seq}: {exc}")
        session.commit()


def main() -> None:
    print(f"Source: {SQLITE_URL}")
    print(f"Target: {POSTGRES_URL}")

    Base.metadata.create_all(target_engine)

    # Truncate target tables in reverse order so foreign keys stay valid.
    with Session(target_engine) as session:
        for model in reversed(MODELS):
            session.execute(text(f"TRUNCATE TABLE {model.__tablename__} RESTART IDENTITY CASCADE"))
        session.commit()
    print("Postgres tables truncated.")

    total = 0
    for model in MODELS:
        table = model.__tablename__
        with Session(source_engine) as src:
            rows = src.query(model).all()

        if not rows:
            print(f"{table}: 0 rows")
            continue

        with Session(target_engine) as dst:
            for row in rows:
                data = {c.name: getattr(row, c.name) for c in row.__table__.columns}
                dst.add(model(**data))
            dst.commit()

        count = len(rows)
        total += count
        print(f"{table}: {count} rows migrated")

    _reset_sequences()
    print(f"Migration complete. {total} rows copied to PostgreSQL.")


if __name__ == "__main__":
    main()
