"""Tests for price formatting, symbol search and portfolio snapshots."""
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.collectors import yfinance as yf_module
from app.collectors.yfinance import YFinanceCollector
from app.dashboard.utils import format_price, match_symbols, price_column_format
from app.models.portfolio import Portfolio, PortfolioHolding
from app.models.portfolio_snapshot import PortfolioSnapshot
from app.models.stock import Stock, StockPrice
from app.notifications.notification_service import NotificationService
from app.services import portfolio_valuation
from app.config.settings import settings


# --- price formatting ----------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    (98.5, "R 98.50"),
    (1234.567, "R 1,234.57"),
    (0.0085, "R 0.0085"),   # SKA: "R 0.01" would hide the real price
    (None, "N/A"),
    (float("nan"), "N/A"),
])
def test_format_price(value, expected):
    assert format_price(value) == expected


def test_price_column_format_switches_on_any_sub_rand_value():
    assert price_column_format([98.5, 250.0]) == "R %.2f"
    assert price_column_format([98.5, None], [0.0085]) == "R %.4f"
    assert price_column_format([-0.002], signed=True) == "R %+.4f"
    assert price_column_format([0.0, None]) == "R %.2f"  # zero/empty isn't sub-Rand


# --- symbol search -------------------------------------------------------

def test_match_symbols_orders_exact_local_then_remote(db_session):
    db_session.add_all([Stock(symbol="NPN", name="Naspers Ltd"), Stock(symbol="SOL", name="Sasol Limited")])
    db_session.commit()

    assert list(match_symbols(db_session, "npn", remote=[])) == ["NPN"]
    by_name = match_symbols(db_session, "sasol", remote=[{"symbol": "SOLBE1", "name": "Sasol BEE"}])
    # Name matches come before the "use as typed" fallback for ticker-shaped text.
    assert list(by_name) == ["SOL", "SOLBE1", "SASOL"]
    assert by_name["SASOL"] == "use as typed"


def test_match_symbols_long_names_are_not_offered_as_tickers(db_session):
    assert match_symbols(db_session, "standardbank", remote=[]) == {}


def test_match_symbols_survives_failed_remote_search(db_session):
    db_session.add(Stock(symbol="CPI", name="Capitec Bank"))
    db_session.commit()
    assert list(match_symbols(db_session, "capitec", remote=None)) == ["CPI"]


def test_search_jse_keeps_only_jse_listings(monkeypatch):
    quotes = [
        {"symbol": "SSL", "exchange": "NYQ", "shortname": "Sasol Ltd."},
        {"symbol": "SOL.JO", "exchange": "JNB", "longname": "Sasol Limited"},
        {"symbol": "SAOA.F", "exchange": "FRA", "shortname": "SASOL"},
    ]
    monkeypatch.setattr(yf_module.yf, "Search", MagicMock(return_value=SimpleNamespace(quotes=quotes)))
    collector = YFinanceCollector()
    collector.enabled = True

    assert collector.search_jse("sasol") == [{"symbol": "SOL", "name": "Sasol Limited"}]


def test_search_jse_failure_returns_none(monkeypatch):
    monkeypatch.setattr(yf_module.yf, "Search", MagicMock(side_effect=RuntimeError("down")))
    monkeypatch.setattr(YFinanceCollector, "_call_with_retry", staticmethod(lambda label, fn, **k: fn()))
    collector = YFinanceCollector()
    collector.enabled = True

    assert collector.search_jse("sasol") is None


# --- portfolio snapshots -------------------------------------------------

def _portfolio_with_holding(db, close=100.0):
    stock = Stock(symbol="SOL", name="Sasol")
    portfolio = Portfolio(name="P")
    db.add_all([stock, portfolio])
    db.flush()
    db.add(StockPrice(stock_id=stock.id, symbol="SOL", timestamp=datetime(2026, 11, 17), close_price=close))
    db.add(PortfolioHolding(
        portfolio_id=portfolio.id, stock_id=stock.id, symbol="SOL", account_type="Manual",
        quantity=10, purchase_price=90.0, purchase_date=datetime(2026, 1, 1),
    ))
    db.commit()
    return portfolio


def test_revalue_portfolio_records_one_snapshot_per_day(db_session, monkeypatch):
    portfolio = _portfolio_with_holding(db_session)
    monkeypatch.setattr(portfolio_valuation, "_today", lambda: date(2026, 11, 18))

    assert portfolio_valuation.revalue_portfolio(db_session) == pytest.approx(1000.0)
    # A second revaluation the same day overwrites, not duplicates.
    db_session.query(StockPrice).update({"close_price": 110.0})
    db_session.commit()
    portfolio_valuation.revalue_portfolio(db_session)

    rows = db_session.query(PortfolioSnapshot).filter_by(portfolio_id=portfolio.id).all()
    assert len(rows) == 1
    assert rows[0].snapshot_date == date(2026, 11, 18)
    assert rows[0].total_value == pytest.approx(1100.0)
    assert rows[0].total_cost == pytest.approx(900.0)


