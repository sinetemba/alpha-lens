"""
AlphaLens headless updater service.

Runs independently of the Streamlit UI so prices, news, and historical data
stay fresh even when the dashboard is closed. Designed for Windows Task
Scheduler or a Windows Service wrapper.

Schedule defaults (all Africa/Johannesburg):
- 09:00, 13:00, 17:00 : stock prices + news
- 18:00 daily          : historical prices
- Sunday 02:00         : clean old news
"""

import os
import sys
import time
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import EVENT_JOB_ERROR
from loguru import logger
from sqlalchemy.orm import Session

# Make sure app imports resolve when the script is run from any cwd.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import settings
from app.database.init_db import init_database
from app.models.base import SessionLocal
from app.models.watchlist import WatchlistItem
from app.models.portfolio import PortfolioHolding
from app.services.data_service import DataService
from app.scrapers.news import NewsScraper
from app.collectors.twelve_data import TwelveDataCollector


# Track the last time a logon update ran so a user can log on/off repeatedly
# without hammering the APIs. Always store this in the local data dir so it
# works for both SQLite and PostgreSQL.
LAST_LOGON_FILE = ROOT / "data" / ".updater_last_logon"

# Portfolio account types the updater should ignore (kept in sync with
# app/dashboard/portfolio.py so the headless updater can avoid importing Streamlit).
EXCLUDED_ACCOUNT_TYPES = {"Demo ZAR"}


def _get_tracked_symbols(db: Session) -> list:
    """Symbols relevant to the user: watchlist + non-excluded portfolio holdings + JSE index."""
    watchlist_symbols = [
        item.symbol for item in db.query(WatchlistItem).filter(WatchlistItem.is_active == True).all()
    ]
    portfolio_symbols = [
        h.symbol for h in db.query(PortfolioHolding).filter(
            ~PortfolioHolding.account_type.in_(EXCLUDED_ACCOUNT_TYPES)
        ).all()
    ]
    tracked = {settings.jse_index_yf_symbol}
    tracked.update(watchlist_symbols)
    tracked.update(portfolio_symbols)
    if tracked:
        return sorted(tracked)
    # No user data yet; fall back to the configured default watchlist.
    return list(settings.default_watchlist_symbols)


def _estimate_api_calls(symbols: list, include_historical: bool = False) -> int:
    """Rough upper-bound of Twelve Data calls for the next run."""
    calls = len(symbols)  # quotes
    if include_historical:
        calls += len(symbols)
    return calls


def _has_quota(symbols: list, include_historical: bool = False) -> tuple[bool, str]:
    """Check whether Twelve Data likely has enough quota left for the planned calls."""
    with TwelveDataCollector._lock:
        TwelveDataCollector._reset_daily_count_if_new_day()
        remaining = max(0, TwelveDataCollector._daily_limit - TwelveDataCollector._daily_count)
    needed = _estimate_api_calls(symbols, include_historical)
    if needed > remaining:
        return False, (
            f"Twelve Data quota too low for planned run: need ~{needed}, "
            f"have {remaining} left. Will fall back to Yahoo Finance where supported."
        )
    return True, f"Quota OK: need ~{needed}, have {remaining} left"


def _should_run_logon_update(min_seconds: int = 600) -> bool:
    """Avoid rapid logon-triggered updates."""
    if not LAST_LOGON_FILE.exists():
        return True
    try:
        last = float(LAST_LOGON_FILE.read_text(encoding="utf-8").strip() or 0)
        return (time.time() - last) > min_seconds
    except (ValueError, OSError):
        return True


def _record_logon_update() -> None:
    try:
        LAST_LOGON_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_LOGON_FILE.write_text(str(time.time()), encoding="utf-8")
    except OSError as e:
        logger.warning(f"Could not write last-logon timestamp: {e}")


