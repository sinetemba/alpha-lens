from datetime import datetime, timezone
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from app.config.settings import settings


def utcnow_naive() -> datetime:
    """Current UTC time as a naive datetime.

    The DateTime columns are naive; writing aware values works on Postgres
    (UTC session) but stores a '+00:00' suffix on SQLite, breaking ordering.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)

def _ensure_sqlite_dir(url: str) -> None:
    """Create the folder of a file-based SQLite URL. On a fresh checkout the
    default ./data/stock_analyzer.db has no data/ folder yet, and SQLite
    fails with "unable to open database file" (first seen in CI)."""
    from pathlib import Path
    from sqlalchemy.engine import make_url
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and parsed.database and parsed.database != ":memory:":
        Path(parsed.database).expanduser().parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_dir(settings.database_url)

# Create SQLAlchemy engine. For Postgres, pin the session timezone to UTC:
# the DateTime columns are naive, and a non-UTC session timezone silently
# converts aware values to local wall-clock on write.
engine = create_engine(
    settings.database_url,
    connect_args=(
        {"check_same_thread": False}
        if "sqlite" in settings.database_url
        else {"options": "-c timezone=UTC"}
    ),
    # Recycle stale pooled connections: after a Postgres restart or idle
    # timeout the first checkout otherwise fails with OperationalError.
    pool_pre_ping=True,
    echo=False,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, connection_record):
    """Enable WAL mode for SQLite so reads and writes don't block each other."""
    if "sqlite" in settings.database_url:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA busy_timeout=5000;")
        cursor.close()

# Create SessionLocal class
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create Base class for models
class Base(DeclarativeBase):
    pass


def get_db():
    """Dependency to get database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
