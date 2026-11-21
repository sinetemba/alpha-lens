"""SENS announcements for the JSE shares you hold: store, alert, classify.

The daily historical job calls refresh_holding_announcements(): one request
per held JSE share to its Moneyweb company page (~10 most recent SENS
each), new ones stored in `sens_announcements` and raised as `type="sens"`
notifications. Announcements that may need a decision from a shareholder
(repurchase/takeover offers, delisting, business rescue...) are flagged in
the title. holding_statuses() feeds the Portfolio page: which holdings are
suspended, and any pending corporate action.

Why company pages rather than the all-company SENS page: that page spans
about one trading day, so a day the PC slept through would lose
announcements for good. Streamlit-free - the updater imports it.
"""
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.base import utcnow_naive
from app.models.sens_announcement import SensAnnouncement

# First fetch for a symbol backfills its history - only alert on what's recent.
BACKFILL_ALERT_WINDOW = timedelta(days=3)
REQUEST_SPACING_SECONDS = 0.5
# A suspension counts as current when its latest suspension announcement is
# this recent (JSE requires quarterly suspension updates) and nothing later
# says trading resumed.
SUSPENSION_FRESH = timedelta(days=200)

# Trading suspensions only - "temporary suspension of operations at <mine>"
# (WEZ) and "suspensive conditions" (ENX) are not.
SUSPENSION_PHRASES = ("suspension update", "suspension of trading", "suspension of listing",
                      "suspension in trading", "trading suspended", "suspended from trading",
                      "remain suspended")
RESUMPTION_PHRASES = ("lifting of suspension", "suspension lifted", "resumption of trading",
                      "lifting of the suspension", "trading resumes")
# Headlines that can require a shareholder decision or change what you hold.
# Routine AGMs are deliberately absent (every company has one a year - they
# flagged 28 of 42 holdings); a meeting that decides something comes with a
# circular, which is matched.
CORPORATE_ACTION_PHRASES = (
    "repurchase offer", "offer to shareholders", "mandatory offer", "firm intention",
    "scheme of arrangement", "delisting", "delist", "odd-lot", "odd lot", "rights offer",
    "unbundling", "business rescue", "liquidation", "takeover", "tender offer", "circular",
)
# Outcomes of something already decided - nothing left to act on.
CORPORATE_ACTION_EXCLUDE = ("results of", "report of proceedings", "report on proceedings",
                            "withdrawal of cautionary")


def _has(headline: str, phrases) -> bool:
    text = (headline or "").lower()
    return any(p in text for p in phrases)


def _phrases(headline: str) -> set:
    """Corporate-action phrases in a headline ('odd lot' normalised to 'odd-lot')."""
    text = (headline or "").lower().replace("odd lot", "odd-lot")
    return {p for p in CORPORATE_ACTION_PHRASES if p != "odd lot" and p in text}


def is_corporate_action(headline: str) -> bool:
    return _has(headline, CORPORATE_ACTION_PHRASES) and not _has(headline, CORPORATE_ACTION_EXCLUDE)


def _to_naive_utc(ts):
    if ts is None:
        return None
    if ts.tzinfo is not None:
        from datetime import timezone
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


def held_sens_symbols(db: Session) -> list[str]:
    """Held JSE share codes (no indexes/ETF pseudo-symbols with . ^ _ =)."""
    from app.services.penny_scan import held_jse_symbols
    return sorted(s for s in held_jse_symbols(db) if s and not any(c in s for c in ".^_="))


