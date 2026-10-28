"""Headless EasyEquities holdings sync (same path as the Portfolio page)."""
from datetime import datetime, timezone

from app.models.base import SessionLocal
from app.models.portfolio import Portfolio
from app.collectors.easyequities import EasyEquitiesCollector
from app.dashboard.portfolio import _sync_portfolio_from_easyequities

collector = EasyEquitiesCollector()
if not collector.enabled:
    print("EasyEquities credentials not configured (EASYEQUITIES_USERNAME/PASSWORD).")
    raise SystemExit(1)

db = SessionLocal()
try:
    portfolio = db.query(Portfolio).first()
    if not portfolio:
        print("No portfolio row found.")
        raise SystemExit(1)

    synced = _sync_portfolio_from_easyequities(db, portfolio.id, collector)
    if synced is None:
        print(f"Sync failed: {collector.last_error}")
        raise SystemExit(1)

    portfolio.last_easyequities_sync = datetime.now(timezone.utc)
    db.commit()
    print(f"Synced {synced} holding(s); last_easyequities_sync = {portfolio.last_easyequities_sync}")
finally:
    db.close()
