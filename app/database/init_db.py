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


def init_database():
    """Initialize the database with tables and seed data."""
    print("Initializing database...")
    create_tables()
    
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
