from sqlalchemy import Column, Integer, String, DateTime
from .base import Base, utcnow_naive


class UpcomingListing(Base):
    """A JSE IPO / new listing spotted in Moneyweb SENS announcements.

    Genuine company IPOs are rare (~5-10/year); rows are deduped by
    `source_url` so the daily SENS scan only records each announcement once.
    `status` moves "announced" -> "listed" once Yahoo starts serving bars
    for the ticker.
    """

    __tablename__ = "upcoming_listings"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(10), index=True)        # JSE ticker when SENS shows one
    name = Column(String(300))                     # company name from the announcement
    headline = Column(String(500))                 # SENS headline text
    status = Column(String(20), default="announced")  # announced | listed
    sens_published_at = Column(DateTime)
    source_url = Column(String(1000), unique=True)  # dedup key
    created_at = Column(DateTime, default=utcnow_naive, index=True)
    updated_at = Column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)

    def __repr__(self):
        return f"<UpcomingListing(symbol={self.symbol}, status={self.status})>"
