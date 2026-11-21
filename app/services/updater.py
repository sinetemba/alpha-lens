"""
AlphaLens headless updater service.

Runs independently of the Streamlit UI so prices, news, and historical data
stay fresh even when the dashboard is closed. Designed for Windows Task
Scheduler or a Windows Service wrapper.

Schedule defaults (all Africa/Johannesburg):
- 09:00, 13:00, 17:00 : stock prices + news
- 18:00 daily          : historical prices + dividends + penny near-52w-low
                         alerts (made up from the heartbeat if missed while
                         the machine slept)
- Sunday 02:00         : clean old news
- every 7 days         : database backup (checked each minute, so a sleeping
                         machine catches up on wake - see services/backup.py)
"""

import functools
import os
import sys
import time
import logging
import threading
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import EVENT_JOB_ERROR
from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

# Make sure app imports resolve when the script is run from any cwd.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config.settings import settings
from app.database.init_db import init_database
from app.models.base import SessionLocal, utcnow_naive
from app.models.notification import Notification
from app.models.watchlist import WatchlistItem
from app.models.portfolio import (
    Portfolio,
    PortfolioHolding,
    EXCLUDED_ACCOUNT_TYPES as PORTFOLIO_EXCLUDED_ACCOUNT_TYPES,
    is_jse_tracked_account,
)
from app.models.stock import Stock, StockPrice
from app.models.updater_run import UpdaterRun
from app.services.data_service import DataService
from app.scrapers.news import NewsScraper
from app.collectors.twelve_data import TwelveDataCollector
from app.services import backup, process_lock
from app.services.portfolio_valuation import record_snapshot, revalue_portfolio, valued_holdings
from app.services.luno_sync import sync_luno_holdings
from app.services.net_worth import compute_net_worth
from app.collectors.luno import LunoCollector
from app.collectors.easyequities import EasyEquitiesCollector
from app.collectors.moneyweb import MoneywebCollector
from app.services.easyequities_sync import sync_portfolio_from_easyequities


def _run_notification_checks(db: Session) -> None:
    """Fire watchlist price/technical alerts after fresh prices are saved.

    Alert rows are always created (the dashboard Notifications page is an
    in-app channel); only external delivery is gated on
    notification_enabled. Failures here must never fail the update run.
    """
    try:
        from app.notifications.notification_service import NotificationService
        notifier = NotificationService(db)
        notifier.check_price_alerts()
        notifier.check_technical_alerts()
        notifier.check_news_alerts()
        notifier.send_pending_notifications()
    except Exception as e:
        logger.error(f"Notification checks failed: {e}")


def _sync_easyequities_if_due(db: Session) -> None:
    """Refresh holdings from EasyEquities when the last sync is stale.

    The Portfolio page also syncs on load, so this only matters on days the
    dashboard is never opened - without it, units/cost-basis/reconciliation
    (sold positions) silently lag for days. Gated to about once a day to
    stay well clear of EasyEquities rate limiting.
    """
    ee = EasyEquitiesCollector()
    if not ee.enabled:
        return
    portfolio = db.query(Portfolio).first()
    if not portfolio:
        return
    last = portfolio.last_easyequities_sync
    if last is not None and utcnow_naive() - last < EE_SYNC_MIN_AGE:
        return
    synced, _raw = sync_portfolio_from_easyequities(db, portfolio.id, ee)
    if synced is None:
        logger.warning(f"EasyEquities sync failed: {ee.last_error}")
        return
    portfolio.last_easyequities_sync = utcnow_naive()
    db.commit()
    logger.info(f"EasyEquities sync: {synced} holding(s)")


