"""Tests for the Luno collector and balance sync (HTTP is mocked)."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests

from app.collectors import luno as luno_module
from app.collectors.luno import LunoCollector, luno_asset_for, luno_coin_id
from app.config.settings import settings
from app.dashboard.crypto import _holding_price
from app.models.crypto import CryptoBundle, CryptoHolding
from app.services.luno_sync import BUNDLE_NAME, bundle_invested, sync_luno_holdings, wallet_cost, zar_amount


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

def _fake_collector(balances, bundle=None, history=None):
    """Wallet {asset: amount}, bundle {asset: amount}; None = balance fetch failed.

    `history` maps account ids ("W-<asset>" wallet, "B-<asset>" bundle) to
    transaction lists; the string "fail" makes every history call fail.
    """
    if balances is None:
        return SimpleNamespace(get_accounts=lambda: None, last_error="down")
    accounts = [{"account_id": f"W-{a}", "asset": a, "account_type": "TRANSACTIONAL", "balance": str(v)}
                for a, v in balances.items()]
    accounts += [{"account_id": f"B-{a}", "asset": a, "account_type": "BUNDLE", "balance": str(v)}
                 for a, v in (bundle or {}).items()]
    fake = SimpleNamespace(get_accounts=lambda: accounts, last_error=None)
    if history == "fail":
        def _fail(account_id):
            fake.last_error = "The Luno API key lacks permission for this"
            return None
        fake.get_transactions = _fail
    else:
        fake.get_transactions = lambda account_id: list((history or {}).get(account_id, []))
    return fake

def test_sync_creates_luno_rows_and_reports_cash(db_session):
    result = sync_luno_holdings(db_session, _fake_collector({"XBT": 0.015, "ZAR": 152.3, "ETH": 0.0}))

    # ZAR cash becomes a "luno-zar" holding row (net worth counts it);
    # result.fiat still reports it for the page caption.
    rows = db_session.query(CryptoHolding).order_by(CryptoHolding.id).all()
    assert [(r.coin_id, r.symbol, r.source, r.quantity) for r in rows] == [
        ("bitcoin", "BTC", "luno", pytest.approx(0.015)),
        ("luno-zar", "ZAR", "luno", pytest.approx(152.3)),
    ]
    assert rows[0].last_synced_at is not None
    assert result.synced == 2 and result.fiat == {"ZAR": pytest.approx(152.3)}


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


def test_account_balances_keep_account_type(collector, monkeypatch):
    _mock_get(monkeypatch, _response(200, {"balance": [
        {"asset": "XRP", "balance": "10", "account_type": "TRANSACTIONAL"},
        {"asset": "XRP", "balance": "2", "account_type": "BUNDLE"},
        {"asset": "BNB", "balance": "0.004", "account_type": "BUNDLE"},
    ]}))
    assert collector.get_account_balances() == [
        ("XRP", "TRANSACTIONAL", 10.0), ("XRP", "BUNDLE", 2.0), ("BNB", "BUNDLE", 0.004),
    ]


def test_bundle_coins_kept_apart_from_wallet_coins(db_session):
    """The user's real layout: ADA/XLM/XRP in wallets, a 5-coin bundle that also holds XRP."""
    sync_luno_holdings(db_session, _fake_collector(
        {"ADA": 100.0, "XLM": 50.0, "XRP": 10.0, "XBT": 0.0, "ETH": 0.0},
        bundle={"XBT": 0.0006, "ETH": 0.0036, "XRP": 1.9, "SOL": 0.017, "BNB": 0.004, "ADA": 0.0},
    ))

    rows = {(r.symbol, r.bundle): r.quantity for r in db_session.query(CryptoHolding)}
    assert rows == {
        ("ADA", None): 100.0, ("XLM", None): 50.0, ("XRP", None): 10.0,
        ("BTC", BUNDLE_NAME): 0.0006, ("ETH", BUNDLE_NAME): 0.0036, ("XRP", BUNDLE_NAME): 1.9,
        ("SOL", BUNDLE_NAME): 0.017, ("BNB", BUNDLE_NAME): 0.004,
    }
    assert db_session.query(CryptoBundle).filter_by(name=BUNDLE_NAME).count() == 1


def test_bundle_cost_survives_resync(db_session):
    sync_luno_holdings(db_session, _fake_collector({}, bundle={"XBT": 0.0006}))
    db_session.query(CryptoBundle).one().purchase_cost = 800.0
    db_session.commit()

    sync_luno_holdings(db_session, _fake_collector({}, bundle={"XBT": 0.0007}))

    assert db_session.query(CryptoBundle).one().purchase_cost == pytest.approx(800.0)
    assert db_session.query(CryptoHolding).one().quantity == pytest.approx(0.0007)


def test_bundle_value_sums_coins():
    from app.dashboard.crypto import _bundle_value
    coins = [
        SimpleNamespace(source="luno", coin_id="bitcoin", quantity=0.0006, purchase_price=None),
        SimpleNamespace(source="luno", coin_id="binancecoin", quantity=0.004, purchase_price=None),
    ]
    value = _bundle_value(coins, markets={}, luno_prices={"XBT": 1_000_000.0, "BNB": 9_500.0})
    assert value == pytest.approx(600.0 + 38.0)



