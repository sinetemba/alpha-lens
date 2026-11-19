"""The Home page's top movers come from Moneyweb's public feed - no login."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.collectors.moneyweb import MoneywebCollector


def test_movers_fetch_does_not_log_in(monkeypatch):
    """A wp-login rate limit (HTTP 429) must not blank the movers."""
    rows = [["<a>MTU</a>", "0.12", "0.02", "20.00%"]]
    session = SimpleNamespace(post=MagicMock(return_value=SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: rows)))
    collector = MoneywebCollector()
    login = MagicMock(return_value=False)
    monkeypatch.setattr(collector, "_login", login)
    monkeypatch.setattr(collector, "_get_session", lambda: session)

    result = collector.get_winners_and_losers()

    login.assert_not_called()
    assert result is not None
    winners, losers = result
    assert len(winners) == 1 and len(losers) == 1
    assert winners[0]["Price"] == 0.12
    assert winners[0]["Change"] == 20.0