def _snapshot_portfolio(db: Session) -> None:
    """Sync brokers, revalue everything and record today's net-worth snapshot.

    Keeps the Portfolio page's value-history chart (JSE + crypto + bullion)
    filling in on days the dashboard is never opened, and keeps crypto/EE
    balances current without a page visit. Each step is best-effort:
    failures must never fail the update run.
    """
    luno = LunoCollector()
    if luno.enabled:
        try:
            if sync_luno_holdings(db, luno) is None:
                logger.warning(f"Luno sync failed: {luno.last_error}")
        except Exception as e:
            db.rollback()
            logger.error(f"Luno sync failed: {e}")

    try:
        _sync_easyequities_if_due(db)
    except Exception as e:
        db.rollback()
        logger.error(f"EasyEquities sync failed: {e}")

    try:
        value = revalue_portfolio(db)
        if value is None:
            return
        worth = compute_net_worth(db, jse_value=value)
        portfolio = db.query(Portfolio).first()
        record_snapshot(
            db, portfolio.id, portfolio.current_value,
            sum(h.quantity * h.purchase_price for h in valued_holdings(db, portfolio.id)),
            crypto_value=worth.crypto, bullion_value=worth.bullion,
        )
        logger.info(
            f"Net worth snapshot: R {worth.total:,.2f} (JSE R {value:,.2f}"
            + (f", crypto R {worth.crypto:,.2f}" if worth.crypto else "")
            + (f", bullion R {worth.bullion:,.2f}" if worth.bullion else "")
            + (f"; unpriced: {', '.join(worth.missing)}" if worth.missing else "")
            + ")"
        )
    except Exception as e:
        db.rollback()
        logger.error(f"Portfolio snapshot failed: {e}")


# Track the last time a logon update ran so a user can log on/off repeatedly
# without hammering the APIs. Always store this in the local data dir so it
# works for both SQLite and PostgreSQL.
LAST_LOGON_FILE = ROOT / "data" / ".updater_last_logon"

# Only one process may run a fetch/write job at a time (a manual
# `run_updater.py` next to the daemon, or the dashboard's refresh button,
# would otherwise double-fetch and double-write). The lock is taken per job
# - see app/services/process_lock.py - so the always-on daemon never locks
# out the logon/unlock refresh or a manual --full for the whole session.

# How long a job waits for another process's job to finish, per mode.
# Daemon jobs wait out a manual run; manual one-shots wait out a daemon job
# (a full run takes several minutes). Logon refreshes never wait - if
# another process is fetching right now, the data is being refreshed anyway.
PROCESS_LOCK_TIMEOUT_DAEMON = 10 * 60
PROCESS_LOCK_TIMEOUT_ONE_SHOT = 30 * 60


def _with_process_lock(job_name: str):
    """Decorator: run an UpdaterService job under the cross-process lock."""
    def decorate(method):
        @functools.wraps(method)
        def wrapper(self, *args, **kwargs):
            lock = process_lock.JOB_LOCK
            if not lock.acquire(timeout=self.process_lock_timeout):
                logger.warning(
                    f"{job_name} skipped: another AlphaLens process is running "
                    f"a fetch job (waited {self.process_lock_timeout:.0f}s)"
                )
                return False
            try:
                return method(self, *args, **kwargs)
            finally:
                lock.release()
        return wrapper
    return decorate

# Shared with the dashboard so the headless updater and UI never drift apart.
EXCLUDED_ACCOUNT_TYPES = PORTFOLIO_EXCLUDED_ACCOUNT_TYPES

# The daemon refreshes EasyEquities holdings at most this often (the
# Portfolio page syncs more eagerly on visits).
EE_SYNC_MIN_AGE = timedelta(hours=20)


def _get_tracked_symbols(db: Session) -> list:
    """Symbols relevant to the user: watchlist + non-excluded portfolio holdings + JSE index.

    Stocks deactivated as dead/delisted/bad tickers are excluded even when a
    watchlist item or holding still references them - otherwise deactivation
    is cosmetic and the updater keeps burning provider calls on them.
    """
    active_stocks = {
        s for (s,) in db.query(Stock.symbol).filter(Stock.is_active == True).all()
    }
    watchlist_symbols = [
        item.symbol for item in db.query(WatchlistItem).filter(WatchlistItem.is_active == True).all()
        if item.symbol in active_stocks
    ]
    portfolio_symbols = [
        h.symbol for h in db.query(PortfolioHolding).filter(
            ~PortfolioHolding.account_type.in_(EXCLUDED_ACCOUNT_TYPES)
        ).all()
        if is_jse_tracked_account(h.account_type) and h.symbol in active_stocks
    ]
    tracked = {settings.jse_index_yf_symbol}
    tracked.update(watchlist_symbols)
    tracked.update(portfolio_symbols)
    # No user data yet (the index alone doesn't count): fall back to the
    # configured default watchlist.
    if len(tracked) == 1:
        return sorted(set(settings.default_watchlist_symbols) | {settings.jse_index_yf_symbol})
    return sorted(tracked)