def refresh_holding_announcements(db: Session, collector=None, symbols=None,
                                  spacing: float = REQUEST_SPACING_SECONDS) -> Optional[int]:
    """Fetch, store and alert new SENS for held shares. Returns the number of
    new announcements stored, or None when every fetch failed."""
    from app.collectors.moneyweb import MoneywebCollector
    from app.notifications.notification_service import NotificationService
    from app.services.source_health import record_source

    collector = collector or MoneywebCollector()
    symbols = held_sens_symbols(db) if symbols is None else symbols
    if not symbols:
        return 0

    known_urls = {u for (u,) in db.query(SensAnnouncement.source_url).all()}
    has_history = {s for (s,) in db.query(SensAnnouncement.symbol).distinct().all()}
    now = utcnow_naive()
    notifier = NotificationService(db)
    stored, failed, alerts = 0, [], []

    for i, symbol in enumerate(symbols):
        if i and spacing:
            time.sleep(spacing)
        rows = collector.get_company_sens(symbol)
        if rows is None:
            failed.append(symbol)
            continue
        backfill = symbol not in has_history
        for row in rows:
            url, headline = row.get("source_url"), row.get("headline")
            if not url or not headline or url in known_urls:
                continue
            known_urls.add(url)
            published = _to_naive_utc(row.get("published_at"))
            ann = SensAnnouncement(
                symbol=symbol, name=row.get("name"), headline=headline[:500],
                published_at=published, source_url=url[:1000],
            )
            db.add(ann)
            stored += 1
            if not backfill or (published and now - published <= BACKFILL_ALERT_WINDOW):
                alerts.append(ann)
    try:
        db.commit()
    except IntegrityError:
        # A concurrent refresh stored some of these first - keep theirs, no alerts.
        db.rollback()
        logger.warning("SENS refresh hit a concurrent write; skipping alerts this run")
        alerts, stored = [], 0

    for ann in alerts:
        action = is_corporate_action(ann.headline)
        when = f"{ann.published_at:%d %b %Y %H:%M} UTC" if ann.published_at else "recently"
        try:
            notifier.create_notification(
                notification_type="sens",
                title=(f"Action may be needed - {ann.symbol}: {ann.headline}" if action
                       else f"SENS {ann.symbol}: {ann.headline}")[:450],
                message=(
                    f"{ann.name or ann.symbol} published \"{ann.headline}\" on SENS ({when}). "
                    + ("This kind of announcement can need a decision from shareholders "
                       "(e.g. accepting an offer) - read it: " if action else "Read it: ")
                    + ann.source_url
                ),
                stock_symbol=ann.symbol,
                metadata={"url": ann.source_url, "corporate_action": action},
            )
        except Exception as e:
            logger.warning(f"Could not create SENS notification for {ann.symbol}: {e}")

    ok = len(failed) < len(symbols)
    record_source(db, "moneyweb_company_sens", ok,
                  f"failed for {len(failed)}/{len(symbols)}: {', '.join(failed[:10])}" if failed else None)
    logger.info(f"Holding SENS: {stored} new announcement(s), {len(alerts)} alerted, "
                f"{len(failed)}/{len(symbols)} fetches failed")
    return stored if ok else None


@dataclass
class HoldingStatus:
    symbol: str
    suspended: bool = False
    suspension: Optional[SensAnnouncement] = None        # latest suspension notice
    corporate_action: Optional[SensAnnouncement] = None  # latest decision-type notice
    latest: Optional[SensAnnouncement] = None
    recent: list = field(default_factory=list)


def holding_statuses(db: Session, symbols, now=None) -> dict[str, HoldingStatus]:
    """{symbol: HoldingStatus} from stored announcements (newest first)."""
    now = now or utcnow_naive()
    symbols = list(symbols)
    if not symbols:
        return {}
    rows = db.query(SensAnnouncement).filter(
        SensAnnouncement.symbol.in_(symbols)
    ).order_by(SensAnnouncement.published_at.desc().nullslast()).all()
    out: dict[str, HoldingStatus] = {}
    concluded: dict[str, set] = {}   # symbol -> action phrases a later "results of ..." closed
    for ann in rows:
        st = out.setdefault(ann.symbol, HoldingStatus(ann.symbol))
        if st.latest is None:
            st.latest = ann
        st.recent.append(ann)
        phrases = _phrases(ann.headline)
        if _has(ann.headline, CORPORATE_ACTION_EXCLUDE):
            # Newest-first: an outcome ("Results of the Odd-lot Offer") closes
            # the earlier announcements of the same action (RMH's offer price).
            concluded.setdefault(ann.symbol, set()).update(phrases)
        elif (st.corporate_action is None and phrases
              and not phrases & concluded.get(ann.symbol, set())):
            st.corporate_action = ann
        if st.suspension is None and not _has(ann.headline, RESUMPTION_PHRASES) \
                and _has(ann.headline, SUSPENSION_PHRASES):
            st.suspension = ann
    for st in out.values():
        resumed = next((a for a in st.recent if _has(a.headline, RESUMPTION_PHRASES)), None)
        s = st.suspension
        st.suspended = bool(
            s and s.published_at and now - s.published_at <= SUSPENSION_FRESH
            and (resumed is None or (resumed.published_at or now) < s.published_at)
        )
    return out
