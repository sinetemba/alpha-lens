"""Tests for portfolio gain/loss math in app/dashboard/portfolio.py."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.dashboard.home import _get_latest_and_previous_prices
from app.dashboard.portfolio import (
    EXCLUDED_ACCOUNT_TYPES,
    EXCLUDED_PORTFOLIO_SYMBOLS,
    _get_group_latest_and_previous_prices,
    _holding_action,
    _holding_price_stats,
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


# --- per-account sync (dashboard "Sync Holdings" button) --------------------

def _fake_collector(holdings):
    return SimpleNamespace(
        get_holdings=lambda account_id: holdings,
        last_error=None,
    )


def _usd_position():
    """get_holdings returns no account_name - the service must tag it."""
    return {
        "symbol": "AAPL", "name": "Apple",
        "shares": 2.0,
        "purchase_value": 200.0, "purchase_price": 100.0,
        "current_price": 110.0, "current_value": 220.0,
        "currency": "USD",
    }


def test_sync_account_holdings_tags_account_and_converts_usd(db_session, monkeypatch):
    """A USD account synced via the button used to write dollars into the
    Rand fields under account_type "ZAR" - it must tag the real account and
    convert at USD/ZAR like the full sync."""
    from app.services import easyequities_sync

    portfolio = _add_portfolio(db_session)
    monkeypatch.setattr(
        easyequities_sync.YFinanceCollector, "get_usd_zar",
        lambda self: 18.0,
    )

    synced, skipped = easyequities_sync.sync_account_holdings(
        db_session, portfolio.id,
        _fake_collector([_usd_position()]),
        "acct-1", "EasyEquities USD",
    )

    assert synced == 1 and skipped == 0
    holding = db_session.query(PortfolioHolding).one()
    assert holding.account_type == "USD"
    assert holding.purchase_price == pytest.approx(1800.0)
    assert holding.current_price == pytest.approx(1980.0)
    assert holding.current_value == pytest.approx(3960.0)


def test_sync_account_holdings_without_rate_writes_nothing(db_session, monkeypatch):
    """No USD/ZAR rate -> keep last synced values, never store dollars."""
    from app.services import easyequities_sync

    portfolio = _add_portfolio(db_session)
    monkeypatch.setattr(
        easyequities_sync.YFinanceCollector, "get_usd_zar",
        lambda self: None,
    )

    synced, skipped = easyequities_sync.sync_account_holdings(
        db_session, portfolio.id,
        _fake_collector([_usd_position()]),
        "acct-1", "EasyEquities USD",
    )

    assert synced == 0 and skipped == 1
    assert db_session.query(PortfolioHolding).count() == 0


def test_sync_account_holdings_jse_account_passthrough(db_session, monkeypatch):
    from app.services import easyequities_sync

    portfolio = _add_portfolio(db_session)
    zar_calls = []
    monkeypatch.setattr(
        easyequities_sync.YFinanceCollector, "get_usd_zar",
        lambda self: zar_calls.append(1) or 18.0,
    )
    position = _usd_position()
    position["currency"] = "ZAR"

    synced, skipped = easyequities_sync.sync_account_holdings(
        db_session, portfolio.id,
        _fake_collector([position]),
        "acct-1", "EasyEquities ZAR",
    )

    assert synced == 1 and skipped == 0 and not zar_calls
    holding = db_session.query(PortfolioHolding).one()
    assert holding.account_type == "ZAR"
    assert holding.purchase_price == pytest.approx(100.0)


# --- Action column ---------------------------------------------------------

def _stats(**kw):
    base = dict(price=1.0, low_52w=0.8, sma20=1.0, sma50=1.0, mom_3m=0.0)
    base.update(kw)
    return base


def _watch(**kw):
    base = dict(stop_loss=None, target_price=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_action_stop_loss_wins_over_everything():
    assert _holding_action(-80.0, _stats(), _watch(stop_loss=1.1)) == "Sell — stop hit"
    assert _holding_action(50.0, _stats(), _watch(stop_loss=1.1)) == "Sell — stop hit"


def test_action_target_hit_trims_a_winner_not_a_loser():
    assert _holding_action(10.0, _stats(), _watch(target_price=0.9)) == "Trim — target hit"
    assert _holding_action(-30.0, _stats(), _watch(target_price=0.9)) != "Trim — target hit"


def test_action_loser_still_falling_is_a_sell():
    assert _holding_action(-30.0, _stats(price=0.9, sma20=1.0, mom_3m=-10.0)) == "Sell — still falling"
    # Flat at the 52w low with no bounce is dead, not "hold and hope".
    assert _holding_action(-80.0, _stats(price=0.80, low_52w=0.80, sma20=0.81, mom_3m=0.0)) == "Sell — still falling"


def test_action_small_loss_in_a_dip_is_not_a_sell():
    # -10% + falling: above the -15% sell threshold, so hold and watch.
    assert _holding_action(-10.0, _stats(price=0.9, sma20=1.0, mom_3m=-10.0)) == "Hold — watch"


def test_action_loser_recovering_is_a_buy():
    assert _holding_action(-20.0, _stats(price=1.05, sma20=1.0, mom_3m=5.0)) == "Buy — recovering"


def test_action_winner_in_uptrend_is_a_buy():
    assert _holding_action(20.0, _stats(price=1.2, sma50=1.0, mom_3m=8.0)) == "Buy — uptrend"


def test_action_plain_hold_and_none_gain_loss():
    assert _holding_action(5.0, _stats()) == "Hold"
    assert _holding_action(None, _stats()) == "Hold"


def test_holding_price_stats_computes_series_stats(db_session):
    stock = Stock(symbol="TST", name="T")
    db_session.add(stock)
    db_session.flush()
    base = datetime.now(timezone.utc).replace(tzinfo=None)
    for i in range(80):
        db_session.add(StockPrice(
            stock_id=stock.id, symbol="TST",
            timestamp=base - timedelta(days=80 - i), close_price=100.0 + i,
        ))
    db_session.commit()

    stats = _holding_price_stats(db_session, ["TST", "MISSING"])
    s = stats["TST"]
    assert s["price"] == 179.0 and s["low_52w"] == 100.0
    assert s["sma20"] == pytest.approx(169.5)  # mean of closes 160..179
    assert s["mom_3m"] > 0
    assert "MISSING" not in stats

# --- Dividend income attribution (per ex-date quantity) --------------------

def test_dividend_income_uses_quantity_held_before_each_ex_date(db_session):
    """Top-ups after the first ex-date must not be credited with income they
    never earned (the old code used current quantity x all dividends)."""
    from app.models.dividend import Dividend
    from app.models.transaction import PortfolioTransaction
    from app.services.portfolio_valuation import dividend_income_by_holding

    stock = Stock(symbol="SBK", name="SBK")
    db_session.add(stock)
    db_session.flush()
    portfolio = _add_portfolio(db_session)
    holding = _add_holding(db_session, portfolio, stock, quantity=12, purchase_price=40.0)
    db_session.add_all([
        Dividend(stock_id=stock.id, symbol="SBK", amount=1.0,
                 ex_dividend_date=datetime(2023, 3, 1)),
        Dividend(stock_id=stock.id, symbol="SBK", amount=1.0,
                 ex_dividend_date=datetime(2023, 6, 1)),
    ])
    db_session.add_all([
        PortfolioTransaction(
            portfolio_id=portfolio.id, stock_id=stock.id, symbol="SBK",
            account_type="ZAR", transaction_type="buy", quantity=10, price=40,
            value=400.0, transacted_at=datetime(2023, 1, 1)),
        PortfolioTransaction(
            portfolio_id=portfolio.id, stock_id=stock.id, symbol="SBK",
            account_type="ZAR", transaction_type="buy", quantity=5, price=40,
            value=200.0, transacted_at=datetime(2023, 4, 1)),
        PortfolioTransaction(
            portfolio_id=portfolio.id, stock_id=stock.id, symbol="SBK",
            account_type="ZAR", transaction_type="sell", quantity=3, price=50,
            value=150.0, transacted_at=datetime(2023, 9, 1)),
    ])
    db_session.commit()

    income, per_share = dividend_income_by_holding(db_session, [holding])[holding.id]
    # 10 held before the March ex-date, 15 before the June one.
    assert income == pytest.approx(25.0)
    assert per_share == pytest.approx(2.0)


def test_dividend_income_excludes_ex_date_on_purchase_day(db_session):
    """Buying on the ex-date earns nothing - requires strictly-before."""
    from app.models.dividend import Dividend
    from app.models.transaction import PortfolioTransaction
    from app.services.portfolio_valuation import dividend_income_by_holding

    stock = Stock(symbol="SBK", name="SBK")
    db_session.add(stock)
    db_session.flush()
    portfolio = _add_portfolio(db_session)
    holding = _add_holding(db_session, portfolio, stock, quantity=10, purchase_price=40.0)
    db_session.add(Dividend(
        stock_id=stock.id, symbol="SBK", amount=1.0,
        ex_dividend_date=datetime(2023, 1, 1),  # == purchase_date
    ))
    db_session.add(PortfolioTransaction(
        portfolio_id=portfolio.id, stock_id=stock.id, symbol="SBK",
        account_type="ZAR", transaction_type="buy", quantity=10, price=40,
        value=400.0, transacted_at=datetime(2023, 1, 1),
    ))
    db_session.commit()

    assert dividend_income_by_holding(db_session, [holding]) == {}


def test_dividend_income_without_log_falls_back_to_current_quantity(db_session):
    from app.models.dividend import Dividend
    from app.services.portfolio_valuation import dividend_income_by_holding

    stock = Stock(symbol="SBK", name="SBK")
    db_session.add(stock)
    db_session.flush()
    portfolio = _add_portfolio(db_session)
    holding = _add_holding(db_session, portfolio, stock, quantity=10, purchase_price=40.0)
    db_session.add(Dividend(
        stock_id=stock.id, symbol="SBK", amount=1.0,
        ex_dividend_date=datetime(2023, 3, 1),
    ))
    db_session.commit()

    income, _per_share = dividend_income_by_holding(db_session, [holding])[holding.id]
    assert income == pytest.approx(10.0)
