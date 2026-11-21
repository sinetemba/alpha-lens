"""Streamlit-free EasyEquities holdings sync.

Extracted from app.dashboard.portfolio so the headless updater can run the
same sync without importing Streamlit. The dashboard wraps
``sync_portfolio_from_easyequities`` to stash the raw holdings for its debug
expander; the updater calls it directly.
"""
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.exc import IntegrityError

from app.utils.formatting import format_price
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.portfolio import (
    PortfolioHolding,
    EXCLUDED_ACCOUNT_TYPES,
    EXCLUDED_PORTFOLIO_SYMBOLS,
    is_jse_tracked_account,
)
from app.models.stock import Stock, StockPrice
from app.collectors.easyequities import EasyEquitiesCollector
from app.collectors.yfinance import YFinanceCollector


def _normalize_account_type(account_type: str | None) -> str:
    """Strip the 'EasyEquities ' prefix and whitespace so account names
    align with the portfolio's account_type values (e.g. 'ZAR', 'TFSA')."""
    if not account_type:
        return "ZAR"
    return account_type.strip().replace("EasyEquities ", "")


def _ee_reconcile_units(
    db: Session, ee_holding: dict, account_type: str, latest_closes: dict | None = None
) -> None:
    """Cross-check EasyEquities' implied price against the stored JSE close.

    EE's ZAR amounts are never rescaled: every time this used to "fix" EE
    to match the stored close (SKA, KBO) it was the stored series that was
    100x off - Yahoo serves stretches of .JO history in Rand instead of
    cents. On a ~100x disagreement EE's values are kept and a system
    notification names the symbol so the stored series gets repaired
    (`run_updater.py --normalize-closes`). USD-denominated amounts on
    JSE-tracked accounts are flagged and skipped.

    `latest_closes` is an optional {symbol: latest close} map - syncs prefetch
    it in one grouped query instead of querying per holding.
    """
    symbol = ee_holding.get("symbol", "?")
    price = ee_holding.get("current_price") or 0.0
    if price <= 0 or not is_jse_tracked_account(account_type):
        return
    currency = (ee_holding.get("currency") or "").upper()
    if currency and currency != "ZAR":
        logger.warning(
            f"EasyEquities {symbol}: {currency}-denominated amount on "
            f"JSE-tracked account {account_type} - skipping stored-close check"
        )
        return
    if latest_closes is not None:
        stored_close = latest_closes.get(symbol)
    else:
        stored = db.query(StockPrice.close_price).filter(
            StockPrice.symbol == symbol
        ).order_by(StockPrice.timestamp.desc()).first()
        stored_close = stored.close_price if stored else None
    if not stored_close:
        return
    ratio = max(price, stored_close) / min(price, stored_close)
    if ratio <= 3.0:
        return
    unit_mismatch = 50 <= ratio <= 200
    logger.warning(
        f"EasyEquities {symbol}: implied price {format_price(price)} vs stored close "
        f"{format_price(stored_close)} ({ratio:.0f}x) - keeping EE values"
        + (" - stored series looks 100x off" if unit_mismatch else "")
    )
    if unit_mismatch:
        # Notified after the sync commits (create_notification commits).
        ee_holding["unit_conflict"] = (price, stored_close, ratio)


def _notify_unit_conflicts(db: Session, holdings) -> None:
    """System notification per holding _ee_reconcile_units flagged ~100x off."""
    for ee_holding in holdings:
        conflict = ee_holding.get("unit_conflict")
        if not conflict:
            continue
        price, stored_close, ratio = conflict
        symbol = ee_holding.get("symbol", "?")
        try:
            from app.notifications.notification_service import NotificationService
            NotificationService(db).create_notification(
                notification_type="system",
                title=f"Price unit conflict: {symbol}",
                message=(
                    f"EasyEquities prices {symbol} at {format_price(price)} but the stored close is "
                    f"{format_price(stored_close)} (~{ratio:.0f}x apart). EasyEquities' value is kept; "
                    "the stored price history is probably in the wrong unit - run "
                    "`python scripts\\run_updater.py --normalize-closes` (backup first)."
                ),
                stock_symbol=symbol,
            )
        except Exception as e:
            logger.warning(f"Could not record unit-conflict notification for {symbol}: {e}")