# A stock whose newest stored bar is older than this is treated as dead
# (delisted or an invalid ticker) and deactivated so daily jobs stop
# retrying it forever.
STALE_SYMBOL_MAX_AGE = timedelta(days=45)


def _record_run(db: Session, job_name: str, success: bool, detail: str = None) -> None:
    """Write a heartbeat row for the dashboard's stale-run banner.

    Best-effort - a logging failure here must never fail the job itself.
    """
    try:
        db.add(UpdaterRun(job_name=job_name, success=success, detail=detail))
        # Prune old rows so the table stays small.
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
        db.query(UpdaterRun).filter(UpdaterRun.finished_at < cutoff).delete()
        db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning(f"Could not record updater run: {e}")


def _maybe_deactivate_dead_symbol(db: Session, symbol: str) -> None:
    """Deactivate a Stock whose feed produces no recent bars.

    Called when a historical update saves nothing. The >45d guard means
    transient feed outages and healthy up-to-date symbols (which also save 0
    rows) are never affected:
    - with bars: the newest bar must be >45d old;
    - with no bars ever: the Stock must have existed for >45d. A brand-new
      symbol is left alone (the user is warned at add time); one that has
      never priced in 45 days is not on any feed (e.g. a suspended share held
      on EasyEquities) and only produces errors every run.
    Deactivation only stops price fetching - holdings keep their last
    EasyEquities-synced values.
    """
    stock = db.query(Stock).filter(Stock.symbol == symbol).first()
    if not stock or not stock.is_active:
        return
    latest = db.query(func.max(StockPrice.timestamp)).filter(
        StockPrice.symbol == symbol
    ).scalar()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if latest is not None:
        if now - latest <= STALE_SYMBOL_MAX_AGE:
            return
        reason = f"newest stored bar is {latest:%Y-%m-%d}"
    else:
        if stock.created_at is None or now - stock.created_at <= STALE_SYMBOL_MAX_AGE:
            return
        reason = f"never priced since it was added on {stock.created_at:%Y-%m-%d}"
    stock.is_active = False
    db.commit()
    logger.warning(
        f"Deactivated {symbol}: {reason} (>{STALE_SYMBOL_MAX_AGE.days}d) - "
        "likely delisted, suspended or an invalid ticker"
    )


