"""Transaction log + realized P&L for portfolio holdings.

One writer for every buy/sell event so the manual dashboard paths and the
EasyEquities sync record the same shape. Callers add rows inside their own
transaction - no commit here (except `delete_transaction`, a user action).

Realized P&L is approximate by construction: holdings aggregate at average
cost (not per lot), EasyEquities doesn't expose trade dates or fill prices,
so EE events are dated at sync time and priced from EE's reported values.

transaction_type values (String(10)):
  buy / sell              - position opened/increased, closed/reduced
  xfer_in / xfer_out      - same position moved between accounts (no P&L)
"""
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.base import utcnow_naive
from app.models.transaction import PortfolioTransaction

BUY, SELL, XFER_IN, XFER_OUT = "buy", "sell", "xfer_in", "xfer_out"


def record_transaction(
    db: Session,
    *,
    portfolio_id: int,
    stock_id: int | None,
    symbol: str,
    account_type: str | None,
    transaction_type: str,
    quantity: float,
    price: float,
    realized_gain: float | None = None,
    source: str = "manual",
    transacted_at: datetime | None = None,
) -> PortfolioTransaction:
    """Append a buy/sell event (flush-only; the caller commits)."""
    tx = PortfolioTransaction(
        portfolio_id=portfolio_id,
        stock_id=stock_id,
        symbol=symbol,
        account_type=account_type or "ZAR",
        transaction_type=transaction_type,
        quantity=quantity,
        price=price,
        value=quantity * price,
        realized_gain=realized_gain,
        source=source,
        transacted_at=transacted_at or utcnow_naive(),
    )
    db.add(tx)
    return tx


def record_sale(
    db: Session,
    holding,
    sell_price: float,
    source: str,
    quantity: float | None = None,
    sold_at: datetime | None = None,
) -> PortfolioTransaction:
    """Log a sell + realized gain (whole holding unless `quantity` is given)."""
    qty = holding.quantity if quantity is None else quantity
    return record_transaction(
        db,
        portfolio_id=holding.portfolio_id,
        stock_id=holding.stock_id,
        symbol=holding.symbol,
        account_type=holding.account_type,
        transaction_type=SELL,
        quantity=qty,
        price=sell_price,
        realized_gain=qty * (sell_price - (holding.purchase_price or 0.0)),
        source=source,
        transacted_at=sold_at,
    )


def delete_transaction(db: Session, portfolio_id: int, transaction_id: int) -> bool:
    """Delete one logged event (user correction). Commits."""
    tx = db.query(PortfolioTransaction).filter(
        PortfolioTransaction.id == transaction_id,
        PortfolioTransaction.portfolio_id == portfolio_id,
    ).first()
    if tx is None:
        return False
    db.delete(tx)
    db.commit()
    return True


def backfill_opening_buys(db: Session) -> int:
    """Give every holding with no logged events an opening buy.

    Holdings that predate the transaction log otherwise have sells with no
    matching buy, and "Total buys" understates. Dated at the holding's
    purchase_date and priced at its stored average cost. Flush-only.
    """
    from app.models.portfolio import PortfolioHolding

    logged = {
        (pid, sym, acct)
        for pid, sym, acct in db.query(
            PortfolioTransaction.portfolio_id,
            PortfolioTransaction.symbol,
            PortfolioTransaction.account_type,
        ).distinct().all()
    }
    added = 0
    for h in db.query(PortfolioHolding).all():
        acct = h.account_type or "ZAR"
        if (h.portfolio_id, h.symbol, acct) in logged or not h.quantity:
            continue
        record_transaction(
            db,
            portfolio_id=h.portfolio_id, stock_id=h.stock_id, symbol=h.symbol,
            account_type=acct, transaction_type=BUY,
            quantity=h.quantity, price=h.purchase_price or 0.0,
            source="backfill",
            transacted_at=h.purchase_date or h.created_at,
        )
        added += 1
    return added
