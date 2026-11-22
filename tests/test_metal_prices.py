"""Live metal spots: Yahoo first, GoldAPI fallback with a failure cool-off."""
from types import SimpleNamespace

import pytest
import requests

from app.collectors import gold_api as gold_api_module
from app.collectors.gold_api import GoldAPICollector
from app.services import metal_prices
from app.services.metal_prices import GOLDAPI_SOURCE, YAHOO_SOURCE, live_metal_spots


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    GoldAPICollector._blocked_until = 0.0
    GoldAPICollector.last_error = None
    monkeypatch.setattr(metal_prices, "_record", lambda *a, **k: None)
    yield
    GoldAPICollector._blocked_until = 0.0


def _yahoo(spots):
    return SimpleNamespace(get_metal_spots=lambda: spots)


def _goldapi(prices, calls):
    def get_live_price(metal, currency):
        calls.append((metal, currency))
        return {"price": prices[(metal, currency)]} if (metal, currency) in prices else None
    return SimpleNamespace(enabled=True, blocked=lambda: False, last_error=None, get_live_price=get_live_price)


def test_yahoo_serves_both_metals_and_goldapi_is_not_called():
    calls = []
    spots = live_metal_spots(_yahoo({"XAU": {"usd": 3500.0, "zar": 61500.0},
                                     "XAG": {"usd": 41.0, "zar": 720.0}}), _goldapi({}, calls))
    assert spots["XAU"] == {"usd": 3500.0, "zar": 61500.0, "source": YAHOO_SOURCE}
    assert calls == []


def test_goldapi_fills_only_the_metal_yahoo_missed():
    calls = []
    spots = live_metal_spots(_yahoo({"XAU": {"usd": 3500.0, "zar": 61500.0}}),
                             _goldapi({("XAG", "USD"): 41.0, ("XAG", "ZAR"): 720.0}, calls))
    assert spots["XAG"]["source"] == GOLDAPI_SOURCE
    assert {m for m, _ in calls} == {"XAG"}


def test_everything_failing_is_empty():
    assert live_metal_spots(_yahoo(None), _goldapi({}, [])) == {}


def test_goldapi_403_trips_a_long_cooloff_and_stops_requests(monkeypatch):
    """A 403 (bad key / quota) used to be retried ~14x per page rerun."""
    sent = []

    def fake_get(url, headers, timeout):
        sent.append(url)
        resp = requests.Response()
        resp.status_code = 403
        raise requests.HTTPError("403 Client Error: Forbidden", response=resp)

    monkeypatch.setattr(gold_api_module.requests, "get", fake_get)
    monkeypatch.setattr(gold_api_module.settings, "gold_api_key", "k")
    api = GoldAPICollector()
    assert api.get_live_price("XAU", "ZAR") is None
    assert GoldAPICollector.blocked()
    assert "quota" in GoldAPICollector.last_error
    # Further calls - even from new instances (each rerun makes one) - send nothing.
    assert GoldAPICollector().get_live_price("XAU", "USD") is None
    assert GoldAPICollector().get_historical_price("XAU", "ZAR", "20251231") is None
    assert len(sent) == 1
    assert GoldAPICollector._blocked_until - gold_api_module.time.monotonic() > 5 * 3600


def test_a_transient_error_cools_off_briefly(monkeypatch):
    def fake_get(url, headers, timeout):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(gold_api_module.requests, "get", fake_get)
    monkeypatch.setattr(gold_api_module.settings, "gold_api_key", "k")
    GoldAPICollector().get_live_price("XAU", "ZAR")
    remaining = GoldAPICollector._blocked_until - gold_api_module.time.monotonic()
    assert 0 < remaining <= 10 * 60