def _estimate_api_calls(symbols: list, include_historical: bool = False) -> int:
    """Rough upper-bound of Twelve Data credits for the next run."""
    if not settings.twelve_data_enabled:
        return 0
    calls = len(symbols)  # batched quotes still cost one credit per symbol
    if include_historical:
        # The daily job also calls the history and dividends endpoints once
        # per symbol (before any Yahoo fallback).
        calls += 2 * len(symbols)
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

    # How old stored market data may get before the daemon makes up a missed
    # run itself, and how long to wait before retrying a failed catch-up.
    CATCH_UP_MAX_AGE = timedelta(hours=4)
    CATCH_UP_RETRY_SECONDS = 30 * 60
    # A failed backup (e.g. Postgres down) is retried at most this often.
    BACKUP_RETRY_SECONDS = 60 * 60
    # The 18:00 historical job is made up when no successful run is newer
    # than this - a day plus slack, so the scheduled run normally wins.
    HISTORICAL_MAX_AGE = timedelta(hours=26)
    HISTORICAL_RETRY_SECONDS = 60 * 60
    # Class-level: the Windows service restart loop builds a fresh
    # UpdaterService per retry, so an instance flag would never latch.
    _logging_configured = False

    def __init__(self):
        self.scheduler = BackgroundScheduler(timezone=settings.scheduler_timezone)
        self.scheduler.add_listener(self._on_job_error, EVENT_JOB_ERROR)
        # Serializes price+news runs across cron jobs, catch-up and logon
        # one-shots so they can never overlap.
        self._job_lock = threading.Lock()
        # Same for the historical job (cron vs catch-up).
        self._historical_lock = threading.Lock()
        self._last_catch_up_attempt: Optional[datetime] = None
        self._last_historical_attempt: Optional[datetime] = None
        self._last_backup_attempt: Optional[datetime] = None
        self.process_lock_timeout: float = PROCESS_LOCK_TIMEOUT_DAEMON

    def _on_job_error(self, event):
        logger.error(f"Scheduler job {event.job_id} failed: {event.exception}")

    def _catch_up_if_stale(self) -> None:
        """Run a price+news update when stored market data is too old.

        APScheduler drops jobs that misfire past their grace period (e.g.
        while the machine was asleep or hibernated), so the daemon checks
        once a minute whether the last successful fetch is older than
        CATCH_UP_MAX_AGE and makes up the missed run itself. This also
        covers daemon startup after downtime.
        """
        now = datetime.now(timezone.utc)
        if (
            self._last_catch_up_attempt is not None
            and (now - self._last_catch_up_attempt).total_seconds() < self.CATCH_UP_RETRY_SECONDS
        ):
            return

        db = SessionLocal()
        try:
            symbols = _get_tracked_symbols(db)
            if not symbols:
                return
            # Scope to tracked symbols: commodity/gold snapshot rows (AV_*,
            # XAU_* written by dashboard page visits) must not make stale JSE
            # data look fresh.
            latest_fetched, latest_bar = db.query(
                func.max(StockPrice.fetched_at), func.max(StockPrice.timestamp)
            ).filter(StockPrice.symbol.in_(symbols)).first()
        except Exception as e:
            logger.warning(f"Catch-up freshness check failed: {e}")
            return
        finally:
            db.close()

        if latest_bar is not None:
            # `timestamp` is naive exchange-local midnight, not a UTC instant
            # like fetched_at - convert before comparing so a recent bar can't
            # look up to a UTC-offset's worth "newer" than it really is.
            try:
                latest_bar = (
                    latest_bar.replace(tzinfo=ZoneInfo(settings.scheduler_timezone))
                    .astimezone(timezone.utc)
                    .replace(tzinfo=None)
                )
            except Exception as e:
                # SCHEDULER_TIMEZONE is user-configurable - a bad value raises
                # ZoneInfoNotFoundError; skip this check rather than crash.
                logger.warning(f"Catch-up bar-time conversion failed: {e}")
                return
        latest = max(
            (t for t in (latest_fetched, latest_bar) if t is not None), default=None
        )
        if latest is not None:
            if latest.tzinfo is None:
                latest = latest.replace(tzinfo=timezone.utc)
            if now - latest <= self.CATCH_UP_MAX_AGE:
                return

        self._last_catch_up_attempt = now
        logger.info(
            f"Market data stale (last fetch: {latest or 'never'}) - running catch-up update"
        )
        if not self._update_prices_and_news():
            logger.warning("Catch-up update did not complete successfully")

    @_with_process_lock("Price + news update")
    def _update_prices_and_news(self) -> bool:
        """3x-daily job: update tracked stock prices and collect news.

        Returns True on success so one-shot invocations can propagate a
        non-zero exit code to Task Scheduler.
        """
        if not self._job_lock.acquire(blocking=False):
            logger.warning("Price + news update skipped: another update is in progress")
            return False
        logger.info("Running scheduled price + news update")
        ok = False
        detail = None
        db = None
        try:
            # Inside try: a SessionLocal() failure must still reach the
            # finally that releases _job_lock or it stays held forever.
            db = SessionLocal()
            symbols = _get_tracked_symbols(db)
            quota_ok, msg = _has_quota(symbols, include_historical=False)
            logger.info(msg)

            data_service = DataService(db)
            if not quota_ok:
                # The plan exceeds today's remaining quota: go straight to
                # Yahoo instead of burning the remainder into 429s.
                data_service.twelve_data.force_disabled = True
            updated = data_service.update_stock_prices(symbols)
            logger.info(f"Updated {updated} tracked stock prices")
            prices_ok = not symbols or updated > 0
            if not prices_ok:
                detail = "0 symbols updated"
                logger.error(
                    "Price update refreshed 0 symbols - marking the run failed "
                    "but still collecting news/alerts/snapshot"
                )
            if data_service.last_failed_symbols and detail is None:
                detail = f"no price for: {','.join(data_service.last_failed_symbols)}"[:200]
                logger.warning(
                    f"Symbols with no fresh price this run: {data_service.last_failed_symbols}"
                )

            # A total price failure (e.g. Yahoo down) must not also stop news,
            # alert checks or the daily portfolio snapshot - they don't depend
            # on this run's prices.
            news_scraper = NewsScraper(db)
            articles = news_scraper.scrape_all_sources()
            logger.info(f"Collected {len(articles)} news articles")

            _run_notification_checks(db)
            _snapshot_portfolio(db)
            ok = prices_ok
            return prices_ok
        except Exception as e:
            detail = str(e)[:200]
            logger.error(f"Price + news update failed: {e}")
            # A dirty session here would leak partial writes into
            # _record_run's commit.
            if db is not None:
                db.rollback()
            return False
        finally:
            try:
                if db is not None:
                    _record_run(db, "price_news_update", ok, detail)
            finally:
                if db is not None:
                    db.close()
                self._job_lock.release()

    @_with_process_lock("Historical update")
    def _update_historical(self) -> bool:
        """Daily job: update historical data and dividends for tracked symbols."""
        if not self._historical_lock.acquire(blocking=False):
            logger.warning("Historical update skipped: another historical update is in progress")
            return False
        logger.info("Running daily historical update")
        ok = False
        detail = None
        db = None
        try:
            db = SessionLocal()
            symbols = _get_tracked_symbols(db)
            quota_ok, msg = _has_quota(symbols, include_historical=True)
            logger.info(msg)

            data_service = DataService(db)
            if not quota_ok:
                data_service.twelve_data.force_disabled = True
            results, failed_symbols = data_service.update_historical_data_batch(symbols, days=365)
            failed = len(failed_symbols)
            for symbol in symbols:
                if symbol not in failed_symbols and results.get(symbol, 0) == 0:
                    _maybe_deactivate_dead_symbol(db, symbol)

            data_service.update_dividends(symbols)

            # The Moneyweb Dividend Watch calendar was previously only
            # refreshed by a manual button on the Dividends page - refresh it
            # daily so it never silently goes stale.
            try:
                moneyweb = MoneywebCollector()
                if moneyweb.enabled:
                    watch_rows = moneyweb.save_dividend_watch(db)
                    logger.info(f"Moneyweb dividend watch refreshed: {watch_rows} rows")
            except Exception as e:
                db.rollback()
                logger.warning(f"Dividend watch refresh failed: {e}")

            # Near-52w-low penny alerts ride on this job so they run on the
            # day's closes and inherit its catch-up after sleep. One batched
            # Yahoo request; a failure here never fails the historical run.
            try:
                from app.services.penny_scan import run_near_low_alerts
                run_near_low_alerts(db)
            except Exception as e:
                db.rollback()
                logger.warning(f"Penny near-52w-low alert check failed: {e}")

            logger.info(f"Daily historical update completed for {len(symbols)} symbols")
            if symbols and failed == len(symbols):
                detail = f"historical failed for all {failed} symbols"
                logger.error("Historical update failed for every symbol - treating the run as failed")
                return False
            detail = f"{failed}/{len(symbols)} failed" if failed else None
            ok = True
            return True
        except Exception as e:
            detail = str(e)[:200]
            logger.error(f"Daily historical update failed: {e}")
            # A dirty session here would leak partial writes into
            # _record_run's commit.
            if db is not None:
                db.rollback()
            return False
        finally:
            try:
                if db is not None:
                    _record_run(db, "daily_historical", ok, detail)
            finally:
                if db is not None:
                    db.close()
                self._historical_lock.release()

    @_with_process_lock("Close normalization")
    def _normalize_closes(self) -> bool:
        """One-shot repair: rewrite stored bars on the adjusted-close convention.

        Needed once after the raw/adjusted mixing fix - see
        DataService.normalize_closes. Run via `run_updater.py --normalize-closes`.
        """
        db = SessionLocal()
        try:
            symbols = _get_tracked_symbols(db)
            if not symbols:
                return True
            fixed = DataService(db).normalize_closes(symbols)
            return fixed > 0
        except Exception as e:
            logger.error(f"Close normalization failed: {e}")
            db.rollback()
            return False
        finally:
            db.close()

    def _historical_catch_up_if_due(self) -> None:
        """Run the historical job when no successful run is recent enough.

        The 18:00 cron job misfires whenever the machine is asleep or off at
        that time (APScheduler drops it after misfire_grace_time), which left
        history, dividends and dead-symbol deactivation silently not running
        for days. The daemon's minute loop makes it up from the heartbeat.
        """
        now = datetime.now(timezone.utc)
        if (
            self._last_historical_attempt is not None
            and (now - self._last_historical_attempt).total_seconds() < self.HISTORICAL_RETRY_SECONDS
        ):
            return

        db = SessionLocal()
        try:
            last_ok = db.query(func.max(UpdaterRun.finished_at)).filter(
                UpdaterRun.job_name == "daily_historical",
                UpdaterRun.success == True,
            ).scalar()
        except Exception as e:
            logger.warning(f"Historical catch-up check failed: {e}")
            return
        finally:
            db.close()

        if last_ok is not None and now - last_ok.replace(tzinfo=timezone.utc) <= self.HISTORICAL_MAX_AGE:
            return

        self._last_historical_attempt = now
        logger.info(
            f"No successful historical update since {last_ok or 'ever'} - running catch-up"
        )
        if not self._update_historical():
            logger.warning("Historical catch-up did not complete successfully")

    @_with_process_lock("Weekly cleanup")
    def _weekly_cleanup(self) -> bool:
        """Weekly job: delete old news articles and old notifications."""
        logger.info("Running weekly cleanup")
        db = SessionLocal()
        ok = False
        detail = None
        try:
            news_scraper = NewsScraper(db)
            deleted = news_scraper.clean_old_articles(days=90)
            # Alert rows are written unconditionally on every check run;
            # without pruning the table grows forever.
            notification_cutoff = utcnow_naive() - timedelta(days=90)
            deleted_notifications = db.query(Notification).filter(
                Notification.created_at < notification_cutoff
            ).delete()
            db.commit()
            logger.info(
                f"Cleaned up {deleted} old news articles, "
                f"{deleted_notifications} old notifications"
            )
            ok = True
            return True
        except Exception as e:
            detail = str(e)[:200]
            logger.error(f"Weekly cleanup failed: {e}")
            db.rollback()
            return False
        finally:
            try:
                _record_run(db, "weekly_cleanup", ok, detail)
            finally:
                db.close()

    @_with_process_lock("SENS IPO scan")
    def _update_ipo_listings(self) -> bool:
        """Daily job: scan Moneyweb SENS for IPO announcements (public page)."""
        logger.info("Running SENS IPO scan")
        db = SessionLocal()
        ok = False
        detail = None
        try:
            from app.services.ipo import update_ipo_listings
            added = update_ipo_listings(db)
            detail = f"{added} new" if added else None
            ok = True
            return True
        except Exception as e:
            detail = str(e)[:200]
            logger.error(f"SENS IPO scan failed: {e}")
            db.rollback()
            return False
        finally:
            try:
                _record_run(db, "ipo_listings", ok, detail)
            finally:
                db.close()

    def _run_backup(self) -> bool:
        """Back up the database and record the run."""
        logger.info("Running database backup")
        ok = False
        detail = None
        try:
            path = backup.backup_database()
            detail = path.name
            ok = True
            return True
        except Exception as e:
            detail = str(e)[:200]
            logger.error(f"Database backup failed: {e}")
            return False
        finally:
            db = SessionLocal()
            try:
                _record_run(db, "database_backup", ok, detail)
            finally:
                db.close()

    def _backup_if_due(self) -> None:
        """Take a backup when the newest one is older than the interval."""
        now = datetime.now(timezone.utc)
        if (
            self._last_backup_attempt is not None
            and (now - self._last_backup_attempt).total_seconds() < self.BACKUP_RETRY_SECONDS
        ):
            return
        try:
            if not backup.backup_due():
                return
        except Exception as e:
            logger.warning(f"Backup due check failed: {e}")
            return
        self._last_backup_attempt = now
        self._run_backup()

    def configure(self):
        """Register the default 3x/day + logon schedule."""
        self.scheduler.add_job(
            self._update_prices_and_news,
            trigger=CronTrigger(hour="9,13,17", minute=0),
            id="price_news_update",
            name="Price and News Update (3x daily)",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
        )
        self.scheduler.add_job(
            self._update_historical,
            trigger=CronTrigger(hour=18, minute=0),
            id="daily_historical",
            name="Daily Historical + Dividend Update",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
        )
        self.scheduler.add_job(
            self._update_ipo_listings,
            trigger=CronTrigger(hour=8, minute=0),
            id="ipo_listings",
            name="Daily SENS IPO Scan",
            replace_existing=True,
            misfire_grace_time=86400,
            coalesce=True,
        )
        self.scheduler.add_job(
            self._weekly_cleanup,
            trigger=CronTrigger(day_of_week="sun", hour=2, minute=0),
            id="weekly_cleanup",
            name="Weekly Old News Cleanup",
            replace_existing=True,
            misfire_grace_time=86400,
            coalesce=True,
        )

    def setup(self):
        """Configure logging, set the working directory and initialize the database."""
        os.chdir(ROOT)
        Path(settings.updater_log_file).parent.mkdir(parents=True, exist_ok=True)
        # Guarded: the Windows service's crash-restart loop calls setup() on
        # every retry - each unguarded logger.add stacks another file sink and
        # another stdlib->loguru handler, duplicating every line.
        if not UpdaterService._logging_configured:
            logger.add(
                settings.updater_log_file,
                rotation="10 MB",
                retention="30 days",
                level=settings.log_level,
            )

            # APScheduler logs misfires/errors through stdlib logging, not loguru -
            # route them into the same file so dropped jobs are visible.
            class _LoguruHandler(logging.Handler):
                def emit(self, record):
                    logger.opt(depth=6, exception=record.exc_info).log(
                        record.levelname, record.getMessage()
                    )

            apscheduler_logger = logging.getLogger("apscheduler")
            apscheduler_logger.addHandler(_LoguruHandler())
            apscheduler_logger.setLevel(logging.INFO)
            UpdaterService._logging_configured = True

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
                # A single failed check must never kill the daemon - each
                # helper is internally guarded, but not airtight (e.g. a bad
                # SCHEDULER_TIMEZONE used to escape _catch_up_if_stale).
                try:
                    self._catch_up_if_stale()
                    self._historical_catch_up_if_due()
                    self._backup_if_due()
                except Exception:
                    logger.exception("Minute-loop check failed - continuing")
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
          - backup: back up the database now and exit.
          - ipo: run the SENS IPO scan once and exit.

        One-shot modes exit non-zero on failure so Task Scheduler surfaces the
        run as failed instead of silently succeeding.
        """
        self.setup()

        # Jobs take the cross-process lock themselves (per job, not per
        # process). One-shots wait out a running daemon job; a one-shot that
        # still can't get the lock reports failure (exit 1) rather than
        # silently "succeeding" with nothing done.
        if mode != "daemon":
            self.process_lock_timeout = PROCESS_LOCK_TIMEOUT_ONE_SHOT

        if mode == "logon":
            if _should_run_logon_update(min_seconds=settings.updater_logon_min_interval or 600):
                # Another process fetching right now (the daemon's catch-up,
                # a dashboard refresh) is refreshing the same data - skip.
                if not process_lock.JOB_LOCK.acquire(timeout=0):
                    logger.info("Skipping logon update: another process is fetching right now")
                    return
                try:
                    logger.info("Running logon-triggered update")
                    ok = self._update_prices_and_news()
                    # A failed run must not eat the debounce - let the next
                    # logon attempt retry instead of waiting out the interval.
                    if ok:
                        _record_logon_update()
                finally:
                    process_lock.JOB_LOCK.release()
                sys.exit(0 if ok else 1)
            else:
                logger.info("Skipping logon update: ran too recently")
            return

        if mode == "price_news":
            logger.info("Running price + news update")
            ok = self._update_prices_and_news()
            sys.exit(0 if ok else 1)

        if mode == "backup":
            sys.exit(0 if self._run_backup() else 1)

        if mode == "ipo":
            sys.exit(0 if self._update_ipo_listings() else 1)

        if mode == "normalize_closes":
            sys.exit(0 if self._normalize_closes() else 1)

        if mode == "full":
            logger.info("Running full update")
            ok = self._update_prices_and_news()
            ok = self._update_historical() and ok
            sys.exit(0 if ok else 1)

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
    group.add_argument("--backup", action="store_true", help="Back up the database now and exit")
    group.add_argument("--ipo", action="store_true", help="Scan Moneyweb SENS for IPO announcements and exit")
    group.add_argument("--normalize-closes", action="store_true", help="Rewrite all stored bars on the adjusted-close convention and exit")
    args = parser.parse_args()

    mode = "daemon"
    if args.price_news:
        mode = "price_news"
    elif args.full:
        mode = "full"
    elif args.logon:
        mode = "logon"
    elif args.backup:
        mode = "backup"
    elif args.ipo:
        mode = "ipo"
    elif args.normalize_closes:
        mode = "normalize_closes"

    service = UpdaterService()
    service.start(mode=mode)


if __name__ == "__main__":
    main()
