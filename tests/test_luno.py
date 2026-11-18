"""Tests for the Luno collector and balance sync (HTTP is mocked)."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

from app.collectors import luno as luno_module
from app.collectors.luno import LunoCollector, luno_asset_for, luno_coin_id
from app.config.settings import settings
from app.dashboard.crypto import _holding_price
from app.models.crypto import CryptoHolding
from app.services.luno_sync import sync_luno_holdings


def _response(status, payload):
    response = MagicMock(status_code=status)
    response.json.return_value = payload
    if status >= 400:
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=response)
    return response


@pytest.fixture
def collector(monkeypatch):
    monkeypatch.setattr(settings, "luno_api_key_id", "key-id")
    monkeypatch.setattr(settings, "luno_api_secret", "secret")
    monkeypatch.setattr(luno_module.time, "sleep", lambda s: None)
    return LunoCollector()


def _mock_get(monkeypatch, *responses):
    get = MagicMock(side_effect=list(responses))
    monkeypatch.setattr(luno_module.requests, "get", get)
    return get


# --- collector -----------------------------------------------------------

def test_balances_summed_per_asset_with_basic_auth(collector, monkeypatch):
    get = _mock_get(monkeypatch, _response(200, {"balance": [
        {"account_id": "1", "asset": "XBT", "balance": "0.0100", "reserved": "0.002"},
        {"account_id": "2", "asset": "XBT", "balance": "0.0050"},   # second BTC account
        {"account_id": "3", "asset": "ZAR", "balance": "152.30"},
        {"account_id": "4", "asset": "ETH", "balance": "0"},
    ]}))

    assert collector.get_balances() == {
        "XBT": pytest.approx(0.015), "ZAR": pytest.approx(152.30), "ETH": 0.0,
    }
    assert get.call_args.kwargs["auth"] == ("key-id", "secret")


def test_rejected_key_is_reported_without_retry(collector, monkeypatch):
    get = _mock_get(monkeypatch, _response(401, {"error_code": "ErrAPIKeyNotFound"}))

    assert collector.get_balances() is None
    assert get.call_count == 1
    assert "ErrAPIKeyNotFound" in collector.last_error
    assert "secret" not in collector.last_error


def test_server_errors_are_retried(collector, monkeypatch):
    get = _mock_get(
        monkeypatch, _response(503, {}), _response(200, {"balance": [{"asset": "XBT", "balance": "1"}]})
    )
    assert collector.get_balances() == {"XBT": 1.0}
    assert get.call_count == 2


def test_unconfigured_collector_makes_no_request(monkeypatch):
    monkeypatch.setattr(settings, "luno_api_key_id", "")
    get = _mock_get(monkeypatch)
    collector = LunoCollector()

    assert collector.enabled is False
    assert collector.get_balances() is None
    get.assert_not_called()


def test_prices_use_bid_and_skip_non_zar(collector, monkeypatch):
    _mock_get(monkeypatch, _response(200, {"tickers": [
        {"pair": "XBTZAR", "bid": "1085444.00", "last_trade": "1086659.00", "status": "ACTIVE"},
        # Thin pair: stale last trade far below the live bid.
        {"pair": "DOTZAR", "bid": "12.1601", "last_trade": "3.00", "status": "ACTIVE"},
        {"pair": "UNIZAR", "bid": "0", "last_trade": "80.00", "status": "ACTIVE"},
        {"pair": "XBTEUR", "bid": "55000", "status": "ACTIVE"},
        {"pair": "ADAZAR", "bid": "2.6", "status": "DISABLED"},
    ]}))

    assert collector.get_zar_prices() == {
        "XBT": pytest.approx(1085444.0), "DOT": pytest.approx(12.1601), "UNI": pytest.approx(80.0),
    }


def test_asset_key_round_trip():
    assert luno_coin_id("XBT") == "bitcoin"
    assert luno_asset_for("bitcoin") == "XBT"
    assert luno_coin_id("NEWCOIN") == "luno-newcoin"
    assert luno_asset_for("luno-newcoin") == "NEWCOIN"


# --- sync ----------------------------------------------------------------

def _fake_collector(balances):
    return SimpleNamespace(get_balances=lambda: balances, last_error=None)


def test_sync_creates_luno_rows_and_reports_cash(db_session):
    result = sync_luno_holdings(db_session, _fake_collector({"XBT": 0.015, "ZAR": 152.3, "ETH": 0.0}))

    rows = db_session.query(CryptoHolding).all()
    assert [(r.coin_id, r.symbol, r.source, r.quantity) for r in rows] == [
        ("bitcoin", "BTC", "luno", pytest.approx(0.015)),
    ]
    assert rows[0].last_synced_at is not None
    assert result.synced == 1 and result.fiat == {"ZAR": pytest.approx(152.3)}


def test_sync_updates_quantity_but_keeps_cost_basis(db_session):
    sync_luno_holdings(db_session, _fake_collector({"XBT": 0.015}))
    row = db_session.query(CryptoHolding).one()
    row.purchase_price = 900_000.0
    db_session.commit()

    sync_luno_holdings(db_session, _fake_collector({"XBT": 0.02}))

    row = db_session.query(CryptoHolding).one()
    assert row.quantity == pytest.approx(0.02)
    assert row.purchase_price == pytest.approx(900_000.0)


def test_sync_removes_sold_coins_but_never_manual_rows(db_session):
    db_session.add(CryptoHolding(coin_id="bitcoin", symbol="BTC", name="Bitcoin", quantity=0.5))  # cold wallet
    db_session.commit()
    sync_luno_holdings(db_session, _fake_collector({"XBT": 0.015, "ETH": 0.3}))

    result = sync_luno_holdings(db_session, _fake_collector({"XBT": 0.015, "ETH": 0.0}))

    rows = {(r.coin_id, r.source) for r in db_session.query(CryptoHolding)}
    assert rows == {("bitcoin", "manual"), ("bitcoin", "luno")}
    assert result.removed == 1


def test_failed_fetch_changes_nothing(db_session):
    sync_luno_holdings(db_session, _fake_collector({"XBT": 0.015}))

    assert sync_luno_holdings(db_session, _fake_collector(None)) is None
    assert db_session.query(CryptoHolding).count() == 1


def test_dust_balances_are_not_holdings(db_session):
    sync_luno_holdings(db_session, _fake_collector({"XBT": 1e-10}))
    assert db_session.query(CryptoHolding).count() == 0


# --- page pricing --------------------------------------------------------

def test_luno_rows_priced_by_luno_manual_by_coingecko():
    markets = {"bitcoin": {"current_price": 1_079_415.0}}
    luno_prices = {"XBT": 1_085_444.0}
    luno_row = SimpleNamespace(source="luno", coin_id="bitcoin")
    manual_row = SimpleNamespace(source="manual", coin_id="bitcoin")

    assert _holding_price(luno_row, markets, luno_prices) == 1_085_444.0
    assert _holding_price(manual_row, markets, luno_prices) == 1_079_415.0


def test_assets_without_zar_pair_priced_via_usdt(collector, monkeypatch):
    _mock_get(monkeypatch, _response(200, {"tickers": [
        {"pair": "BNBUSDT", "bid": "550.01", "status": "ACTIVE"},
        {"pair": "USDTZAR", "bid": "17.3658", "status": "ACTIVE"},
        {"pair": "XBTZAR", "bid": "1085444", "status": "ACTIVE"},
        {"pair": "XBTUSDT", "bid": "60000", "status": "ACTIVE"},  # direct ZAR pair wins
    ]}))

    prices = collector.get_zar_prices()

    assert prices["BNB"] == pytest.approx(550.01 * 17.3658)
    assert prices["XBT"] == pytest.approx(1085444.0)


def test_luno_row_without_luno_price_falls_back_to_coingecko():
    row = SimpleNamespace(source="luno", coin_id="binancecoin")
    markets = {"binancecoin": {"current_price": 9981.34}}
    assert _holding_price(row, markets, luno_prices={}) == 9981.34
