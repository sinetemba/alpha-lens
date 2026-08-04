from app.models.base import Base, engine, SessionLocal
from app.models import Stock, StockPrice, NewsArticle, WatchlistItem, Portfolio, PortfolioHolding, Dividend, Notification
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
    columns to tables that already exist.  This function fills that gap by
    running simple ``ALTER TABLE`` statements when needed.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)

    # --- portfolio_holdings.account_type (added to track ZAR / TFSA / USD) ---
    if "portfolio_holdings" in inspector.get_table_names():
        columns = [col["name"] for col in inspector.get_columns("portfolio_holdings")]
        if "account_type" not in columns:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE portfolio_holdings ADD COLUMN account_type VARCHAR(50) DEFAULT 'ZAR'")
                )
            print("Migration: added 'account_type' column to portfolio_holdings.")

    # --- portfolios.last_easyequities_sync (added to persist sync staleness tracking) ---
    if "portfolios" in inspector.get_table_names():
        columns = [col["name"] for col in inspector.get_columns("portfolios")]
        if "last_easyequities_sync" not in columns:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE portfolios ADD COLUMN last_easyequities_sync DATETIME")
                )
            print("Migration: added 'last_easyequities_sync' column to portfolios.")


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


def init_database():
    """Initialize the database with tables and seed data."""
    print("Initializing database...")
    create_tables()
    _migrate_schema()
    _cleanup_excluded_holdings()
    _backfill_stock_names()
    
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
