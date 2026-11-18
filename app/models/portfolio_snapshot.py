from sqlalchemy import Column, Integer, Float, Date, DateTime, ForeignKey, UniqueConstraint
from .base import Base, utcnow_naive


class PortfolioSnapshot(Base):
    """One row per portfolio per day: total value and cost basis.

    `Portfolio.current_value` is a moving point; these rows are the series
    behind the Portfolio page's value-over-time chart. The day's row is
    overwritten in place on every revaluation, so it ends up holding the
    day's last value.
    """

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "snapshot_date", name="uq_portfolio_snapshots_portfolio_date"),
    )

    id = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"), nullable=False, index=True)
    snapshot_date = Column(Date, nullable=False)  # exchange-local trading date
    total_value = Column(Float, nullable=False)
    total_cost = Column(Float, nullable=False)
    created_at = Column(DateTime, default=utcnow_naive)
    updated_at = Column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)
