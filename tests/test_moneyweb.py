"""Regression tests for Moneyweb login / SENS parsing."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import requests

from app.collectors.moneyweb import MoneywebCollector


def _session_with_login_cookie():
    """Session whose wp-login POST returns a 302 + logged-in cookie."""
    session = requests.Session()
    session.cookies.set("wordpress_logged_in_abc", "user|123")
    session.post = MagicMock(return_value=SimpleNamespace(
        status_code=302, text="", headers={}))
    session.get = MagicMock(return_value=SimpleNamespace(
        status_code=200, text=""))
    return session


def test_login_accepts_wordpress_cookie(monkeypatch):
    """The auth-cookie check must read Cookie.name, not iterate the jar."""
    session = _session_with_login_cookie()
    collector = MoneywebCollector()
    monkeypatch.setattr(collector, "_get_session", lambda: session)
    monkeypatch.setattr(collector, "enabled", True)

    assert collector._login() is True
    assert collector._authenticated is True


def test_login_rejects_missing_cookie(monkeypatch):
    """A 302 without the logged-in cookie is a failed login, not success."""
    session = requests.Session()
    session.cookies.set("wordpress_test_cookie", "WP+Cookie+check")
    session.post = MagicMock(return_value=SimpleNamespace(
        status_code=302, text="", headers={}))
    session.get = MagicMock(return_value=SimpleNamespace(
        status_code=200, text=""))
    collector = MoneywebCollector()
    monkeypatch.setattr(collector, "_get_session", lambda: session)
    monkeypatch.setattr(collector, "enabled", True)

    assert collector._login() is False
    assert "no auth cookie" in collector.last_error
