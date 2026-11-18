"""
Portfolio revaluation and daily value snapshots (Streamlit-free).

Shared by the Portfolio page and the headless updater, so the value-history
series fills in on days the dashboard is never opened.
"""

from datetime import datetime, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.config.settings import settings
from app.models.dividend import Dividend
from app.models.portfolio import (
    EXCLUDED_ACCOUNT_TYPES,
    EXCLUDED_PORTFOLIO_SYMBOLS,
    Portfolio,
    PortfolioHolding,
    is_jse_tracked_account,
)
from app.models.portfolio_snapshot import PortfolioSnapshot
from app.services.price_queries import get_latest_and_previous_prices


def valued_holdings(db: Session, portfolio_id: int) -> list:
    """Holdings that count towards portfolio value (same filters as the page)."""
    return db.query(PortfolioHolding).options(
        joinedload(PortfolioHolding.stock)
    ).filter(
        PortfolioHolding.portfolio_id == portfolio_id,
        ~PortfolioHolding.symbol.in_(EXCLUDED_PORTFOLIO_SYMBOLS),
        ~PortfolioHolding.account_type.in_(EXCLUDED_ACCOUNT_TYPES),
    ).order_by(PortfolioHolding.purchase_date.asc()).all()


def update_holding_values(
    db: Session,
    holdings: list,
    price_lookup: Callable = get_latest_and_previous_prices,
) -> None:
    """Update current values for holdings from the latest stored prices.

    One batched latest-price query plus one commit, instead of a query and a
    commit per holding.

    Only JSE-tracked accounts are priced: stored prices are JSE quotes, so a
    USD-account holding (e.g. AAPL under a bare US symbol) would either be
    valued with the wrong instrument or have its EasyEquities-synced values
    wiped to None. Those holdings keep whatever the sync last set.
    """
    jse_holdings = [h for h in holdings if is_jse_tracked_account(h.account_type)]
    if not jse_holdings:
        return

    symbols = [h.symbol for h in jse_holdings]
    latest_by_symbol = {
        symbol: snapshots[0]
        for symbol, snapshots in price_lookup(db, symbols).items()
    }

    # Dividends received per share since each holding's purchase date - same
    # rule as the Dividends page income estimate.
    dividends_by_symbol: dict[str, list] = {}
    for sym, amount, ex_date in db.query(
        Dividend.symbol, Dividend.amount, Dividend.ex_dividend_date
    ).filter(Dividend.symbol.in_(symbols)).all():
        dividends_by_symbol.setdefault(sym, []).append((ex_date, amount or 0.0))

    for holding in jse_holdings:
        latest = latest_by_symbol.get(holding.symbol)
        if latest and latest.close_price is not None:
            holding.current_price = latest.close_price
            holding.current_value = holding.quantity * latest.close_price
            cost_basis = holding.quantity * holding.purchase_price
            if cost_basis:
                holding.gain_loss = holding.current_value - cost_basis
                holding.gain_loss_percentage = (holding.gain_loss / cost_basis) * 100
            else:
                holding.gain_loss = holding.current_value
                holding.gain_loss_percentage = 0.0
        # No stored bar: keep whatever the last EasyEquities sync set - a
        # missing/failed JSE price fetch must not wipe last-known values.

        holding.total_dividends_received = (holding.quantity or 0.0) * sum(
            amount
            for ex_date, amount in dividends_by_symbol.get(holding.symbol, [])
            if holding.purchase_date is None
            or (ex_date is not None and ex_date >= holding.purchase_date)
        )

    db.commit()


def update_portfolio_totals(db: Session, portfolio: Portfolio, holdings: list) -> None:
    """Recompute portfolio-level summary metrics and record today's snapshot."""
    total_value = sum(h.current_value or 0.0 for h in holdings)
    total_cost = sum(h.quantity * h.purchase_price for h in holdings)

    portfolio.current_value = total_value
    portfolio.total_gain_loss = total_value - total_cost
    portfolio.total_gain_loss_percentage = (
        (portfolio.total_gain_loss / total_cost) * 100 if total_cost else 0.0
    )

    db.commit()
    if holdings:
        record_snapshot(db, portfolio.id, total_value, total_cost)


def _today() -> "datetime.date":
    return datetime.now(timezone.utc).astimezone(ZoneInfo(settings.scheduler_timezone)).date()


def record_snapshot(
    db: Session, portfolio_id: int, total_value: float, total_cost: float, day=None,
    crypto_value: Optional[float] = None, bullion_value: Optional[float] = None,
) -> None:
    """Upsert the (portfolio, day) snapshot. Best-effort: never fails the caller.

    `crypto_value`/`bullion_value` are only written when given - None means
    "not measured this time", not zero.
    """
    day = day or _today()
    for attempt in range(2):
        try:
            row = db.query(PortfolioSnapshot).filter(
                PortfolioSnapshot.portfolio_id == portfolio_id,
                PortfolioSnapshot.snapshot_date == day,
            ).first()
            if row is None:
                row = PortfolioSnapshot(portfolio_id=portfolio_id, snapshot_date=day)
                db.add(row)
            row.total_value = total_value
            row.total_cost = total_cost
            if crypto_value is not None:
                row.crypto_value = crypto_value
            if bullion_value is not None:
                row.bullion_value = bullion_value
            db.commit()
            return
        except IntegrityError:
            # The dashboard and updater inserted the same day concurrently -
            # retry once as an update of the row the other process wrote.
            db.rollback()
        except Exception as e:
            db.rollback()
            logger.warning(f"Could not record portfolio snapshot: {e}")
            return


def revalue_portfolio(db: Session) -> Optional[float]:
    """Revalue the portfolio from stored prices and snapshot it (updater path).

    Returns the new total value, or None when there is no portfolio.
    """
    portfolio = db.query(Portfolio).first()
    if not portfolio:
        return None
    holdings = valued_holdings(db, portfolio.id)
    update_holding_values(db, holdings)
    update_portfolio_totals(db, portfolio, holdings)
    return portfolio.current_value