def test_no_snapshot_for_empty_portfolio(db_session):
    db_session.add(Portfolio(name="P"))
    db_session.commit()

    assert portfolio_valuation.revalue_portfolio(db_session) == pytest.approx(0.0)
    assert db_session.query(PortfolioSnapshot).count() == 0


def test_revalue_without_portfolio_returns_none(db_session):
    assert portfolio_valuation.revalue_portfolio(db_session) is None


# --- notification status / test send -------------------------------------

def _clear_channels(monkeypatch):
    for name in ("notification_email_from", "notification_email_password", "notification_ntfy_topic",
                 "notification_discord_webhook_url", "notification_telegram_bot_token",
                 "notification_telegram_chat_id"):
        monkeypatch.setattr(settings, name, "")


def test_channel_status_reflects_settings(monkeypatch):
    _clear_channels(monkeypatch)
    monkeypatch.setattr(settings, "notification_ntfy_topic", "alphalens")
    monkeypatch.setattr(settings, "notification_telegram_bot_token", "token")  # chat id missing

    assert NotificationService.channel_status() == {
        "email": False, "ntfy": True, "discord": False, "telegram": False,
    }


def test_send_test_explains_missing_channels(monkeypatch, db_session):
    _clear_channels(monkeypatch)
    monkeypatch.setattr(settings, "notification_enabled", True)

    ok, message = NotificationService(db_session).send_test()

    assert ok is False
    assert "No delivery channel" in message


def test_send_test_when_disabled(monkeypatch, db_session):
    monkeypatch.setattr(settings, "notification_enabled", False)
    ok, message = NotificationService(db_session).send_test()
    assert ok is False and "disabled" in message


# --- per-row price formatting (watchlist) ----------------------------------

import pandas as pd  # noqa: E402
from app.dashboard.utils import format_price_cell, style_price_columns  # noqa: E402


@pytest.mark.parametrize("value,reference,signed,expected", [
    (210.38, 210.38, False, "R 210.38"),
    (-1.460000000000008, 210.38, True, "R -1.46"),
    (0.11000000000000032, 5.61, True, "R +0.11"),
    (0.008500000238, None, False, "R 0.0085"),
    (0.62, 0.62, False, "R 0.62"),
    (1.2, 0.62, False, "R 1.20"),
    (0.0, 0.0085, True, "R +0.00"),
    (1234.5, None, False, "R 1,234.50"),
    (None, 5.0, False, ""),
])
def test_format_price_cell(value, reference, signed, expected):
    assert format_price_cell(value, reference, signed) == expected


def test_one_penny_stock_does_not_widen_other_rows():
    df = pd.DataFrame({"Current Price": [210.38, 0.0085], "Change": [-1.46, 0.0]})
    text = style_price_columns(
        df.style, df, "Current Price",
        price_cols=("Current Price",), signed_cols=("Change",),
    ).to_string()
    assert "R 210.38" in text and "R 210.3800" not in text
    assert "R -1.46" in text and "R 0.0085" in text


def test_style_price_columns_reference_series_from_hidden_column():
    """Portfolio: the user hid Current Price, decimals still follow it."""
    full = pd.DataFrame({"Purchase Price": [1.2, 150.0], "Current Price": [0.62, 210.38]})
    visible = full[["Purchase Price"]]
    text = style_price_columns(
        visible.style, visible, full["Current Price"], price_cols=("Purchase Price",),
    ).to_string()
    assert "R 1.20" in text and "R 150.00" in text


def test_style_price_columns_without_reference_judges_each_value():
    """Dividends: R 0.0525 keeps 4 decimals, R 6.10 gets 2 - no R 6.1000."""
    df = pd.DataFrame({"Amount": [0.0525, 6.1, None]})
    text = style_price_columns(df.style, df, price_cols=("Amount",)).to_string()
    assert "R 0.0525" in text and "R 6.10" in text and "6.1000" not in text


def test_style_price_columns_missing_reference_column_is_safe():
    df = pd.DataFrame({"Price": [0.5]})
    text = style_price_columns(df.style, df, "Nope", price_cols=("Price", "Absent")).to_string()
    assert "R 0.50" in text
