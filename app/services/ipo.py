"""Upcoming JSE IPOs / new listings, spotted in Moneyweb SENS.

Genuine company IPOs are rare (~5-10/year), so the filter favours
precision: whitelist real-IPO phrases, exclude the daily flood of warrant/
note/ETF instrument listings and interest-payment announcements. Dedup is
by `source_url`, so the daily scan is idempotent.
"""
from datetime import timedelta
from typing import Optional

import pandas as pd
from loguru import logger
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.collectors.moneyweb import MoneywebCollector
from app.collectors.yfinance import YFinanceCollector
from app.models.base import utcnow_naive
from app.models.stock import StockPrice
from app.models.upcoming_listing import UpcomingListing

# Real-IPO phrases in SENS headlines (matched case-insensitively).
IPO_INCLUDE_PHRASES = (
    "proposed listing",
    "pre-listing statement",
    "intention to list",
    "admission to listing",
    "initial public offer",
)

# Daily noise that would trip the include phrases - checked first.
IPO_EXCLUDE_PHRASES = (
    "new financial instrument",
    "listing of additional",
    "listing of transition",
    "listing of tap",
    "tap issue",
    "interest payment",
    "interest payable",
    "capital redemption",
    "partial delisting",
    "final redemption",
    "partial redemption",
    # Capital raises by companies that are already listed. ("Private
    # placement" alone is NOT excluded - IPOs are often done "by way of a
    # private placement"; a listed company's placement is caught instead by
    # the already-trading check in _mark_newly_listed.)
    "additional shares",
    "accelerated bookbuild",
    "rights offer",
)

# An announcement that never reaches a listing day stops being probed (and
# leaves the "upcoming" table) after this long. Offer periods run weeks, and
# postponed IPOs are re-announced with a new SENS (new source_url).
ANNOUNCEMENT_EXPIRY = timedelta(days=180)

# status values: announced -> listed (began trading after the announcement)
#                announced -> existing (the ticker was already trading before
#                             it - a listed company's capital raise, not an IPO)
#                announced -> expired (never listed within ANNOUNCEMENT_EXPIRY)


def is_ipo_announcement(headline: Optional[str]) -> bool:
    """Whether a SENS headline looks like a genuine company IPO announcement."""
    text = (headline or "").lower()
    if any(p in text for p in IPO_EXCLUDE_PHRASES):
        return False
    return any(p in text for p in IPO_INCLUDE_PHRASES)


def update_ipo_listings(db: Session, collector=None) -> int:
    """Scan SENS, store new IPO announcements, mark listed ones, notify.

    Returns the number of new announcements alerted as possible IPOs
    (stored announcements whose ticker was already trading are not counted).
    """
    from app.notifications.notification_service import NotificationService

    collector = collector or MoneywebCollector()
    announcements = collector.get_sens_announcements() or []
    existing_urls = {
        u for (u,) in db.query(UpcomingListing.source_url).all()
    }
    notifier = NotificationService(db)
    new_listings = []
    for ann in announcements:
        url = ann.get("source_url")
        headline = ann.get("headline")
        if not url or url in existing_urls or not is_ipo_announcement(headline):
            continue
        published = ann.get("published_at")
        if published is not None and published.tzinfo is not None:
            published = published.replace(tzinfo=None)
        listing = UpcomingListing(
            symbol=ann.get("symbol"),
            name=ann.get("name"),
            headline=(headline or "")[:500],
            sens_published_at=published,
            source_url=url,
        )
        db.add(listing)
        new_listings.append(listing)
    if new_listings:
        try:
            db.commit()
        except IntegrityError:
            # A concurrent scan stored one first - keep theirs, notify on none.
            db.rollback()
            new_listings = []
    # Classify before alerting: an announcement whose ticker is already
    # trading is a listed company's capital raise, not an IPO - it must not
    # raise a "Possible IPO" alert (or a bogus "Now trading" one).
    _mark_newly_listed(db, notifier)
    alerted = 0
    for listing in new_listings:
        if listing.status not in ("announced", "listed"):
            continue
        alerted += 1
        notifier.create_notification(
            notification_type="ipo",
            title=f"Possible IPO: {listing.name or listing.symbol or 'JSE listing'}",
            message=f"{listing.headline}\n{listing.source_url}",
            stock_symbol=listing.symbol,
            metadata={"source_url": listing.source_url},
        )
    if new_listings:
        logger.info(
            f"SENS scan: {len(new_listings)} new IPO-shaped announcement(s), "
            f"{alerted} alerted"
        )
    return alerted


def _announced_on(listing):
    """Calendar day of the announcement (falls back to when we stored it)."""
    when = listing.sens_published_at or listing.created_at or utcnow_naive()
    return when.date()


def _first_bar_day(frame):
    """Earliest bar date in a Yahoo frame, or None if the index isn't dated."""
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.empty:
        return None
    return frame.index.min().date()


def _mark_newly_listed(db: Session, notifier) -> int:
    """Resolve 'announced' rows once Yahoo serves bars for the ticker.

    Bars dated before the announcement mean the company was already listed
    ("existing", no alert); bars only from the announcement day on mean it
    has just started trading ("listed", "Now trading" alert). Rows older than
    ANNOUNCEMENT_EXPIRY are retired instead of being probed forever.
    """
    pending = db.query(UpcomingListing).filter(
        UpcomingListing.status == "announced",
        UpcomingListing.symbol.isnot(None),
    ).all()
    cutoff = (utcnow_naive() - ANNOUNCEMENT_EXPIRY).date()
    changed = False
    for listing in pending:
        if _announced_on(listing) < cutoff:
            listing.status = "expired"
            changed = True
    pending = [l for l in pending if l.status == "announced"]

    # Stored history from before the announcement settles it without Yahoo.
    if pending:
        earliest = dict(db.query(
            StockPrice.symbol, func.min(StockPrice.timestamp)
        ).filter(
            StockPrice.symbol.in_({l.symbol for l in pending})
        ).group_by(StockPrice.symbol).all())
        for listing in pending:
            first = earliest.get(listing.symbol)
            if first is not None and first.date() < _announced_on(listing):
                listing.status = "existing"
                changed = True
        pending = [l for l in pending if l.status == "announced"]

    if not pending:
        if changed:
            db.commit()
        return 0
    symbols = [l.symbol for l in pending]
    try:
        frames = YFinanceCollector().get_latest_bars(symbols, period="5d") or {}
    except Exception as e:
        logger.warning(f"IPO listing-day probe failed: {e.__class__.__name__}")
        return 0
    listed = 0
    for listing in pending:
        frame = frames.get(listing.symbol)
        if frame is None or getattr(frame, "empty", True):
            continue
        first_day = _first_bar_day(frame)
        if first_day is not None and first_day < _announced_on(listing):
            listing.status = "existing"
            changed = True
            logger.info(
                f"SENS scan: {listing.symbol} was already trading before "
                f"'{listing.headline}' - not an IPO"
            )
            continue
        listing.status = "listed"
        listed += 1
        notifier.create_notification(
            notification_type="ipo",
            title=f"Now trading: {listing.name or listing.symbol}",
            message=(
                f"{listing.symbol} has started trading on the JSE.\n"
                f"{listing.source_url}"
            ),
            stock_symbol=listing.symbol,
            metadata={"source_url": listing.source_url},
        )
    if listed or changed:
        db.commit()
    if listed:
        logger.info(f"SENS scan: {listed} listing(s) went live")
    return listed