class UpdaterService:
    """Headless background updater for AlphaLens."""

    def __init__(self):
        self.scheduler = BackgroundScheduler(timezone=settings.scheduler_timezone)
        self.scheduler.add_listener(self._on_job_error, EVENT_JOB_ERROR)

    def _on_job_error(self, event):
        logger.error(f"Scheduler job {event.job_id} failed: {event.exception}")

    def _update_prices_and_news(self):
        """3x-daily job: update tracked stock prices and collect news."""
        logger.info("Running scheduled price + news update")
        db = SessionLocal()
        try:
            symbols = _get_tracked_symbols(db)
            ok, msg = _has_quota(symbols, include_historical=False)
            logger.info(msg)

            data_service = DataService(db)
            updated = data_service.update_stock_prices(symbols)
            logger.info(f"Updated {updated} tracked stock prices")

            news_scraper = NewsScraper(db)
            articles = news_scraper.scrape_all_sources()
            logger.info(f"Collected {len(articles)} news articles")
        except Exception as e:
            logger.error(f"Price + news update failed: {e}")
        finally:
            db.close()

    def _update_historical(self):
        """Daily job: update historical data and dividends for tracked symbols."""
        logger.info("Running daily historical update")
        db = SessionLocal()
        try:
            symbols = _get_tracked_symbols(db)
            ok, msg = _has_quota(symbols, include_historical=True)
            logger.info(msg)

            data_service = DataService(db)
            for symbol in symbols:
                data_service.update_historical_data(symbol, days=365)

            data_service.update_dividends(symbols)
            logger.info(f"Daily historical update completed for {len(symbols)} symbols")
        except Exception as e:
            logger.error(f"Daily historical update failed: {e}")
        finally:
            db.close()

    def _weekly_cleanup(self):
        """Weekly job: delete old news articles."""
        logger.info("Running weekly cleanup")
        db = SessionLocal()
        try:
            news_scraper = NewsScraper(db)
            deleted = news_scraper.clean_old_articles(days=90)
            logger.info(f"Cleaned up {deleted} old news articles")
        except Exception as e:
            logger.error(f"Weekly cleanup failed: {e}")
        finally:
            db.close()

    def configure(self):
        """Register the default 3x/day + logon schedule."""
        self.scheduler.add_job(
            self._update_prices_and_news,
            trigger=CronTrigger(hour="9,13,17", minute=0),
            id="price_news_update",
            name="Price and News Update (3x daily)",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._update_historical,
            trigger=CronTrigger(hour=18, minute=0),
            id="daily_historical",
            name="Daily Historical + Dividend Update",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._weekly_cleanup,
            trigger=CronTrigger(day_of_week="sun", hour=2, minute=0),
            id="weekly_cleanup",
            name="Weekly Old News Cleanup",
            replace_existing=True,
        )

    def setup(self):
        """Configure logging, set the working directory and initialize the database."""
        os.chdir(ROOT)
        Path(settings.updater_log_file).parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            settings.updater_log_file,
            rotation="10 MB",
            retention="30 days",
            level=settings.log_level,
        )

        try:
            init_database()
            logger.info("Database initialized")
        except Exception as e:
            logger.error(f"Database init failed: {e}")
            raise

    def run_forever(self, stop_event=None):
        """Start the scheduler and keep running until a stop event is set."""
        self.configure()
        self.scheduler.start()
        logger.info("AlphaLens updater service started")

        try:
            while True:
                if stop_event and stop_event.wait(60):
                    logger.info("Stop event received")
                    break
                elif stop_event is None:
                    time.sleep(60)
        except KeyboardInterrupt:
            logger.info("Shutting down updater service (keyboard interrupt)")
        finally:
            self.stop()

    def start(self, mode: str = "daemon", stop_event=None):
        """Initialize the database and either start the scheduler or run one pass.

        Modes:
          - daemon: run the APScheduler continuously.
          - price_news: run the 3x-daily price + news job and exit.
          - full: run the 3x-daily job plus historical data and dividends, then exit.
          - logon: run the 3x-daily job only if it has not run recently, then exit.
        """
        self.setup()

        if mode == "logon":
            if _should_run_logon_update(min_seconds=settings.updater_logon_min_interval or 600):
                logger.info("Running logon-triggered update")
                self._update_prices_and_news()
                _record_logon_update()
            else:
                logger.info("Skipping logon update: ran too recently")
            return

        if mode == "price_news":
            logger.info("Running price + news update")
            self._update_prices_and_news()
            return

        if mode == "full":
            logger.info("Running full update")
            self._update_prices_and_news()
            self._update_historical()
            return

        self.run_forever(stop_event=stop_event)

    def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=True)
            logger.info("Updater service stopped")
        else:
            logger.info("Updater scheduler already stopped")


def main():
    parser = argparse.ArgumentParser(description="AlphaLens headless updater")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--price-news", action="store_true", help="Run the price + news job and exit")
    group.add_argument("--full", action="store_true", help="Run the full update (prices, news, history, dividends) and exit")
    group.add_argument("--logon", action="store_true", help="Run a lightweight logon update and exit")
    args = parser.parse_args()

    mode = "daemon"
    if args.price_news:
        mode = "price_news"
    elif args.full:
        mode = "full"
    elif args.logon:
        mode = "logon"

    service = UpdaterService()
    service.start(mode=mode)


if __name__ == "__main__":
    main()