def _sync_holding_from_easyequities(
    db: Session,
    portfolio_id: int,
    ee_holding: dict,
    stocks_by_symbol: dict | None = None,
    holdings_by_key: dict | None = None,
    latest_closes: dict | None = None,
    opened: dict | None = None,
):
    """Create or update a holding from parsed EasyEquities data.

    Logs the position change to the transaction history: a new position is
    a buy (recorded in `opened` so reconciliation can recognise an
    account-to-account move), a quantity increase a buy of the delta, a
    decrease a partial sell at EE's current price.

    Does NOT commit - callers commit once per sync so a mid-loop failure does
    not persist a half-synced portfolio.
    """
    from app.services.transactions import BUY, record_transaction

    symbol = ee_holding["symbol"]
    account_type = _normalize_account_type(ee_holding.get("account_name"))
    if (
        not symbol
        or symbol in EXCLUDED_PORTFOLIO_SYMBOLS
        or account_type in EXCLUDED_ACCOUNT_TYPES
        or ee_holding["shares"] <= 0
    ):
        return

    _ee_reconcile_units(db, ee_holding, account_type, latest_closes)

    stock = (stocks_by_symbol or {}).get(symbol)
    if stock is None:
        stock = db.query(Stock).filter(Stock.symbol == symbol).first()
    if not stock:
        stock = Stock(symbol=symbol, name=ee_holding.get("name") or symbol)
        db.add(stock)
        db.flush()
        if stocks_by_symbol is not None:
            stocks_by_symbol[symbol] = stock

    holding = (holdings_by_key or {}).get((symbol, account_type))
    if holding is None:
        holding = db.query(PortfolioHolding).filter(
            PortfolioHolding.portfolio_id == portfolio_id,
            PortfolioHolding.symbol == symbol,
            PortfolioHolding.account_type == account_type,
        ).first()

    is_new = holding is None
    if is_new:
        holding = PortfolioHolding(
            portfolio_id=portfolio_id,
            stock_id=stock.id,
            symbol=symbol,
            account_type=account_type,
            purchase_date=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(holding)
        # Log the position opening so the transaction history / realized P&L
        # has a buy to match a later sell. transacted_at is sync time - the
        # real trade date isn't exposed by the EE holdings endpoint.
        tx = record_transaction(
            db,
            portfolio_id=portfolio_id, stock_id=stock.id, symbol=symbol,
            account_type=account_type, transaction_type=BUY,
            quantity=ee_holding["shares"], price=ee_holding["purchase_price"],
            source="easyequities",
        )
        if opened is not None:
            opened.setdefault(symbol, []).append(tx)
    else:
        _log_quantity_change(db, holding, ee_holding, opened)

    # EasyEquities is treated as the source of truth for quantity, cost basis, and market value on sync
    holding.quantity = ee_holding["shares"]
    holding.purchase_price = ee_holding["purchase_price"]
    holding.current_price = ee_holding["current_price"]
    holding.current_value = ee_holding["current_value"]
    holding.gain_loss = holding.current_value - (holding.quantity * holding.purchase_price)
    cost_basis = holding.quantity * holding.purchase_price
    holding.gain_loss_percentage = (
        (holding.gain_loss / cost_basis) * 100
        if cost_basis else 0.0
    )
    # Always update the timestamp so the UI reflects the latest sync time,
    # even when EasyEquities returns identical values.
    holding.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)


# Quantity moves smaller than this (relative) are float noise, not trades.
QTY_TOLERANCE = 1e-6


