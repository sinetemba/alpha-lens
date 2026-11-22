from sqlalchemy import Column, DateTime, Integer, String

from .base import Base, utcnow_naive


class SensAnnouncement(Base):
    """A SENS announcement for a JSE share the user holds.

    Filled daily from each held company's Moneyweb page
    (services/holding_sens.py) and deduped by `source_url`. Drives the
    `type="sens"` notifications and the Portfolio page's suspended /
    corporate-action notices.
    """

    __tablename__ = "sens_announcements"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(10), index=True, nullable=False)
    name = Column(String(300))
    headline = Column(String(500), nullable=False)
    published_at = Column(DateTime, index=True)   # naive UTC
    source_url = Column(String(1000), unique=True, nullable=False)
    # Key dates/prices pulled from the announcement text (services/sens_facts),
    # " · "-joined; only fetched for decision-type announcements.
    facts = Column(String(500))
    # When a notification was raised for it - NULL = never (e.g. skipped by
    # the first-fetch backfill); open corporate actions are caught up once.
    alerted_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow_naive, index=True)

    def __repr__(self):
        return f"<SensAnnouncement({self.symbol}: {self.headline[:40]})>"
