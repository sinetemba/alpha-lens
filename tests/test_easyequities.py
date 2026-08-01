import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.collectors.easyequities import EasyEquitiesCollector
from app.dashboard.portfolio import _sync_portfolio_from_easyequities, EXCLUDED_PORTFOLIO_SYMBOLS
from app.models.portfolio import Portfolio, PortfolioHolding


def _mock_amount_parser(value):
    """Simple parser replacement that strips a leading 'R ' and converts to float."""
    if not value or not str(value).strip():
        raise ValueError("empty amount")
    cleaned = str(value).replace("R ", "").replace(",", "")
    return ("ZAR", value, float(cleaned))


@pytest.fixture(autouse=True)
def patch_amount_parser(monkeypatch):
    """Replace the EasyEquities amount parser so tests don't depend on its regex."""
    monkeypatch.setattr(
        "easy_equities_client.accounts.parsers.get_amount_and_currency_from_string",
        _mock_amount_parser,
    )


def test_parse_holding_extracts_symbol_and_values():
    raw = {
        "contract_code": "EQU.ZA.NPN",
        "name": "Naspers Ltd",
        "isin": "ZAE000296111",
        "shares": "100",
        "purchase_value": "12345.67",
        "current_price": "150.00",
        "current_value": "15000.00",
    }
    parsed = EasyEquitiesCollector._parse_holding(raw)
    assert parsed["symbol"] == "NPN"
    assert parsed["name"] == "Naspers Ltd"
    assert parsed["shares"] == 100.0
    assert parsed["purchase_price"] == pytest.approx(123.4567)
    assert parsed["current_price"] == pytest.approx(150.0)
    assert parsed["current_value"] == pytest.approx(15000.0)


def test_parse_holding_handles_zero_shares():
    raw = {
        "contract_code": "EQU.ZA.CFR",
        "name": "Richemont",
        "shares": "0",
        "purchase_value": "1000.00",
        "current_price": "50.00",
        "current_value": "0.00",
    }
    parsed = EasyEquitiesCollector._parse_holding(raw)
    assert parsed["shares"] == 0.0
    assert parsed["purchase_price"] == 0.0
    assert parsed["current_value"] == 0.0


def test_parse_holding_missing_contract_code():
    raw = {
        "shares": "10",
        "purchase_value": "1000.00",
        "current_price": "120.00",
        "current_value": "1200.00",
    }
    parsed = EasyEquitiesCollector._parse_holding(raw)
    assert parsed["symbol"] == ""


def test_collector_disabled_without_credentials():
    collector = EasyEquitiesCollector()
    collector.enabled = False
    assert collector.list_accounts() is None
    assert collector.get_all_holdings() is None


def test_get_all_holdings_aggregates_accounts(monkeypatch):
    raw_holding = {
        "contract_code": "EQU.ZA.NPN",
        "name": "Naspers Ltd",
        "shares": "10",
        "purchase_value": "1234.50",
        "current_price": "150.00",
        "current_value": "1500.00",
    }
    account = MagicMock()
    account.id = "zar-1"
    account.name = "ZAR"
    account.trading_currency_id = "ZAR"
    client = MagicMock()
    client.accounts.list.return_value = [account]
    client.accounts.holdings.return_value = [raw_holding]

    collector = EasyEquitiesCollector()
    collector.enabled = True
    monkeypatch.setattr(collector, "_get_client", lambda: client)

    result = collector.get_all_holdings()
    assert len(result) == 1
    assert result[0]["symbol"] == "NPN"
    assert result[0]["account_name"] == "ZAR"


def test_sync_portfolio_aggregates_and_excludes(db_session):
    portfolio = Portfolio(name="Test Portfolio")
    db_session.add(portfolio)
    db_session.commit()

    ee_holdings = [
        {
            "symbol": "NPN",
            "name": "Naspers",
            "shares": 10.0,
            "purchase_price": 100.0,
            "current_price": 110.0,
            "current_value": 1100.0,
            "purchase_value": 1000.0,
            "account_name": "ZAR",
        },
        {
            "symbol": "NPN",
            "name": "Naspers",
            "shares": 5.0,
            "purchase_price": 100.0,
            "current_price": 110.0,
            "current_value": 550.0,
            "purchase_value": 500.0,
            "account_name": "ZAR",
        },
        {
            "symbol": "SZK",
            "name": "Excluded",
            "shares": 100.0,
            "purchase_price": 1.0,
            "current_price": 1.0,
            "current_value": 100.0,
            "purchase_value": 100.0,
            "account_name": "ZAR",
        },
    ]
    collector = MagicMock()
    collector.get_all_holdings.return_value = ee_holdings

    synced = _sync_portfolio_from_easyequities(db_session, portfolio.id, collector)

    assert synced == 1
    assert "SZK" in EXCLUDED_PORTFOLIO_SYMBOLS

    holding = db_session.query(PortfolioHolding).filter_by(symbol="NPN").first()
    assert holding is not None
    assert holding.quantity == 15.0
    assert holding.purchase_price == pytest.approx(100.0)
    assert holding.current_price == pytest.approx(110.0)
    assert holding.current_value == pytest.approx(1650.0)
    assert holding.updated_at is not None
    updated_at = holding.updated_at.replace(tzinfo=None) if holding.updated_at.tzinfo else holding.updated_at
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    assert (now_utc - updated_at).total_seconds() < 5
