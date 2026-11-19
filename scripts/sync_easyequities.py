r"""Headless EasyEquities holdings sync (same path as the Portfolio page).

Run from the repo root:
    python scripts\sync_easyequities.py
"""
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.models.base import SessionLocal
from app.models.portfolio import Portfolio
from app.collectors.easyequities import EasyEquitiesCollector
from app.services.easyequities_sync import sync_portfolio_from_easyequities
from app.services import process_lock

collector = EasyEquitiesCollector()
if not collector.enabled:
    print("EasyEquities credentials not configured (EASYEQUITIES_USERNAME/PASSWORD).")
    raise SystemExit(1)

# Don't race the daemon's own update/sync - last-writer-wins otherwise.
if not process_lock.JOB_LOCK.acquire(timeout=60):
    print("Another updater process is fetching right now - try again shortly.")
    raise SystemExit(1)
try:
    db = SessionLocal()
    try:
        portfolio = db.query(Portfolio).first()
        if not portfolio:
            print("No portfolio row found.")
            raise SystemExit(1)

        synced, _raw = sync_portfolio_from_easyequities(db, portfolio.id, collector)
        if synced is None:
            print(f"Sync failed: {collector.last_error}")
            raise SystemExit(1)

        # Naive UTC - all DateTime columns are naive; an aware value would store
        # a "+00:00" suffix on SQLite and break staleness comparisons.
        portfolio.last_easyequities_sync = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
        print(f"Synced {synced} holding(s); last_easyequities_sync = {portfolio.last_easyequities_sync}")
    finally:
        db.close()
finally:
    process_lock.JOB_LOCK.release()
