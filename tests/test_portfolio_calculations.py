"""Tests for portfolio gain/loss math in app/dashboard/portfolio.py."""
from datetime import datetime, timezone

import pytest

from app.dashboard.home import _get_latest_and_previous_prices
from app.dashboard.portfolio import (
    EXCLUDED_ACCOUNT_TYPES,
    EXCLUDED_PORTFOLIO_SYMBOLS,
    _get_group_latest_and_previous_prices,
    _normalize_account_type,
    _sync_holding_from_easyequities,
    _update_holding_values,
    _update_portfolio_totals,
)
from app.models.portfolio import Portfolio, PortfolioHolding
from app.models.stock import Stock, StockPrice


@pytest.fixture(autouse=True)
def _clear_price_caches():
    _get_latest_and_previous_prices.clear()
    _get_group_latest_and_previous_prices.clear()
    yield
    _get_latest_and_previous_prices.clear()
    _get_group_latest_and_previous_prices.clear()


def _add_stock_with_price(db, symbol, close_price):
    stock = Stock(symbol=symbol, name=symbol)
    db.add(stock)
    db.flush()
    db.add(StockPrice(stock_id=stock.id, symbol=symbol, timestamp=datetime(2024, 1, 2), close_price=close_price))
    db.commit()
    return stock


def _add_portfolio(db):
    portfolio = Portfolio(name="Test Portfolio")
    db.add(portfolio)
    db.commit()
    return portfolio


def _add_holding(db, portfolio, stock, quantity, purchase_price, account_type="ZAR"):
    holding = PortfolioHolding(
        portfolio_id=portfolio.id,
        stock_id=stock.id,
        symbol=stock.symbol,
        account_type=account_type,
        quantity=quantity,
        purchase_price=purchase_price,
        purchase_date=datetime(2023, 1, 1),
    )
    db.add(holding)
    db.commit()
    return holding


def test_normalize_account_type_strips_easyequities_prefix():
    assert _normalize_account_type("EasyEquities TFSA") == "TFSA"
    assert _normalize_account_type("EasyEquities ZAR") == "ZAR"


def test_normalize_account_type_passes_through_plain_name():
    assert _normalize_account_type("ZAR") == "ZAR"


def test_normalize_account_type_defaults_for_none_or_empty():
    assert _normalize_account_type(None) == "ZAR"
    assert _normalize_account_type("") == "ZAR"


def test_normalize_account_type_strips_surrounding_whitespace():
    assert _normalize_account_type("  EasyEquities TFSA  ") == "TFSA"


def test_update_holding_values_computes_gain(db_session):
    stock = _add_stock_with_price(db_session, "NPN", 120.0)
    portfolio = _add_portfolio(db_session)
    holding = _add_holding(db_session, portfolio, stock, quantity=10, purchase_price=100.0)

    _update_holding_values(db_session, [holding])

    assert holding.current_price == 120.0
    assert holding.current_value == 1200.0
    assert holding.gain_loss == pytest.approx(200.0)
    assert holding.gain_loss_percentage == pytest.approx(20.0)


def test_update_holding_values_computes_loss(db_session):
    stock = _add_stock_with_price(db_session, "NPN", 80.0)
    portfolio = _add_portfolio(db_session)
    holding = _add_holding(db_session, portfolio, stock, quantity=10, purchase_price=100.0)

    _update_holding_values(db_session, [holding])

    assert holding.gain_loss == pytest.approx(-200.0)
    assert holding.gain_loss_percentage == pytest.approx(-20.0)


def test_update_holding_values_zero_cost_basis_avoids_division_by_zero(db_session):
    stock = _add_stock_with_price(db_session, "NPN", 120.0)
    portfolio = _add_portfolio(db_session)
    holding = _add_holding(db_session, portfolio, stock, quantity=10, purchase_price=0.0)

    _update_holding_values(db_session, [holding])

    assert holding.current_value == 1200.0
    assert holding.gain_loss == pytest.approx(1200.0)
    assert holding.gain_loss_percentage == 0.0


def test_update_holding_values_missing_price_clears_computed_fields(db_session):
    stock = Stock(symbol="NPN", name="NPN")
    db_session.add(stock)
    db_session.flush()
    portfolio = _add_portfolio(db_session)
    holding = _add_holding(db_session, portfolio, stock, quantity=10, purchase_price=100.0)

    _update_holding_values(db_session, [holding])

    assert holding.current_price is None
    assert holding.current_value is None
    assert holding.gain_loss is None
    assert holding.gain_loss_percentage is None


