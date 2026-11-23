"""Corporate actions: rescale portfolio holdings when a stock splits.

A split changes a holding's quantity and per-share cost basis, not its
value. EasyEquities-synced accounts would self-heal on the next sync (EE
reports the new share count - but it would be logged as a fake "top-up
buy"); Manual holdings would stay wrong by the split factor forever. The
updater applies Yahoo's split events to every holding directly, and the
logged ``split`` transaction doubles as the idempotence key so a weekly
window overlap can't double-apply.
"""
from datetime import date

from loguru import logger
from sqlalchemy.orm import Session

from app.collectors.yfinance import YFinanceCollector
from app.models.portfolio import EXCLUDED_PORTFOLIO_SYMBOLS, PortfolioHolding


def apply_recent_splits(db: Session, symbols: list, since: date) -> list:
    """Apply Yahoo split events dated after `since` to matching holdings.

    Returns the symbols a split was applied for. Idempotent: a (symbol,
    ex-date) with an existing ``split`` transaction is skipped.
    """
    from app.models.transaction import PortfolioTransaction
    from app.services.transactions import SPLIT, record_transaction

    collector = YFinanceCollector()
    applied = []
    for symbol in symbols:
        if symbol in EXCLUDED_PORTFOLIO_SYMBOLS:
            continue
        try:
            splits = collector.get_splits(symbol)
        except Exception as e:
            logger.warning(f"Split check failed for {symbol}: {e}")
            continue
        if splits is None or getattr(splits, "empty", True):
            continue
        holdings = db.query(PortfolioHolding).filter(
            PortfolioHolding.symbol == symbol
        ).all()
        if not holdings:
            continue

        for raw_date, ratio in splits.items():
            try:
                ex_ts = raw_date.to_pydatetime().replace(tzinfo=None)
                factor = float(ratio)
            except (AttributeError, TypeError, ValueError):
                continue
            if factor <= 0 or ex_ts.date() <= since:
                continue
            already = db.query(PortfolioTransaction).filter(
                PortfolioTransaction.symbol == symbol,
                PortfolioTransaction.transaction_type == SPLIT,
                PortfolioTransaction.transacted_at == ex_ts,
            ).first()
            if already:
                continue

            for holding in holdings:
                old_qty = holding.quantity or 0.0
                holding.quantity = old_qty * factor
                if holding.purchase_price:
                    holding.purchase_price = holding.purchase_price / factor
                if holding.current_price:
                    holding.current_price = holding.current_price / factor
                if holding.current_price:
                    holding.current_value = holding.quantity * holding.current_price
                # quantity = shares added (negative for a consolidation) so
                # quantity-timeline math treats it like a buy/return.
                record_transaction(
                    db,
                    portfolio_id=holding.portfolio_id, stock_id=holding.stock_id,
                    symbol=symbol, account_type=holding.account_type,
                    transaction_type=SPLIT, quantity=holding.quantity - old_qty,
                    price=0.0, source="corporate_action", transacted_at=ex_ts,
                )
                logger.warning(
                    f"Applied {factor:g}:1 split to {symbol} "
                    f"({holding.account_type}): {old_qty:g} -> {holding.quantity:g} shares"
                )
            applied.append(symbol)

    if applied:
        db.commit()
        try:
            from app.notifications.notification_service import NotificationService
            NotificationService(db).create_notification(
                notification_type="system",
                title=f"Stock split applied: {', '.join(sorted(applied))}"[:450],
                message=(
                    "Quantity and per-share cost basis were rescaled for a stock "
                    f"split: {', '.join(sorted(applied))}. Transaction history and "
                    "realized P&L before the split stay on the old basis."
                ),
            )
        except Exception as e:
            logger.warning(f"Could not record split notification: {e}")
    return applied
