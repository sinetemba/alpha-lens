"""Per-data-source health: heartbeats, status, and failing-source alerts.

Each fetch of an external source records one row in `updater_runs` with
job_name "source:<key>" (no new table - same heartbeat mechanism as the
updater jobs). The Settings page lists every source's last success and
failure; check_source_health() raises a system notification for a source
that has kept failing for FAILING_ALERT_AFTER without one success - e.g.
the Moneyweb login being rejected or a feed changing its layout, which
used to appear only in the log.

Silence is not failure: a source that simply wasn't fetched (the
Commodities page not opened) is never alerted on. Streamlit-free - the
updater imports it. Recording is best-effort and must never fail a caller.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.base import utcnow_naive
from app.models.updater_run import UpdaterRun

PREFIX = "source:"
FAILING_ALERT_AFTER = timedelta(hours=48)
HISTORY_DAYS = 30

# key -> display label. RSS feeds are keyed "rss:<host>" and labelled dynamically.
SOURCES = {
    "yahoo_prices": "Yahoo Finance - share prices",
    "yahoo_futures": "Yahoo Finance - energy, agriculture & livestock futures",
    "safex": "Grain SA - SAFEX maize & wheat",
    "metals_dev": "Metals.dev - metal spot prices",
    "moneyweb_login": "Moneyweb - login (Dividend Watch)",
    "moneyweb_sens": "Moneyweb - SENS announcements (IPO scan)",
    "easyequities": "EasyEquities - holdings sync",
    "luno": "Luno - crypto balances",
}


def rss_key(url: str) -> str:
    return f"rss:{urlparse(url).netloc or url}"[:40]


def label_for(key: str) -> str:
    if key.startswith("rss:"):
        return f"RSS news - {key[4:]}"
    return SOURCES.get(key, key)


def record_source(db: Session, key: str, ok: bool, detail: Optional[str] = None) -> None:
    """Write one heartbeat for a source. Commits; call after the caller's
    own commit. Never raises."""
    try:
        db.add(UpdaterRun(
            job_name=f"{PREFIX}{key}"[:50], success=ok,
            detail=(detail or None) and str(detail)[:500],
        ))
        db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning(f"Could not record source health for {key}: {e}")


@dataclass
class SourceStatus:
    key: str
    label: str
    last_success: Optional[datetime]
    last_failure: Optional[datetime]
    last_detail: Optional[str]
    failing_since: Optional[datetime]   # first failure after the last success

    @property
    def ok(self) -> bool:
        return self.last_failure is None or (
            self.last_success is not None and self.last_success > self.last_failure
        )

    def failing_for(self, now: datetime) -> Optional[timedelta]:
        return None if self.ok or self.failing_since is None else now - self.failing_since


def source_statuses(db: Session) -> list[SourceStatus]:
    """Status of every source that has recorded at least once."""
    rows = db.query(
        UpdaterRun.job_name, UpdaterRun.success,
        func.max(UpdaterRun.finished_at),
    ).filter(UpdaterRun.job_name.like(f"{PREFIX}%")).group_by(
        UpdaterRun.job_name, UpdaterRun.success
    ).all()
    latest: dict[str, dict] = {}
    for job_name, success, at in rows:
        latest.setdefault(job_name, {})[bool(success)] = at

    statuses = []
    for job_name, by_outcome in latest.items():
        last_ok, last_fail = by_outcome.get(True), by_outcome.get(False)
        failing_since = detail = None
        if last_fail is not None:
            detail_row = db.query(UpdaterRun.detail).filter(
                UpdaterRun.job_name == job_name, UpdaterRun.success.is_(False),
            ).order_by(UpdaterRun.finished_at.desc()).first()
            detail = detail_row[0] if detail_row else None
            if last_ok is None or last_fail > last_ok:
                first = db.query(func.min(UpdaterRun.finished_at)).filter(
                    UpdaterRun.job_name == job_name,
                    UpdaterRun.success.is_(False),
                    *( [UpdaterRun.finished_at > last_ok] if last_ok else [] ),
                ).scalar()
                failing_since = first
        key = job_name[len(PREFIX):]
        statuses.append(SourceStatus(key, label_for(key), last_ok, last_fail, detail, failing_since))
    return sorted(statuses, key=lambda s: (s.ok, s.label))


def check_source_health(db: Session) -> int:
    """Notify for each source failing for FAILING_ALERT_AFTER+ with no
    success; returns how many are failing that long. Also prunes old rows.
    Notifications dedup over 20h, so this is at most a daily reminder."""
    now = utcnow_naive()
    failing = [
        s for s in source_statuses(db)
        if (s.failing_for(now) or timedelta(0)) >= FAILING_ALERT_AFTER
    ]
    if failing:
        from app.notifications.notification_service import NotificationService
        notifier = NotificationService(db)
        for s in failing:
            days = s.failing_for(now).total_seconds() / 86400
            last_ok = f"last success {s.last_success:%d %b %H:%M} UTC" if s.last_success else "no success recorded"
            try:
                notifier.create_notification(
                    notification_type="system",
                    title=f"Data source failing: {s.label}"[:200],
                    message=(
                        f"{s.label} has failed every attempt for {days:.1f} days ({last_ok}). "
                        f"Last error: {s.last_detail or 'n/a'}. See Settings > Data sources."
                    )[:1000],
                )
            except Exception as e:
                logger.warning(f"Could not notify failing source {s.key}: {e}")
    try:
        cutoff = now - timedelta(days=HISTORY_DAYS)
        db.query(UpdaterRun).filter(
            UpdaterRun.job_name.like(f"{PREFIX}%"), UpdaterRun.finished_at < cutoff
        ).delete(synchronize_session=False)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"Could not prune source health rows: {e}")
    return len(failing)
