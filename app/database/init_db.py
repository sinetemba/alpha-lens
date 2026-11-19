from app.models.base import Base, engine, SessionLocal
from app.models import Stock, StockPrice, NewsArticle, WatchlistItem, Portfolio, PortfolioHolding, PortfolioSnapshot, Dividend, Notification, UpdaterRun, KrugerrandHolding, CryptoHolding, CryptoBundle, SchemaMeta
from app.config.settings import settings
import os


def create_tables():
    """Create all database tables."""
    # Ensure data directory exists
    db_path = settings.database_path
    if db_path.startswith("./") or db_path.startswith(".\\"):
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
    
    Base.metadata.create_all(bind=engine)
    print("Database tables created successfully.")


def drop_tables():
    """Drop all database tables."""
    Base.metadata.drop_all(bind=engine)
    print("Database tables dropped successfully.")


def _migrate_schema():
    """Apply lightweight schema migrations for existing databases.

    SQLAlchemy ``create_all`` only creates *new* tables; it will not add
    columns to tables that already exist. Rather than a hand-maintained
    ALTER list (which drifted - several model columns were never migrated,
    breaking old databases with OperationalError: no such column), this
    diffs every mapped table against the live schema and ADDs the missing
    columns generically.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    compiler = engine.dialect.ddl_compiler(engine.dialect, None)

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        existing_columns = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing_columns:
                continue
            spec = compiler.get_column_specification(column)
            # A NOT NULL column with no server default can't be added to a
            # non-empty table - leave it NULL-capable (same outcome the old
            # hand-written migrations produced).
            if column.server_default is None:
                spec = spec.replace(" NOT NULL", "")
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {spec}"))
            print(f"Migration: added '{column.name}' column to {table.name}.")

    # Data backfills the column diff can't express: fetched_at predates the
    # generic migrator and rows written before it exist have NULL.
    if "stock_prices" in existing_tables:
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE stock_prices SET fetched_at = COALESCE(created_at, timestamp) "
                "WHERE fetched_at IS NULL"
            ))

    _enforce_unique_indexes(inspector)
    _ensure_plain_indexes(inspector)


def _enforce_unique_indexes(inspector):
    """Deduplicate rows and enforce unique indexes that protect against
    concurrent writers (dashboard scheduler + headless updater).

    ``create_all`` never alters existing tables, so indexes added to the
    models must be created here for databases that already exist.
    """
    from sqlalchemy import text

    table_names = inspector.get_table_names()

    # (table, index name, columns, soft-delete flag column or None).
    # Soft targets dedupe by deactivating older rows instead of deleting
    # them, and produce a partial unique index - used where inactive rows
    # are kept for history (watchlist soft-delete).
    targets = [
        ("stock_prices", "ix_stock_prices_symbol_timestamp", "symbol, timestamp", None),
        ("dividends", "ix_dividends_symbol_ex_date", "symbol, ex_dividend_date, amount", None),
        ("portfolio_snapshots", "uq_portfolio_snapshots_portfolio_date", "portfolio_id, snapshot_date", None),
        ("watchlist_items", "ix_watchlist_items_active_symbol", "symbol", "is_active"),
        # Luno sync upserts from two concurrent writers (updater + page); a
        # duplicate row is invisible to its dedup map and double-counted.
        ("crypto_holdings", "ix_crypto_holdings_source_coin_bundle",
         "coin_id, source, COALESCE(bundle, '')", None),
        # EE sync + manual adds are check-then-insert and run from page
        # renders AND the headless sync script.
        ("portfolio_holdings", "uq_portfolio_holdings_portfolio_symbol_account",
         "portfolio_id, symbol, account_type", None),
        # Exactly one Portfolio row ever: the constant key (id*0 == 0 for
        # every row) makes a second insert collide. The dedupe keeps the
        # most complete row should two ever co-exist.
        ("portfolios", "uq_portfolios_singleton", "(id * 0)", None),
    ]

    for table, index_name, columns, soft_flag in targets:
        if table not in table_names:
            continue
        indexes = {ix["name"]: ix for ix in inspector.get_indexes(table)}
        existing = indexes.get(index_name)
        # Skip when ANY unique index (including a named UNIQUE constraint,
        # which inspectors may report under a generated name) already covers
        # exactly these columns.
        # Split on top-level commas only - expressions like
        # COALESCE(bundle, '') contain their own commas.
        wanted, depth, current = [], 0, ""
        for ch in columns:
            if ch == "," and depth == 0:
                wanted.append(current.strip())
                current = ""
                continue
            depth += (ch == "(") - (ch == ")")
            current += ch
        wanted.append(current.strip())
        if "(" in columns:
            # Expression index (e.g. COALESCE for nullable keys) - its
            # column_names won't match a plain list, so gate on the name.
            if existing and existing.get("unique"):
                continue
        elif any(
            ix.get("unique") and (ix.get("column_names") or []) == wanted
            for ix in indexes.values()
        ):
            continue

        with engine.begin() as conn:
            if soft_flag:
                # Deactivate all but the newest active row per key; the
                # partial index then enforces one active row per symbol.
                conn.execute(text(
                    f"UPDATE {table} SET {soft_flag} = FALSE WHERE {soft_flag} AND id NOT IN ("
                    f"SELECT MAX(id) FROM {table} WHERE {soft_flag} GROUP BY {columns})"
                ))
            else:
                # Keep the most COMPLETE row per key group (most non-null
                # fields, newest id on ties) - MAX(id) alone could keep an
                # adjusted_close=0 placeholder over a full bar. Rows with
                # NULL key columns are left alone: they can't collide under
                # a unique index (NULLs compare distinct).
                all_columns = [c["name"] for c in inspector.get_columns(table)]
                score = " + ".join(
                    f"CASE WHEN {c} IS NOT NULL THEN 1 ELSE 0 END"
                    for c in all_columns if c != "id"
                )
                # Only plain columns get a NULL check - expressions like
                # COALESCE(bundle, '') are never NULL and can't be split at
                # their inner comma.
                non_null_keys = " AND ".join(
                    f"{c} IS NOT NULL" for c in wanted if "(" not in c
                ) or "TRUE"
                conn.execute(text(
                    f"DELETE FROM {table} WHERE {non_null_keys} AND id NOT IN ("
                    f"SELECT id FROM ("
                    f"  SELECT id, ROW_NUMBER() OVER ("
                    f"    PARTITION BY {columns} ORDER BY ({score}) DESC, id DESC"
                    f"  ) AS rn FROM {table} WHERE {non_null_keys}"
                    f") ranked WHERE rn = 1)"
                ))
            if existing:
                conn.execute(text(f"DROP INDEX {index_name}"))
            conn.execute(text(
                f"CREATE UNIQUE INDEX {index_name} ON {table} ({columns})"
                + (f" WHERE {soft_flag}" if soft_flag else "")
            ))
        print(f"Migration: deduplicated {table} and made ({columns}) unique.")


def _ensure_plain_indexes(inspector):
    """Create non-unique indexes added to models after the table already exists."""
    from sqlalchemy import text

    table_names = inspector.get_table_names()

    targets = [
        ("stock_prices", "ix_stock_prices_fetched_at", "fetched_at"),
        ("portfolio_holdings", "ix_portfolio_holdings_portfolio_id", "portfolio_id"),
        ("portfolio_holdings", "ix_portfolio_holdings_stock_id", "stock_id"),
        ("watchlist_items", "ix_watchlist_items_is_active", "is_active"),
        ("watchlist_items", "ix_watchlist_items_stock_id", "stock_id"),
        ("dividends", "ix_dividends_stock_id", "stock_id"),
        ("news_articles", "ix_news_articles_created_at", "created_at"),
    ]

    for table, index_name, columns in targets:
        if table not in table_names:
            continue
        if index_name in {ix["name"] for ix in inspector.get_indexes(table)}:
            continue
        with engine.begin() as conn:
            conn.execute(text(
                f"CREATE INDEX {index_name} ON {table} ({columns})"
            ))
        print(f"Migration: added index {index_name} on {table}({columns}).")


_NORMALIZE_BARS_KEY = "normalize_bar_timestamps_v1"


def _normalize_bar_timestamps():
    """Collapse non-midnight stock_prices bars to exchange-local midnight.

    Early quote saves stored bars at their fetch time (e.g. 14:32) instead of
    the trading-day midnight used by every other write path. Normalise them so
    the (symbol, timestamp) dedup keeps working; drop any that would collide
    with a real midnight bar for the same day.

    This is a one-time repair: the schema_meta marker short-circuits later
    startups (dashboard AND daemon each call init_database) with a primary-key
    lookup, instead of loading every stock_prices row on every launch.
    """
    from datetime import time as dt_time
    from sqlalchemy import func

    db = SessionLocal()
    try:
        if db.get(SchemaMeta, _NORMALIZE_BARS_KEY) is not None:
            return

        # Push the non-midnight filter into SQL (dialect-aware) so the table
        # is not loaded into Python just to find rows needing repair.
        if db.bind.dialect.name == "sqlite":
            non_midnight = func.strftime('%H:%M:%S', StockPrice.timestamp) != '00:00:00'
        else:
            non_midnight = StockPrice.timestamp > func.date_trunc('day', StockPrice.timestamp)

        intraday = db.query(StockPrice).filter(non_midnight).all()
        if not intraday:
            db.add(SchemaMeta(key=_NORMALIZE_BARS_KEY))
            db.commit()
            return

        symbols = {row.symbol for row in intraday}
        midnight_keys = {
            (symbol, ts)
            for symbol, ts in db.query(StockPrice.symbol, StockPrice.timestamp).filter(
                StockPrice.symbol.in_(symbols)
            ).all()
            if ts.time() == dt_time(0, 0)
        }

        deleted = updated = 0
        for row in intraday:
            midnight = row.timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
            if (row.symbol, midnight) in midnight_keys:
                db.delete(row)
                deleted += 1
            else:
                row.timestamp = midnight
                midnight_keys.add((row.symbol, midnight))
                updated += 1
        db.add(SchemaMeta(key=_NORMALIZE_BARS_KEY))
        db.commit()
        print(f"Migration: normalised {updated} intraday bar(s) to midnight, removed {deleted} duplicate(s).")
    except Exception as e:
        db.rollback()
        print(f"Error normalising bar timestamps: {e}")
    finally:
        db.close()


def _backfill_stock_names():
    """Apply known display-name overrides to existing Stock rows.

    External APIs sometimes fail to resolve JSE symbols (e.g. PHP), leaving
    the database with symbol-only names. This backfills any known overrides
    so all pages show "SYMBOL - Company Name" consistently.
    """
    from app.services.data_service import COMPANY_NAME_OVERRIDES

    db = SessionLocal()
    try:
        updated = 0
        for symbol, name in COMPANY_NAME_OVERRIDES.items():
            stock = db.query(Stock).filter(Stock.symbol == symbol).first()
            if stock:
                stock.name = name
                updated += 1
        if updated:
            db.commit()
            print(f"Backfilled {updated} stock name(s).")
    except Exception as e:
        db.rollback()
        print(f"Error backfilling stock names: {e}")
    finally:
        db.close()


def _cleanup_excluded_holdings():
    """Remove previously-synced holdings from excluded EasyEquities accounts (e.g. Demo ZAR)."""
    db = SessionLocal()
    try:
        deleted = db.query(PortfolioHolding).filter(
            PortfolioHolding.account_type == "Demo ZAR"
        ).delete()
        if deleted:
            db.commit()
            print(f"Cleanup: removed {deleted} 'Demo ZAR' holding(s) from the database.")
    except Exception as e:
        db.rollback()
        print(f"Error cleaning up excluded holdings: {e}")
    finally:
        db.close()


_OPENING_BUYS_KEY = "backfill_opening_buys_v1"


def _backfill_opening_buys():
    """One-time: log an opening buy for holdings that predate the transaction
    log, so later sells have a matching buy. Additive only (inserts rows)."""
    from app.services.transactions import backfill_opening_buys

    db = SessionLocal()
    try:
        if db.get(SchemaMeta, _OPENING_BUYS_KEY) is not None:
            return
        added = backfill_opening_buys(db)
        db.add(SchemaMeta(key=_OPENING_BUYS_KEY))
        db.commit()
        if added:
            print(f"Migration: logged opening buys for {added} pre-existing holding(s).")
    except Exception as e:
        db.rollback()
        print(f"Error backfilling opening buys: {e}")
    finally:
        db.close()


def init_database():
    """Initialize the database with tables and seed data."""
    print("Initializing database...")
    create_tables()
    _migrate_schema()
    _normalize_bar_timestamps()
    _cleanup_excluded_holdings()
    _backfill_stock_names()
    _backfill_opening_buys()
    
    # Add seed data if needed
    # For example, add default JSE stocks
    db = SessionLocal()
    
    try:
        # Check if stocks already exist
        existing_stocks = db.query(Stock).count()
        if existing_stocks == 0:
            # Add popular JSE stocks
            default_stocks = [
                {"symbol": "NPN", "name": "Naspers Limited", "sector": "Technology", "industry": "Internet"},
                {"symbol": "CFR", "name": "Clicks Group Limited", "sector": "Consumer", "industry": "Retail"},
                {"symbol": "AGL", "name": "Anglo American Platinum", "sector": "Mining", "industry": "Metals"},
                {"symbol": "SOL", "name": "Sasol Limited", "sector": "Energy", "industry": "Chemicals"},
                {"symbol": "SBK", "name": "Standard Bank Group", "sector": "Financial", "industry": "Banking"},
                {"symbol": "MTN", "name": "MTN Group Limited", "sector": "Telecommunications", "industry": "Mobile"},
                {"symbol": "VOD", "name": "Vodacom Group", "sector": "Telecommunications", "industry": "Mobile"},
                {"symbol": "ABG", "name": "AB InBev (Anheuser-Busch)", "sector": "Consumer", "industry": "Beverages"},
                {"symbol": "BID", "name": "Bid Corporation", "sector": "Consumer", "industry": "Food Service"},
                {"symbol": "RCL", "name": "RCL Foods", "sector": "Consumer", "industry": "Food"},
            ]
            
            for stock_data in default_stocks:
                stock = Stock(**stock_data)
                db.add(stock)
            
            db.commit()
            print(f"Added {len(default_stocks)} default JSE stocks to database.")
        else:
            print(f"Database already contains {existing_stocks} stocks.")
    
    except Exception as e:
        db.rollback()
        print(f"Error adding seed data: {e}")
    finally:
        db.close()
    
    print("Database initialization complete.")


if __name__ == "__main__":
    init_database()
