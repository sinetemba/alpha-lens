"""Tests for per-source health heartbeats and failing-source alerts."""
from datetime import timedelta
from types import SimpleNamespace

from app.models.base import utcnow_naive
from app.models.notification import Notification
from app.models.updater_run import UpdaterRun
from app.scrapers.rss import RSSScraper
from app.services.source_health import (
    check_source_health,
    record_source,
    rss_key,
    source_statuses,
)


def _beat(db, key, ok, hours_ago, detail=None):
    db.add(UpdaterRun(job_name=f"source:{key}", success=ok, detail=detail,
                      finished_at=utcnow_naive() - timedelta(hours=hours_ago)))
    db.commit()


def _status(db, key):
    return next(s for s in source_statuses(db) if s.key == key)


def test_record_and_status(db_session):
    record_source(db_session, "safex", True)
    s = _status(db_session, "safex")
    assert s.ok and s.last_success is not None and s.last_failure is None
    assert s.label == "Grain SA - SAFEX maize & wheat"


def test_failing_since_is_the_first_failure_after_the_last_success(db_session):
    _beat(db_session, "moneyweb_login", True, 100)
    _beat(db_session, "moneyweb_login", False, 60, "Login rejected with HTTP 403")
    _beat(db_session, "moneyweb_login", False, 10, "Login rejected with HTTP 403")
    s = _status(db_session, "moneyweb_login")
    assert not s.ok
    assert s.last_detail == "Login rejected with HTTP 403"
    assert 59 <= s.failing_for(utcnow_naive()).total_seconds() / 3600 <= 61


def test_a_source_failing_two_days_raises_one_notification(db_session):
    _beat(db_session, "moneyweb_login", False, 50, "Login rejected with HTTP 403")
    _beat(db_session, "moneyweb_login", False, 1, "Login rejected with HTTP 403")
    assert check_source_health(db_session) == 1
    assert check_source_health(db_session) == 1   # dedup: still one row
    note = db_session.query(Notification).one()
    assert note.type == "system" and "Moneyweb" in note.title
    assert "HTTP 403" in note.message


def test_recent_or_recovered_failures_do_not_alert(db_session):
    _beat(db_session, "safex", False, 5)                 # failing, but only 5h
    _beat(db_session, "luno", False, 80)
    _beat(db_session, "luno", True, 2)                   # recovered
    assert check_source_health(db_session) == 0
    assert db_session.query(Notification).count() == 0
    assert _status(db_session, "luno").ok


def test_old_heartbeats_are_pruned(db_session):
    _beat(db_session, "safex", True, 24 * 40)
    _beat(db_session, "safex", True, 1)
    check_source_health(db_session)
    assert db_session.query(UpdaterRun).count() == 1


def test_rss_results_are_tracked_per_feed(monkeypatch):
    scraper = RSSScraper()
    scraper.feeds = ["https://good.example/rss", "https://bad.example/rss"]

    def fake_fetch(url):
        if "bad" in url:
            scraper._last_error = "HTTPError: 404"
            return None
        return [{"title": "t", "url": "u"}]

    monkeypatch.setattr(scraper, "fetch_feed", fake_fetch)
    scraper.fetch_all_feeds()
    assert scraper.feed_results == {"https://good.example/rss": None, "https://bad.example/rss": "HTTPError: 404"}
    assert rss_key("https://bad.example/rss") == "rss:bad.example"


def test_futures_refresh_records_its_outcome(db_session):
    from app.services.commodity_futures import refresh_futures
    refresh_futures(db_session, SimpleNamespace(get_futures_closes=lambda tickers, period: None))
    s = _status(db_session, "yahoo_futures")
    assert not s.ok and "Yahoo download failed" in s.last_detail