def test_update_holding_values_empty_list_is_noop(db_session):
    # Must not raise even though there is nothing to batch-fetch prices for.
    _update_holding_values(db_session, [])


def test_update_portfolio_totals_sums_across_holdings(db_session):
    npn = _add_stock_with_price(db_session, "NPN", 120.0)
    cfr = _add_stock_with_price(db_session, "CFR", 180.0)
    portfolio = _add_portfolio(db_session)
    h1 = _add_holding(db_session, portfolio, npn, quantity=10, purchase_price=100.0)
    h2 = _add_holding(db_session, portfolio, cfr, quantity=5, purchase_price=200.0)
    _update_holding_values(db_session, [h1, h2])

    _update_portfolio_totals(db_session, portfolio, [h1, h2])

    assert portfolio.current_value == pytest.approx(1200.0 + 900.0)
    assert portfolio.total_gain_loss == pytest.approx(200.0 - 100.0)
    total_cost = 10 * 100.0 + 5 * 200.0
    assert portfolio.total_gain_loss_percentage == pytest.approx(
        (portfolio.total_gain_loss / total_cost) * 100
    )


def test_update_portfolio_totals_zero_cost_avoids_division_by_zero(db_session):
    portfolio = _add_portfolio(db_session)
    portfolio.current_value = 0.0

    _update_portfolio_totals(db_session, portfolio, [])

    assert portfolio.current_value == 0.0
    assert portfolio.total_gain_loss == 0.0
    assert portfolio.total_gain_loss_percentage == 0.0


def test_sync_holding_from_easyequities_creates_new_holding(db_session):
    portfolio = _add_portfolio(db_session)
    ee_holding = {
        "symbol": "NPN",
        "name": "Naspers",
        "account_name": "EasyEquities ZAR",
        "shares": 10.0,
        "purchase_price": 100.0,
        "current_price": 120.0,
        "current_value": 1200.0,
    }

    _sync_holding_from_easyequities(db_session, portfolio.id, ee_holding)
    db_session.commit()

    holding = db_session.query(PortfolioHolding).filter(PortfolioHolding.symbol == "NPN").one()
    assert holding.account_type == "ZAR"
    assert holding.quantity == 10.0
    assert holding.current_value == 1200.0
    assert holding.gain_loss == pytest.approx(200.0)
    assert holding.gain_loss_percentage == pytest.approx(20.0)


def test_sync_holding_from_easyequities_updates_existing_holding(db_session):
    stock = Stock(symbol="NPN", name="Naspers")
    db_session.add(stock)
    db_session.flush()
    portfolio = _add_portfolio(db_session)
    holding = _add_holding(db_session, portfolio, stock, quantity=5, purchase_price=90.0)

    ee_holding = {
        "symbol": "NPN",
        "name": "Naspers",
        "account_name": "EasyEquities ZAR",
        "shares": 10.0,
        "purchase_price": 100.0,
        "current_price": 120.0,
        "current_value": 1200.0,
    }
    _sync_holding_from_easyequities(db_session, portfolio.id, ee_holding)
    db_session.commit()

    assert db_session.query(PortfolioHolding).count() == 1
    assert holding.quantity == 10.0
    assert holding.purchase_price == 100.0
    assert holding.gain_loss == pytest.approx(200.0)


@pytest.mark.parametrize(
    "ee_holding",
    [
        {"symbol": "", "account_name": "EasyEquities ZAR", "shares": 10.0, "purchase_price": 1, "current_price": 1, "current_value": 1},
        {"symbol": next(iter(EXCLUDED_PORTFOLIO_SYMBOLS)), "account_name": "EasyEquities ZAR", "shares": 10.0, "purchase_price": 1, "current_price": 1, "current_value": 1},
        {"symbol": "NPN", "account_name": "EasyEquities " + next(iter(EXCLUDED_ACCOUNT_TYPES)), "shares": 10.0, "purchase_price": 1, "current_price": 1, "current_value": 1},
        {"symbol": "NPN", "account_name": "EasyEquities ZAR", "shares": 0.0, "purchase_price": 1, "current_price": 1, "current_value": 1},
    ],
)
def test_sync_holding_from_easyequities_skips_excluded_entries(db_session, ee_holding):
    portfolio = _add_portfolio(db_session)

    _sync_holding_from_easyequities(db_session, portfolio.id, ee_holding)
    db_session.commit()

    assert db_session.query(PortfolioHolding).count() == 0