def _log_quantity_change(db: Session, holding, ee_holding: dict, opened: dict | None) -> None:
    """Log a top-up (buy of the delta) or partial sale of an existing holding.

    Must run before the holding is overwritten with EE's values: the delta
    and the partial sale's realized gain use the previous quantity / average
    cost. A top-up buy is also added to `opened` so reconciliation can pair
    a full transfer into an existing position (transfer, not sell+rebuy).
    """
    from app.services.transactions import BUY, record_sale, record_transaction

    old_qty = holding.quantity or 0.0
    new_qty = ee_holding["shares"]
    delta = new_qty - old_qty
    if abs(delta) <= QTY_TOLERANCE * max(1.0, old_qty):
        return
    if delta > 0:
        # Price of the added units = change in total cost / units added
        # (EE reports average cost, so the new lot's cost is the difference).
        added_cost = new_qty * ee_holding["purchase_price"] - old_qty * (holding.purchase_price or 0.0)
        price = added_cost / delta if added_cost > 0 else ee_holding["current_price"]
        tx = record_transaction(
            db,
            portfolio_id=holding.portfolio_id, stock_id=holding.stock_id,
            symbol=holding.symbol, account_type=holding.account_type,
            transaction_type=BUY, quantity=delta, price=price,
            source="easyequities",
        )
        if opened is not None:
            opened.setdefault(holding.symbol, []).append(tx)
    else:
        # No EE price on suspended/unpriced instruments - fall back to the
        # last known stored price like the removal path does.
        sell_px = (
            ee_holding["current_price"]
            or holding.current_price
            or holding.purchase_price
        )
        if sell_px:
            record_sale(
                db, holding, sell_px,
                source="easyequities", quantity=-delta,
            )


def _match_transfer(opened: dict, holding) -> object | None:
    """The buy opened this sync for the same symbol in ANOTHER account with
    (about) the same quantity - i.e. the removed holding was moved, not sold."""
    for tx in opened.get(holding.symbol, []):
        if (
            tx.account_type != holding.account_type
            and holding.quantity
            and abs(tx.quantity - holding.quantity) / holding.quantity <= 0.01
        ):
            return tx
    return None


