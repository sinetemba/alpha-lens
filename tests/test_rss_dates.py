"""RSS date parsing (News24's non-RFC format) and one warning per feed."""
from datetime import datetime, timezone

import pytest
from loguru import logger

from app.scrapers.rss import RSSScraper


@pytest.mark.parametrize("raw, expected", [
    # RFC 822 (BusinessTech, Moneyweb)
    ("Sat, 21 Nov 2026 19:16:20 +0000", datetime(2026, 11, 21, 19, 16, 20, tzinfo=timezone.utc)),
    ("Sat, 21 Nov 2026 21:16:20 +0200", datetime(2026, 11, 21, 19, 16, 20, tzinfo=timezone.utc)),
    # News24 capi24 - UTC (matches the article pages' datePublished +00:00)
    ("Friday Nov 20 2026 17:24:41", datetime(2026, 11, 20, 17, 24, 41, tzinfo=timezone.utc)),
    ("  Saturday Nov 21 2026 15:21:09 ", datetime(2026, 11, 21, 15, 21, 9, tzinfo=timezone.utc)),
])
def test_known_formats_parse_to_utc(raw, expected):
    assert RSSScraper()._parse_date(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "next Tuesday", "2026-99-99"])
def test_unknown_formats_return_none_quietly(raw):
    assert RSSScraper()._parse_date(raw) is None


_FEED = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>a</title><link>https://x/a</link><pubDate>Friday Nov 20 2026 17:24:41</pubDate></item>
<item><title>b</title><link>https://x/b</link><pubDate>someday</pubDate></item>
<item><title>c</title><link>https://x/c</link><pubDate>whenever</pubDate></item>
</channel></rss>"""


def test_unparseable_dates_warn_once_per_feed(monkeypatch):
    class Resp:
        content = _FEED
        def raise_for_status(self):
            pass

    monkeypatch.setattr("app.scrapers.rss.requests.get", lambda url, timeout: Resp())
    messages = []
    sink = logger.add(lambda m: messages.append(m.record), level="WARNING")
    try:
        articles = RSSScraper().fetch_feed("https://x/rss")
    finally:
        logger.remove(sink)

    assert articles[0]["published_at"] == datetime(2026, 11, 20, 17, 24, 41, tzinfo=timezone.utc)
    assert [a["published_at"] for a in articles[1:]] == [None, None]
    assert len(messages) == 1 and "2/3 dates unparseable" in messages[0]["message"]
