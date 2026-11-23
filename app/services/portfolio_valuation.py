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


def dividend_income_by_holding(db: Session, holdings: list) -> dict[int, tuple]:
    """Dividend income = Σ amount × shares actually held before each ex-date.

    Position size changes across the period: current quantity × every
    dividend since purchase overstates top-ups and drops income from shares
    already sold. The quantity held just before each ex-date comes from the
    transaction log (a buy ON the ex-date earns nothing). A holding with no
    logged events falls back to its current quantity since purchase_date.
    Returns {holding.id: (income, per_share_sum_since_purchase)}.
    """
    if not holdings:
        return {}
    dividends_by_symbol: dict[str, list] = {}
    for sym, amount, ex_date in db.query(
        Dividend.symbol, Dividend.amount, Dividend.ex_dividend_date
    ).filter(Dividend.symbol.in_({h.symbol for h in holdings})).all():
        dividends_by_symbol.setdefault(sym, []).append((ex_date, amount or 0.0))
    if not dividends_by_symbol:
        return {}

    # Position-size timelines from the transaction log - one query for all
    # holdings. Keyed (portfolio_id, symbol, account_type) like holdings.
    from app.models.transaction import PortfolioTransaction
    from app.services.transactions import BUY, SPLIT, XFER_IN

    txs_by_key: dict[tuple, list] = {}
    for portfolio_id, sym, acct, tx_type, qty, when in db.query(
        PortfolioTransaction.portfolio_id, PortfolioTransaction.symbol,
        PortfolioTransaction.account_type, PortfolioTransaction.transaction_type,
        PortfolioTransaction.quantity, PortfolioTransaction.transacted_at,
    ).filter(
        PortfolioTransaction.portfolio_id.in_({h.portfolio_id for h in holdings}),
        PortfolioTransaction.symbol.in_(dividends_by_symbol.keys()),
    ).all():
        txs_by_key.setdefault((portfolio_id, sym, acct), []).append((tx_type, qty, when))

    incomes: dict[int, float] = {}
    for holding in holdings:
        dividends = dividends_by_symbol.get(holding.symbol, [])
        if not dividends:
            continue
        events = sorted(
            txs_by_key.get(
                (holding.portfolio_id, holding.symbol, holding.account_type or "ZAR"), []
            ),
            key=lambda e: (e[2] is None, e[2]),
        )
        total = 0.0
        per_share = 0.0
        for ex_date, amount in dividends:
            if ex_date is None or not amount:
                continue
            # Buying on the ex-date earns nothing - strictly after purchase.
            if holding.purchase_date is not None and ex_date <= holding.purchase_date:
                continue
            per_share += amount
            if events:
                # split rows store shares added (negative for consolidations)
                qty = max(0.0, sum(
                    qty if tx_type in (BUY, XFER_IN, SPLIT) else -qty
                    for tx_type, qty, when in events
                    if when is not None and when < ex_date
                ))
            else:
                qty = holding.quantity or 0.0
            total += qty * amount
        if total or per_share:
            incomes[holding.id] = (total, per_share)
    return incomes


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

    # Dividend income per holding - same attribution as the Dividends page:
    # shares actually held just before each ex-date (see
    # dividend_income_by_holding).
    incomes = dividend_income_by_holding(db, jse_holdings)

    for holding in jse_holdings:
        latest = latest_by_symbol.get(holding.symbol)
        new_price = latest.close_price if latest else None
        if new_price is not None:
            new_value = holding.quantity * new_price
            # Assign only when the value moved - otherwise every Portfolio
            # render is dirty and commits a no-op write.
            if holding.current_price != new_price or holding.current_value != new_value:
                holding.current_price = new_price
                holding.current_value = new_value
                cost_basis = holding.quantity * holding.purchase_price
                if cost_basis:
                    holding.gain_loss = new_value - cost_basis
                    holding.gain_loss_percentage = (holding.gain_loss / cost_basis) * 100
                else:
                    holding.gain_loss = new_value
                    holding.gain_loss_percentage = 0.0
        # No stored bar: keep whatever the last EasyEquities sync set - a
        # missing/failed JSE price fetch must not wipe last-known values.

        income = incomes.get(holding.id, (0.0, 0.0))[0]
        if holding.total_dividends_received != income:
            holding.total_dividends_received = income

    if db.new or db.dirty:
        db.commit()


def update_portfolio_totals(db: Session, portfolio: Portfolio, holdings: list) -> None:
    """Recompute portfolio-level summary metrics and record today's snapshot."""
    total_value = sum(h.current_value or 0.0 for h in holdings)
    total_cost = sum(h.quantity * h.purchase_price for h in holdings)

    # Assign only on change - a same-value render must not dirty the
    # portfolio row and commit on a read path.
    if (
        portfolio.current_value != total_value
        or portfolio.total_gain_loss != total_value - total_cost
    ):
        portfolio.current_value = total_value
        portfolio.total_gain_loss = total_value - total_cost
        portfolio.total_gain_loss_percentage = (
            (portfolio.total_gain_loss / total_cost) * 100 if total_cost else 0.0
        )

    if db.new or db.dirty:
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
            # Assign only on change - an identical re-render must not dirty
            # the row and commit a no-op write.
            if row.total_value != total_value or row.total_cost != total_cost:
                row.total_value = total_value
                row.total_cost = total_cost
            if crypto_value is not None and row.crypto_value != crypto_value:
                row.crypto_value = crypto_value
            if bullion_value is not None and row.bullion_value != bullion_value:
                row.bullion_value = bullion_value
            if db.new or db.dirty:
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
