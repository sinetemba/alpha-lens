"""Tests for net worth (JSE + crypto + bullion) and its daily snapshots."""
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.models.crypto import CryptoHolding
from app.models.krugerrand import KrugerrandHolding
from app.models.portfolio import Portfolio, PortfolioHolding
from app.models.portfolio_snapshot import PortfolioSnapshot
from app.models.stock import Stock, StockPrice
from app.services import net_worth, portfolio_valuation
from app.services.net_worth import NetWorth, bullion_value, compute_net_worth, crypto_value


def _coin(source, coin_id, qty):
    return SimpleNamespace(source=source, coin_id=coin_id, quantity=qty)


# --- pure valuation ------------------------------------------------------

def test_crypto_value_luno_bid_and_coingecko():
    holdings = [_coin("luno", "bitcoin", 0.001), _coin("manual", "solana", 2.0)]
    value = crypto_value(
        holdings,
        markets={"bitcoin": {"current_price": 1_000_000.0}, "solana": {"current_price": 1_200.0}},
        luno_prices={"XBT": 1_050_000.0},
    )
    assert value == pytest.approx(1050.0 + 2400.0)


def test_crypto_value_unpriced_is_none_and_empty_is_zero():
    assert crypto_value([_coin("manual", "solana", 1.0)], markets={}, luno_prices={}) is None
    assert crypto_value([], markets={}, luno_prices={}) == 0.0


def test_bullion_value_by_fine_ounces():
    bars = [SimpleNamespace(metal="XAU", fine_oz_per_unit=0.1, quantity=2),
            SimpleNamespace(metal="XAG", fine_oz_per_unit=1.0, quantity=10)]
    assert bullion_value(bars, {"XAU": 64_000.0, "XAG": 777.0}) == pytest.approx(12_800.0 + 7_770.0)
    assert bullion_value(bars, None) is None
    assert bullion_value([], None) == 0.0


def test_bullion_value_missing_metal_is_none_not_partial():
    """A held metal with no spot must not be silently excluded from the total."""
    bars = [SimpleNamespace(metal="XAU", fine_oz_per_unit=1.0, quantity=1),
            SimpleNamespace(metal="XAG", fine_oz_per_unit=1.0, quantity=5)]
    assert bullion_value(bars, {"XAU": 64_000.0}) is None


def test_net_worth_total_and_missing():
    worth = NetWorth(jse=179_545.45, crypto=None, bullion=0.0)
    assert worth.total == pytest.approx(179_545.45)
    assert worth.missing == ["crypto"]


def test_compute_net_worth_degrades_on_network_failure(db_session, monkeypatch):
    db_session.add(CryptoHolding(coin_id="bitcoin", symbol="BTC", name="Bitcoin", quantity=0.01))
    db_session.add(KrugerrandHolding(product="gold_krugerrand", metal="XAU", label="1 oz",
                                     fine_oz_per_unit=1.0, quantity=1))
    db_session.commit()
    monkeypatch.setattr(net_worth, "fetch_crypto_prices", MagicMock(side_effect=RuntimeError("down")))
    monkeypatch.setattr(net_worth.YFinanceCollector, "get_metal_spots_zar", lambda self: {"XAU": 64_000.0})

    worth = compute_net_worth(db_session, jse_value=1000.0)

    assert worth.jse == 1000.0 and worth.crypto is None
    assert worth.bullion == pytest.approx(64_000.0)


# --- snapshots -----------------------------------------------------------

def test_jse_only_revaluation_keeps_recorded_crypto(db_session):
    db_session.add(Portfolio(id=1, name="P"))
    db_session.commit()
    day = date(2026, 11, 19)
    portfolio_valuation.record_snapshot(db_session, 1, 1000.0, 900.0, day=day,
                                        crypto_value=2658.65, bullion_value=0.0)

    # The Portfolio page later revalues the JSE side only.
    portfolio_valuation.record_snapshot(db_session, 1, 1100.0, 900.0, day=day)

    row = db_session.query(PortfolioSnapshot).one()
    assert row.total_value == pytest.approx(1100.0)
    assert row.crypto_value == pytest.approx(2658.65)
    assert row.bullion_value == 0.0


def test_updater_snapshot_records_net_worth(db_session, monkeypatch):
    from app.services import updater

    stock = Stock(symbol="SOL", name="Sasol")
    portfolio = Portfolio(name="P")
    db_session.add_all([stock, portfolio])
    db_session.flush()
    db_session.add(StockPrice(stock_id=stock.id, symbol="SOL", timestamp=datetime(2026, 11, 18), close_price=100.0))
    db_session.add(PortfolioHolding(portfolio_id=portfolio.id, stock_id=stock.id, symbol="SOL",
                                    account_type="Manual", quantity=10, purchase_price=90.0,
                                    purchase_date=datetime(2026, 1, 1)))
    db_session.commit()
    luno = MagicMock(enabled=True)
    monkeypatch.setattr(updater, "LunoCollector", lambda: luno)
    # The snapshot also gates on EasyEquities - keep it off so the test can't
    # hit the real account when the developer's .env has credentials.
    monkeypatch.setattr(updater, "EasyEquitiesCollector",
                        lambda: SimpleNamespace(enabled=False))
    sync = MagicMock(return_value=SimpleNamespace())
    monkeypatch.setattr(updater, "sync_luno_holdings", sync)
    monkeypatch.setattr(updater, "compute_net_worth",
                        lambda db, jse_value: NetWorth(jse=jse_value, crypto=2658.65, bullion=None))

    updater._snapshot_portfolio(db_session)

    sync.assert_called_once()
    row = db_session.query(PortfolioSnapshot).one()
    assert row.total_value == pytest.approx(1000.0)
    assert row.total_cost == pytest.approx(900.0)
    assert row.crypto_value == pytest.approx(2658.65)
    assert row.bullion_value is None   # unpriced, not zero
