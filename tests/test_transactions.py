"""Tests for the transaction log / realized P&L (services/transactions.py)."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.models.portfolio import Portfolio, PortfolioHolding
from app.models.stock import Stock
from app.models.transaction import PortfolioTransaction
from app.services.easyequities_sync import sync_portfolio_from_easyequities
from app.services.transactions import record_sale, record_transaction


def _portfolio(db):
    portfolio = Portfolio(name="P")
    db.add(portfolio)
    db.flush()
    return portfolio


def _holding(db, portfolio, stock, **kwargs):
    defaults = dict(
        portfolio_id=portfolio.id, stock_id=stock.id, symbol=stock.symbol,
        account_type="ZAR", quantity=10.0, purchase_price=100.0,
        purchase_date=datetime(2024, 1, 1), current_price=120.0,
        current_value=1200.0,
    )
    defaults.update(kwargs)
    holding = PortfolioHolding(**defaults)
    db.add(holding)
    db.flush()
    return holding


def test_record_sale_computes_realized_gain(db_session):
    portfolio = _portfolio(db_session)
    stock = Stock(symbol="SOL", name="Sasol")
    db_session.add(stock)
    db_session.flush()
    holding = _holding(db_session, portfolio, stock)

    record_sale(db_session, holding, sell_price=120.0, source="manual")
    db_session.commit()

    tx = db_session.query(PortfolioTransaction).one()
    assert tx.transaction_type == "sell"
    assert tx.value == pytest.approx(1200.0)
    assert tx.realized_gain == pytest.approx(200.0)  # 10 * (120 - 100)
    assert tx.source == "manual"


def test_record_sale_negative_gain(db_session):
    portfolio = _portfolio(db_session)
    stock = Stock(symbol="LOS", name="Loser")
    db_session.add(stock)
    db_session.flush()
    holding = _holding(db_session, portfolio, stock)

    record_sale(db_session, holding, sell_price=80.0, source="manual")

    tx = db_session.query(PortfolioTransaction).one()
    assert tx.realized_gain == pytest.approx(-200.0)


def _ee_collector(holdings):
    return SimpleNamespace(get_all_holdings=lambda: holdings, last_error=None)


def test_ee_sync_records_buy_for_new_holding(db_session):
    portfolio = _portfolio(db_session)
    db_session.commit()
    collector = _ee_collector([{
        "symbol": "NPN", "name": "Naspers", "account_name": "ZAR",
        "shares": 5.0, "purchase_value": 500.0, "current_value": 600.0,
        "currency": "ZAR",
    }])

    synced, _ = sync_portfolio_from_easyequities(db_session, portfolio.id, collector)

    assert synced == 1
    tx = db_session.query(PortfolioTransaction).filter_by(symbol="NPN").one()
    assert tx.transaction_type == "buy"
    assert tx.quantity == pytest.approx(5.0)
    assert tx.price == pytest.approx(100.0)   # purchase_value / shares
    assert tx.source == "easyequities"


def test_ee_sync_records_sell_when_position_disappears(db_session):
    portfolio = _portfolio(db_session)
    stock = Stock(symbol="OLD", name="OldCo")
    db_session.add(stock)
    db_session.flush()
    _holding(db_session, portfolio, stock, symbol="OLD",
             current_price=120.0, current_value=1200.0)
    # One position EE still reports so reconciliation has an account scope.
    db_session.commit()
    collector = _ee_collector([{
        "symbol": "NPN", "name": "Naspers", "account_name": "ZAR",
        "shares": 5.0, "purchase_value": 500.0, "current_value": 600.0,
        "currency": "ZAR",
    }])

    synced, _ = sync_portfolio_from_easyequities(db_session, portfolio.id, collector)

    assert synced == 1
    assert db_session.query(PortfolioHolding).filter_by(symbol="OLD").count() == 0
    sell = db_session.query(PortfolioTransaction).filter_by(
        symbol="OLD", transaction_type="sell").one()
    assert sell.price == pytest.approx(120.0)
    assert sell.realized_gain == pytest.approx(200.0)
    assert sell.source == "easyequities"
    buy = db_session.query(PortfolioTransaction).filter_by(
        symbol="NPN", transaction_type="buy").one()
    assert buy.source == "easyequities"


# --- quantity changes, transfers, empty accounts, corrections --------------

def _ee(symbol, account, shares, purchase_value, current_value):
    return {"symbol": symbol, "name": symbol, "account_name": account, "shares": shares,
            "purchase_value": purchase_value, "current_value": current_value,
            "currency": "ZAR"}


def _stock(db, symbol):
    stock = Stock(symbol=symbol, name=symbol)
    db.add(stock)
    db.flush()
    return stock


def test_ee_partial_sale_is_logged(db_session):
    portfolio = _portfolio(db_session)
    _holding(db_session, portfolio, _stock(db_session, "SOL"), symbol="SOL")  # 10 @ 100
    db_session.commit()

    sync_portfolio_from_easyequities(
        db_session, portfolio.id, _ee_collector([_ee("SOL", "ZAR", 6.0, 600.0, 720.0)]))

    sell = db_session.query(PortfolioTransaction).filter_by(transaction_type="sell").one()
    assert sell.quantity == pytest.approx(4.0)
    assert sell.price == pytest.approx(120.0)
    assert sell.realized_gain == pytest.approx(80.0)  # 4 * (120 - 100)
    assert db_session.query(PortfolioHolding).one().quantity == pytest.approx(6.0)


def test_ee_top_up_logs_buy_of_delta_at_lot_cost(db_session):
    portfolio = _portfolio(db_session)
    _holding(db_session, portfolio, _stock(db_session, "SOL"), symbol="SOL")  # 10 @ 100 = 1000
    db_session.commit()

    # 15 shares, total cost 1600 -> the 5 new shares cost 600 (R120 each).
    sync_portfolio_from_easyequities(
        db_session, portfolio.id, _ee_collector([_ee("SOL", "ZAR", 15.0, 1600.0, 1800.0)]))

    buy = db_session.query(PortfolioTransaction).filter_by(transaction_type="buy").one()
    assert buy.quantity == pytest.approx(5.0)
    assert buy.price == pytest.approx(120.0)


def test_ee_unchanged_quantity_logs_nothing(db_session):
    portfolio = _portfolio(db_session)
    _holding(db_session, portfolio, _stock(db_session, "SOL"), symbol="SOL")
    db_session.commit()

    sync_portfolio_from_easyequities(
        db_session, portfolio.id, _ee_collector([_ee("SOL", "ZAR", 10.0, 1000.0, 1300.0)]))

    assert db_session.query(PortfolioTransaction).count() == 0


def test_ee_account_move_is_transfer_not_sale(db_session):
    portfolio = _portfolio(db_session)
    _holding(db_session, portfolio, _stock(db_session, "SOL"), symbol="SOL", account_type="ZAR")
    db_session.commit()

    sync_portfolio_from_easyequities(
        db_session, portfolio.id,
        _ee_collector([_ee("SOL", "TFSA", 10.0, 1000.0, 1200.0),
                       # ZAR still reports something, so it is reconciled.
                       _ee("NPN", "ZAR", 1.0, 100.0, 100.0)]))

    types = {(t.account_type, t.transaction_type, t.realized_gain)
             for t in db_session.query(PortfolioTransaction).filter_by(symbol="SOL")}
    assert types == {("ZAR", "xfer_out", None), ("TFSA", "xfer_in", None)}
    assert db_session.query(PortfolioHolding).filter_by(symbol="SOL").one().account_type == "TFSA"


def test_ee_empty_account_keeps_holdings_and_warns(db_session):
    """An account reported with no holdings is not wiped (transient glitch)."""
    from app.models.notification import Notification
    portfolio = _portfolio(db_session)
    _holding(db_session, portfolio, _stock(db_session, "SOL"), symbol="SOL", account_type="TFSA")
    db_session.commit()

    # TFSA is listed but empty (a zero-share row marks the account as seen).
    holdings = [_ee("NPN", "ZAR", 1.0, 100.0, 100.0), _ee("SOL", "TFSA", 0.0, 0.0, 0.0)]

    sync_portfolio_from_easyequities(db_session, portfolio.id, _ee_collector(holdings))

    assert db_session.query(PortfolioHolding).filter_by(symbol="SOL").count() == 1
    assert db_session.query(PortfolioTransaction).filter_by(symbol="SOL").count() == 0
    note = db_session.query(Notification).filter_by(type="system").one()
    assert "TFSA" in note.title


def test_backfill_opening_buys_only_for_unlogged_holdings(db_session):
    from app.services.transactions import backfill_opening_buys
    portfolio = _portfolio(db_session)
    _holding(db_session, portfolio, _stock(db_session, "SOL"), symbol="SOL")
    logged = _holding(db_session, portfolio, _stock(db_session, "NPN"), symbol="NPN")
    record_transaction(db_session, portfolio_id=portfolio.id, stock_id=logged.stock_id,
                       symbol="NPN", account_type="ZAR", transaction_type="buy",
                       quantity=10.0, price=100.0)
    db_session.commit()

    assert backfill_opening_buys(db_session) == 1
    tx = db_session.query(PortfolioTransaction).filter_by(symbol="SOL").one()
    assert tx.source == "backfill"
    assert tx.transacted_at == datetime(2024, 1, 1)
    assert backfill_opening_buys(db_session) == 0


def test_remove_by_mistake_deletes_manual_buys_without_sale(db_session):
    from app.dashboard.portfolio import _remove_holding
    portfolio = _portfolio(db_session)
    stock = _stock(db_session, "SOL")
    _holding(db_session, portfolio, stock, symbol="SOL", account_type="Manual")
    record_transaction(db_session, portfolio_id=portfolio.id, stock_id=stock.id,
                       symbol="SOL", account_type="Manual", transaction_type="buy",
                       quantity=10.0, price=100.0)
    db_session.commit()

    _remove_holding(db_session, portfolio.id, "SOL", "Manual", record_sale=False)

    assert db_session.query(PortfolioHolding).count() == 0
    assert db_session.query(PortfolioTransaction).count() == 0


def test_remove_as_sale_uses_given_price_and_date(db_session):
    from app.dashboard.portfolio import _remove_holding
    portfolio = _portfolio(db_session)
    _holding(db_session, portfolio, _stock(db_session, "SOL"), symbol="SOL", account_type="Manual")
    db_session.commit()

    _remove_holding(db_session, portfolio.id, "SOL", "Manual",
                    sell_price=150.0, sold_at=datetime(2026, 11, 1))

    sell = db_session.query(PortfolioTransaction).one()
    assert sell.price == pytest.approx(150.0)
    assert sell.realized_gain == pytest.approx(500.0)  # 10 * (150 - 100)
    assert sell.transacted_at == datetime(2026, 11, 1)
