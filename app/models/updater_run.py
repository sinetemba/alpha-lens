from sqlalchemy import Column, Integer, String, DateTime, Boolean
from .base import Base, utcnow_naive


class UpdaterRun(Base):
    """Heartbeat: one row per completed headless-updater job run.

    The dashboard reads the newest row per job to warn when the updater
    hasn't run (or last failed) - there is no other link between the two
    processes.
    """

    __tablename__ = "updater_runs"

    id = Column(Integer, primary_key=True, index=True)
    job_name = Column(String(50), nullable=False, index=True)
    success = Column(Boolean, nullable=False)
    finished_at = Column(DateTime, nullable=False, index=True, default=utcnow_naive)
    detail = Column(String(500))
