from sqlalchemy import Column, String, DateTime
from .base import Base, utcnow_naive


class SchemaMeta(Base):
    """One-time data-migration markers.

    ``create_all``/``_migrate_schema`` run on every startup of every process
    (dashboard AND updater daemon). Data-repair passes that scan or rewrite
    existing rows record a key here once applied so later startups skip them
    with a single primary-key lookup instead of an O(table) scan.
    """

    __tablename__ = "schema_meta"

    key = Column(String(100), primary_key=True)
    applied_at = Column(DateTime, default=utcnow_naive, nullable=False)

    def __repr__(self):
        return f"<SchemaMeta(key='{self.key}')>"
