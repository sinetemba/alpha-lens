"""Company View news: the stored RSS articles are used, live feeds only as fallback."""
from datetime import timedelta
from types import SimpleNamespace

from app.models.base import utcnow_naive
from app.models.news import NewsArticle
from app.services.company_news import CompanyNewsService


def _service(db, live_calls):
    svc = CompanyNewsService(db)
    svc.rss = SimpleNamespace(fetch_all_feeds=lambda: live_calls.append(1) or [])
    return svc


def _article(db, title, hours_ago):
    ts = utcnow_naive() - timedelta(hours=hours_ago)
    db.add(NewsArticle(title=title, summary="", url=f"https://x/{title}", source="T",
                       published_at=ts, created_at=ts))
    db.commit()


def test_fresh_store_with_no_match_skips_the_live_scrape(db_session):
    """Most small companies have no RSS match - that used to cost 5 live feeds (~5s)."""
    _article(db_session, "Sasol results", 2)
    calls = []
    assert _service(db_session, calls)._fetch_rss(["aeg", "aveng"]) == []
    assert calls == []


def test_stored_match_is_returned(db_session):
    _article(db_session, "Aveng wins contract", 2)
    calls = []
    assert [a["title"] for a in _service(db_session, calls)._fetch_rss(["aveng"])] == ["Aveng wins contract"]
    assert calls == []


def test_stale_store_falls_back_to_live_feeds(db_session):
    """If the updater hasn't stored anything for 12h+, scrape live."""
    _article(db_session, "Old story", 30)
    calls = []
    _service(db_session, calls)._fetch_rss(["aveng"])
    assert calls == [1]