# --- cost basis from transaction history ---------------------------------
# Shapes copied from real Luno responses (amounts use non-breaking spaces).

def _tx(row, kind, delta, description, approx=None):
    return {"row_index": row, "kind": kind, "balance_delta": delta, "description": description,
            "timestamp": 1742402104041, "details": {"Approximate value": approx} if approx else {}}


def test_zar_amount_parsing():
    assert zar_amount(_tx(1, "EXCHANGE", 1, "Bought XLM 180.91912435 for R 1,000.00")) == 1000.0
    assert zar_amount(_tx(1, "EXCHANGE", -1, "Sold BTC 0.00458216 for ZAR 563.55")) == 563.55
    # Crypto-for-crypto: fall back to Luno's approximate Rand value.
    assert zar_amount(_tx(1, "EXCHANGE", 1, "Bought ETH 0.012 for BTC 0.001",
                          approx="~R 245.10 (20,300 ZAR/ETH)")) == 245.10


def test_wallet_cost_averages_buys():
    txs = [_tx(1, "EXCHANGE", 33.829151, "Bought XRP 33.829151 for ZAR 500.00"),
           _tx(2, "EXCHANGE", 37.426788, "Bought XRP 37.426788 for ZAR 1,000.00")]
    cost, qty, first = wallet_cost(txs)
    assert cost == pytest.approx(1500.0)
    assert qty == pytest.approx(71.255939)
    assert first is not None


def test_wallet_cost_sells_release_cost_and_fees_do_not():
    txs = [_tx(1, "EXCHANGE", 2.0, "Bought ETH 2 for R 1,000.00"),
           _tx(2, "FEE", -0.1, "Withdrawal fee"),               # coins gone, cost stays
           _tx(3, "EXCHANGE", -0.95, "Sold ETH 0.95 for R 600.00")]  # half of 1.9 left
    cost, qty, _ = wallet_cost(txs)
    assert qty == pytest.approx(0.95)
    assert cost == pytest.approx(500.0)


def test_bundle_invested_ignores_rebalances_and_fees():
    txs = [_tx(1, "EXCHANGE", 0.0004061, "Bought BTC 0.0004061 for R 790.82 as part of LLC bundle"),
           _tx(2, "EXCHANGE", 0.0024, "Bought ETH 0.0024 for R 111.93 as part of LLC bundle"),
           _tx(3, "TRANSFER", 1.1e-06, "BundleRebalanceCredit", approx="~R 1.43"),
           _tx(4, "TRANSFER", -2e-06, "BundleTrackingFeeDebit")]
    invested, label = bundle_invested(txs)
    assert invested == pytest.approx(902.75)
    assert label == "LLC bundle"


def test_sync_derives_costs_from_history(db_session):
    history = {
        "W-XLM": [_tx(1, "EXCHANGE", 180.91912435, "Bought XLM 180.91912435 for R 1,000.00")],
        "B-XBT": [_tx(1, "EXCHANGE", 0.0004061, "Bought BTC 0.0004061 for R 790.82 as part of LLC bundle")],
        # Emptied by a rebalance but its purchase still counts as money in.
        "B-ADA": [_tx(1, "EXCHANGE", 0.74, "Bought ADA 0.74 for R 7.64 as part of LLC bundle"),
                  _tx(2, "TRANSFER", -0.74, "BundleRebalanceDebit")],
    }
    result = sync_luno_holdings(db_session, _fake_collector(
        {"XLM": 180.91912435}, bundle={"XBT": 0.0004061, "ADA": 0.0}, history=history))

    xlm = db_session.query(CryptoHolding).filter_by(symbol="XLM").one()
    assert xlm.purchase_price == pytest.approx(1000 / 180.91912435)
    assert xlm.cost_source == "luno" and xlm.purchase_date is not None
    record = db_session.query(CryptoBundle).one()
    assert record.purchase_cost == pytest.approx(798.46)
    assert record.label == "LLC bundle"
    assert result.cost_error is None


def test_manual_cost_is_never_overwritten(db_session):
    history = {"W-XLM": [_tx(1, "EXCHANGE", 100.0, "Bought XLM 100 for R 500.00")]}
    sync_luno_holdings(db_session, _fake_collector({"XLM": 100.0}, history=history))
    row = db_session.query(CryptoHolding).one()
    row.purchase_price, row.cost_source = 9.0, "manual"
    db_session.commit()

    sync_luno_holdings(db_session, _fake_collector({"XLM": 100.0}, history=history))

    assert db_session.query(CryptoHolding).one().purchase_price == pytest.approx(9.0)


def test_unreadable_history_keeps_existing_costs(db_session):
    history = {"W-XLM": [_tx(1, "EXCHANGE", 100.0, "Bought XLM 100 for R 500.00")]}
    sync_luno_holdings(db_session, _fake_collector({"XLM": 100.0}, history=history))

    result = sync_luno_holdings(db_session, _fake_collector({"XLM": 100.0}, history="fail"))

    assert result is not None and "permission" in result.cost_error
    assert db_session.query(CryptoHolding).one().purchase_price == pytest.approx(5.0)
