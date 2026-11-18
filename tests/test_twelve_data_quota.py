"""Tests for Twelve Data credit accounting (app/collectors/twelve_data.py)."""
from collections import deque
from unittest.mock import MagicMock

import pytest
import requests

from app.collectors import twelve_data
from app.collectors.twelve_data import TwelveDataCollector
from app.config.settings import settings


@pytest.fixture
def collector(monkeypatch):
    """Collector with fresh class-level counters and no on-disk persistence."""
    monkeypatch.setattr(TwelveDataCollector, "_request_times", deque())
    monkeypatch.setattr(TwelveDataCollector, "_minute_limit", 8)
    monkeypatch.setattr(TwelveDataCollector, "_daily_limit", 800)
    monkeypatch.setattr(TwelveDataCollector, "_daily_count", 0)
    monkeypatch.setattr(TwelveDataCollector, "_quota_exhausted_logged", False)
    monkeypatch.setattr(TwelveDataCollector, "_reset_daily_count_if_new_day", classmethod(lambda cls: None))
    monkeypatch.setattr(TwelveDataCollector, "_persist_daily_state", classmethod(lambda cls: None))
    monkeypatch.setattr(twelve_data.time, "sleep", lambda s: None)
    monkeypatch.setattr(settings, "twelve_data_enabled", True)
    monkeypatch.setattr(settings, "twelve_data_api_key", "test-key")
    return TwelveDataCollector()


def _response(status, payload=None):
    response = MagicMock(status_code=status)
    response.json.return_value = payload or {}
    if status >= 400:
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=response)
    return response


def test_batch_quote_costs_one_credit_per_symbol(collector, monkeypatch):
    get = MagicMock(return_value=_response(200, {}))
    monkeypatch.setattr(twelve_data.requests, "get", get)

    collector.get_multiple_quotes(["SOL", "NPN", "AGL"])

    assert TwelveDataCollector._daily_count == 3


def test_batches_capped_at_minute_limit(collector, monkeypatch):
    get = MagicMock(return_value=_response(200, {}))
    monkeypatch.setattr(twelve_data.requests, "get", get)

    collector.get_multiple_quotes([f"S{i}" for i in range(10)])

    sent = get.call_args.kwargs["params"]["symbol"].split(",")
    assert len(sent) == 8
    assert TwelveDataCollector._daily_count == 8


def test_retries_are_counted(collector, monkeypatch):
    get = MagicMock(side_effect=[_response(503), _response(503), _response(200, {"price": "1"})])
    monkeypatch.setattr(twelve_data.requests, "get", get)

    collector._make_request("price", {"symbol": "SOL"})

    assert get.call_count == 3
    assert TwelveDataCollector._daily_count == 3


def test_request_skipped_when_day_cannot_cover_it(collector, monkeypatch):
    TwelveDataCollector._daily_count = 795
    get = MagicMock()
    monkeypatch.setattr(twelve_data.requests, "get", get)

    assert collector.get_multiple_quotes([f"S{i}" for i in range(8)]) == {}
    get.assert_not_called()
    assert TwelveDataCollector._daily_count == 795


def test_disabled_makes_no_requests(collector, monkeypatch):
    monkeypatch.setattr(settings, "twelve_data_enabled", False)
    get = MagicMock()
    monkeypatch.setattr(twelve_data.requests, "get", get)

    assert collector.enabled is False
    assert collector._make_request("quote", {"symbol": "SOL"}) is None
    get.assert_not_called()


def test_throttle_waits_for_enough_credits(collector, monkeypatch):
    """A 3-credit request against a window holding 7 must wait for 2 to expire."""
    clock = {"now": 1000.0}
    sleeps = []
    monkeypatch.setattr(twelve_data.time, "monotonic", lambda: clock["now"])

    def _sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(twelve_data.time, "sleep", _sleep)
    TwelveDataCollector._request_times.extend([990.0, 995.0, 999.0, 999.0, 999.0, 999.0, 999.0])

    assert TwelveDataCollector._throttle(3) is True

    # The 2nd-oldest entry (995) must age out: 60 - (1000 - 995) + 0.1.
    assert sleeps == [pytest.approx(55.1)]
