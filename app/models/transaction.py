from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from .base import Base, utcnow_naive


class PortfolioTransaction(Base):
    """A buy/sell event on a holding - the audit trail behind realized P&L.

    Written by the manual add/remove paths and by EasyEquities sync
    reconciliation (positions the broker no longer reports are recorded as
    sells). Sells carry `realized_gain` computed against the stored cost
    basis (average cost, not per-lot - matches how holdings aggregate).
    """

    __tablename__ = "portfolio_transactions"

    id = Column(Integer, primary_key=True, index=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"), nullable=False, index=True)
    stock_id = Column(Integer, ForeignKey("stocks.id"))
    symbol = Column(String(10), index=True, nullable=False)
    account_type = Column(String(50), default="ZAR")

    transaction_type = Column(String(10), nullable=False)  # "buy" | "sell"
    quantity = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    value = Column(Float, nullable=False)      # quantity * price
    realized_gain = Column(Float)              # sells only: value - cost basis

    # Who wrote this row - "manual" (dashboard add/remove) or "easyequities"
    # (sync saw the position open / disappear).
    source = Column(String(20), default="manual")

    transacted_at = Column(DateTime, nullable=False, default=utcnow_naive)
    created_at = Column(DateTime, default=utcnow_naive, index=True)

    portfolio = relationship("Portfolio")
    stock = relationship("Stock")

    def __repr__(self):
        return (
            f"<PortfolioTransaction({self.transaction_type} {self.quantity} "
            f"{self.symbol} @ {self.price})>"
        )