def sync_portfolio_from_easyequities(
    db: Session, portfolio_id: int, collector: EasyEquitiesCollector
) -> tuple[int | None, list]:
    """Sync holdings from EasyEquities.

    Returns (synced_count, raw_holdings); synced_count is None when the
    upstream fetch failed (raw is then whatever came back, possibly []).
    """
    from app.services.source_health import record_source
    ee_holdings = collector.get_all_holdings()
    record_source(db, "easyequities", ee_holdings is not None,
                  None if ee_holdings is not None else (getattr(collector, "last_error", None) or "fetch failed"))
    if ee_holdings is None:
        return None, []

    aggregated_holdings = {}
    seen_account_types = set()
    for ee_holding in ee_holdings:
        symbol = ee_holding["symbol"]
        account_name = _normalize_account_type(ee_holding.get("account_name"))
        if account_name not in EXCLUDED_ACCOUNT_TYPES:
            seen_account_types.add(account_name)
        if (
            not symbol
            or symbol in EXCLUDED_PORTFOLIO_SYMBOLS
            or account_name in EXCLUDED_ACCOUNT_TYPES
            or ee_holding["shares"] <= 0
        ):
            continue
        key = (symbol, account_name)
        current = aggregated_holdings.setdefault(key, {
            "symbol": symbol,
            "name": ee_holding.get("name") or symbol,
            "account_name": account_name,
            "currency": ee_holding.get("currency"),
            "shares": 0.0,
            "purchase_value": 0.0,
            "current_value": 0.0,
        })
        current["shares"] += ee_holding["shares"]
        current["purchase_value"] += ee_holding["purchase_value"]
        current["current_value"] += ee_holding["current_value"]

    # Prefetch lookup maps so each holding sync does not fire two queries.
    if aggregated_holdings:
        stocks_by_symbol = {
            s.symbol: s for s in db.query(Stock).filter(
                Stock.symbol.in_([h["symbol"] for h in aggregated_holdings.values()])
            ).all()
        }
        holdings_by_key = {
            (h.symbol, h.account_type): h for h in db.query(PortfolioHolding).filter(
                PortfolioHolding.portfolio_id == portfolio_id,
            ).all()
        }
        # One grouped latest-close lookup for the EE units check, instead of
        # one ORDER BY DESC query per synced holding.
        latest_ts = db.query(
            StockPrice.symbol.label("symbol"),
            func.max(StockPrice.timestamp).label("max_ts"),
        ).filter(
            StockPrice.symbol.in_([h["symbol"] for h in aggregated_holdings.values()])
        ).group_by(StockPrice.symbol).subquery()
        latest_closes = dict(db.query(StockPrice.symbol, StockPrice.close_price).join(
            latest_ts,
            (StockPrice.symbol == latest_ts.c.symbol)
            & (StockPrice.timestamp == latest_ts.c.max_ts),
        ).all())
    else:
        stocks_by_symbol = {}
        holdings_by_key = {}
        latest_closes = {}

    # USD-account values arrive in dollars: convert to Rand once here so
    # portfolio totals, snapshots, net worth and transaction P&L never sum
    # mixed currencies. Without a rate the holding is kept dollar-valued
    # (still wrong-unit in totals, but visible and retried next sync).
    usd_zar = None
    if any(
        not is_jse_tracked_account(h["account_name"])
        for h in aggregated_holdings.values()
    ):
        usd_zar = YFinanceCollector().get_usd_zar()
        if usd_zar:
            logger.info(f"Converting USD-account EasyEquities values at USD/ZAR {usd_zar:.4f}")

    opened: dict = {}
    for ee_holding in aggregated_holdings.values():
        if not is_jse_tracked_account(ee_holding["account_name"]):
            if usd_zar and (ee_holding.get("currency") or "USD") == "USD":
                ee_holding["purchase_value"] *= usd_zar
                ee_holding["current_value"] *= usd_zar
            elif not usd_zar:
                logger.warning(
                    "USD/ZAR rate unavailable - USD-account holding "
                    f"{ee_holding['symbol']} keeps dollar values this sync"
                )
        ee_holding["purchase_price"] = ee_holding["purchase_value"] / ee_holding["shares"]
        ee_holding["current_price"] = ee_holding["current_value"] / ee_holding["shares"]
        _sync_holding_from_easyequities(
            db, portfolio_id, ee_holding, stocks_by_symbol, holdings_by_key, latest_closes,
            opened,
        )
    try:
        db.commit()
    except IntegrityError:
        # A concurrent sync (Portfolio page vs updater / another session)
        # upserted the same (symbol, account_type) rows first - they carry
        # equivalent data, keep them instead of crashing.
        db.rollback()
        opened = {}  # rolled back - nothing to pair transfers against
        logger.warning("EasyEquities sync hit a concurrent write; keeping the other writer's rows")
    _notify_unit_conflicts(db, aggregated_holdings.values())

    # EasyEquities is the source of truth: remove holdings under the account
    # types we just synced that it no longer reports (fully sold/delisted
    # positions). Otherwise they linger in the DB forever and get retried on
    # every price refresh, which is slow for delisted/wrong-exchange tickers.
    # Guard: only reconcile accounts that reported at least one holding this
    # sync. An upstream glitch that returns an account with an empty holdings
    # list must not wipe (and log fake sales for) everything in it; a really
    # emptied account is surfaced instead, for the user to clear by hand.
    reporting_accounts = {acct for (_symbol, acct) in aggregated_holdings}
    silent_accounts = seen_account_types - reporting_accounts
    if silent_accounts:
        _notify_silent_accounts(db, portfolio_id, silent_accounts)
    if reporting_accounts:
        from app.services.transactions import XFER_IN, XFER_OUT, record_sale, record_transaction
        stale_holdings = db.query(PortfolioHolding).filter(
            PortfolioHolding.portfolio_id == portfolio_id,
            PortfolioHolding.account_type.in_(reporting_accounts),
        ).all()
        removed = []
        for holding in stale_holdings:
            if (holding.symbol, holding.account_type) not in aggregated_holdings:
                moved_to = _match_transfer(opened, holding)
                if moved_to is not None:
                    # Same position re-appeared under another account: a
                    # transfer, not a sale + rebuy (no realized P&L).
                    moved_to.transaction_type = XFER_IN
                    record_transaction(
                        db,
                        portfolio_id=holding.portfolio_id, stock_id=holding.stock_id,
                        symbol=holding.symbol, account_type=holding.account_type,
                        transaction_type=XFER_OUT, quantity=holding.quantity,
                        price=holding.purchase_price or 0.0, source="easyequities",
                    )
                    opened[holding.symbol].remove(moved_to)
                else:
                    # Log the sale before the row is gone: realized P&L uses
                    # EasyEquities' last reported price as the exit price.
                    sell_price = holding.current_price
                    if not sell_price and holding.quantity:
                        sell_price = (holding.current_value or 0.0) / holding.quantity
                    if sell_price:
                        record_sale(db, holding, sell_price, source="easyequities")
                db.delete(holding)
                removed.append(f"{holding.symbol} ({holding.account_type})")
        if removed:
            try:
                db.commit()
            except IntegrityError:
                # Concurrent writer raced the delete - keep their rows.
                db.rollback()
                logger.warning("EasyEquities removal hit a concurrent write")
                removed = []
            if removed:
                logger.info(f"Removed {len(removed)} stale holding(s) no longer reported by EasyEquities")
            # Removed positions used to vanish with no trace - an in-app
            # notification records what was dropped. The symbol list goes in
            # the title because dedup keys on (type, title, symbol): two
            # DIFFERENT same-count removals in one window must both alert.
            try:
                from app.notifications.notification_service import NotificationService
                NotificationService(db).create_notification(
                    notification_type="system",
                    title=f"EE sync removed: {', '.join(sorted(removed))}"[:450],
                    message=(
                        "No longer reported by EasyEquities (sold, transferred or "
                        f"delisted) and removed from the portfolio: {', '.join(sorted(removed))}"
                    ),
                )
            except Exception as e:
                logger.warning(f"Could not record holding-removal notification: {e}")

    return len(aggregated_holdings), ee_holdings


def _notify_silent_accounts(db: Session, portfolio_id: int, accounts: set) -> None:
    """Warn about accounts EE listed with no holdings while we still hold rows.

    Not auto-reconciled (could be a transient empty response); the user
    removes the holdings on the Portfolio page if they really sold out.
    """
    kept = db.query(PortfolioHolding.symbol, PortfolioHolding.account_type).filter(
        PortfolioHolding.portfolio_id == portfolio_id,
        PortfolioHolding.account_type.in_(accounts),
    ).all()
    if not kept:
        return
    names = ", ".join(sorted(f"{s} ({a})" for s, a in kept))
    logger.warning(
        f"EasyEquities reported no holdings for {sorted(accounts)} - kept {len(kept)} holding(s)"
    )
    try:
        from app.notifications.notification_service import NotificationService
        NotificationService(db).create_notification(
            notification_type="system",
            title=f"EasyEquities reported an empty account: {', '.join(sorted(accounts))}",
            message=(
                "EasyEquities listed the account with no holdings, so these were kept "
                f"rather than deleted: {names}. If you sold everything there, remove "
                "them on the Portfolio page; if not, the next sync will confirm them."
            ),
        )
    except Exception as e:
        logger.warning(f"Could not record empty-account notification: {e}")
