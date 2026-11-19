"""Tests for the SENS IPO scanner (app/services/ipo.py + moneyweb parser)."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.collectors.moneyweb import MoneywebCollector
from app.models.notification import Notification
from app.models.upcoming_listing import UpcomingListing
from app.services import ipo
from app.services.ipo import is_ipo_announcement, update_ipo_listings


SENS_HTML = """
<div class="sens-row">
  <div class="col-lg-2"><time datetime="2026-11-19T09:00:00+00:00">19.11.26, 09:00</time></div>
  <div class="col-lg-1"><span><a href="https://www.moneyweb.co.za/tools-and-data/click-a-company/NEW/">NEW</a></span></div>
  <div class="col-lg-6"><a href="https://www.moneyweb.co.za/mny_sens/acme-pre-listing-statement/" title="Go to SENS announcement">
    <strong>ACME HOLDINGS LIMITED</strong> – Publication of Pre-listing Statement</a></div>
</div>
<div class="sens-row">
  <div class="col-lg-2"><time datetime="2026-11-19T08:00:00+00:00">19.11.26, 08:00</time></div>
  <div class="col-lg-1"></div>
  <div class="col-lg-6"><a href="https://www.moneyweb.co.za/mny_sens/listing-of-additional-satrix/" title="Go to SENS announcement">
    <strong>Listing of Additional Satrix MSCI World Feeder</strong></a></div>
</div>
"""


@pytest.mark.parametrize("headline,expected", [
    ("Publication of Pre-listing Statement", True),
    ("Intention to List on the Main Board", True),
    ("Proposed Listing of Acme Holdings", True),
    ("Admission to Listing of Ordinary Shares", True),
    ("Initial Public Offering of NewCo", True),
    ("New Financial Instrument Listing: AMB693", False),
    ("Listing of Additional ETF500 Securities", False),
    ("Interest Payment Notifications MWSI", False),
    ("Listing of Tap Issue", False),
    ("Partial Delisting of Satrix INDI", False),
    ("Transaction in own shares", False),
    ("", False),
    (None, False),
])
def test_is_ipo_announcement(headline, expected):
    assert is_ipo_announcement(headline) is expected


def test_sens_parser_extracts_fields(monkeypatch):
    """The sens-row parser pulls ticker, name, headline, timestamp, link."""
    session = SimpleNamespace(get=lambda *a, **k: SimpleNamespace(
        text=SENS_HTML, raise_for_status=lambda: None))
    collector = MoneywebCollector()
    monkeypatch.setattr(collector, "_get_session", lambda: session)

    rows = collector.get_sens_announcements()

    assert len(rows) == 2
    first, second = rows
    assert first["symbol"] == "NEW"
    assert first["name"] == "ACME HOLDINGS LIMITED"
    assert first["headline"] == "Publication of Pre-listing Statement"
    assert first["published_at"] == datetime(2026, 11, 19, 9, 0, tzinfo=timezone.utc)
    assert "acme-pre-listing-statement" in first["source_url"]
    assert second["symbol"] is None
    assert second["headline"] == "Listing of Additional Satrix MSCI World Feeder"


def _collector(announcements):
    return SimpleNamespace(get_sens_announcements=lambda: announcements)


def test_update_ipo_listings_stores_only_ipo_rows(db_session, monkeypatch):
    # Keep the listing-day probe quiet - no pending rows yet anyway.
    monkeypatch.setattr(ipo, "YFinanceCollector", MagicMock())
    anns = [
        {"published_at": datetime(2026, 11, 19, 9, tzinfo=timezone.utc),
         "symbol": "NEW", "name": "Acme", "headline": "Pre-listing statement",
         "source_url": "https://example.com/ipo1"},
        {"published_at": None, "symbol": "ETF", "name": None,
         "headline": "Listing of Additional ETF500",
         "source_url": "https://example.com/noise"},
        {"published_at": None, "symbol": None, "name": None,
         "headline": "Transaction in own shares",
         "source_url": "https://example.com/noise2"},
    ]
    added = update_ipo_listings(db_session, collector=_collector(anns))

    assert added == 1
    row = db_session.query(UpcomingListing).one()
    assert row.symbol == "NEW"
    assert row.status == "announced"
    assert row.sens_published_at == datetime(2026, 11, 19, 9, 0)
    note = db_session.query(Notification).filter_by(type="ipo").one()
    assert note.stock_symbol == "NEW"
    assert "Possible IPO" in note.title


def test_update_ipo_listings_dedupes_by_url(db_session, monkeypatch):
    monkeypatch.setattr(ipo, "YFinanceCollector", MagicMock())
    anns = [
        {"published_at": None, "symbol": "NEW", "name": "Acme",
         "headline": "Pre-listing statement",
         "source_url": "https://example.com/ipo1"},
    ]
    assert update_ipo_listings(db_session, collector=_collector(anns)) == 1
    assert update_ipo_listings(db_session, collector=_collector(anns)) == 0
    assert db_session.query(UpcomingListing).count() == 1


def test_mark_newly_listed_flips_status(db_session, monkeypatch):
    db_session.add(UpcomingListing(
        symbol="NEW", name="Acme", headline="Pre-listing statement",
        source_url="https://example.com/ipo1",
    ))
    db_session.add(UpcomingListing(
        symbol="WAIT", name="WaitingCo", headline="Intention to list",
        source_url="https://example.com/ipo2",
    ))
    db_session.commit()
    frames = {
        "NEW": pd.DataFrame({"Close": [10.0]}),   # trading now
        "WAIT": pd.DataFrame(),                    # not yet
    }
    yf = SimpleNamespace(get_latest_bars=lambda symbols, **kw: frames)
    monkeypatch.setattr(ipo, "YFinanceCollector", lambda: yf)
    collector = _collector([])  # no new announcements

    update_ipo_listings(db_session, collector=collector)

    rows = {r.symbol: r for r in db_session.query(UpcomingListing).all()}
    assert rows["NEW"].status == "listed"
    assert rows["WAIT"].status == "announced"
    notes = db_session.query(Notification).filter_by(type="ipo").all()
    assert any("Now trading" in n.title and n.stock_symbol == "NEW" for n in notes)


# --- false-positive guards -------------------------------------------------

@pytest.mark.parametrize("headline,expected", [
    ("Results of Private Placement", False),
    ("Intention to List on the JSE by way of a Private Placement", True),
    ("Admission to Listing of Additional Shares", False),
    ("Results of Accelerated Bookbuild", False),
])
def test_capital_raises_are_not_ipos(headline, expected):
    assert is_ipo_announcement(headline) is expected


def _bars(*days):
    return pd.DataFrame({"Close": [10.0] * len(days)}, index=pd.to_datetime(list(days)))


def _yf(frames, calls=None):
    def get_latest_bars(symbols, **kw):
        if calls is not None:
            calls.append(list(symbols))
        return frames
    return SimpleNamespace(get_latest_bars=get_latest_bars)


def test_already_trading_ticker_is_existing_and_not_alerted(db_session, monkeypatch):
    """A listed company's 'proposed listing' of new shares must not raise IPO alerts."""
    monkeypatch.setattr(ipo, "YFinanceCollector", lambda: _yf({
        "NPN": _bars("2026-11-13", "2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19"),
    }))
    anns = [{"published_at": datetime(2026, 11, 19, 9, tzinfo=timezone.utc),
             "symbol": "NPN", "name": "Naspers", "headline": "Proposed listing of N shares",
             "source_url": "https://example.com/npn"}]

    assert update_ipo_listings(db_session, collector=_collector(anns)) == 0

    assert db_session.query(UpcomingListing).one().status == "existing"
    assert db_session.query(Notification).filter_by(type="ipo").count() == 0


def test_stored_history_marks_existing_without_yahoo(db_session, monkeypatch):
    from app.models.stock import Stock, StockPrice
    stock = Stock(symbol="SOL", name="Sasol")
    db_session.add(stock)
    db_session.flush()
    db_session.add(StockPrice(stock_id=stock.id, symbol="SOL",
                              timestamp=datetime(2025, 1, 2), close_price=100.0))
    db_session.add(UpcomingListing(
        symbol="SOL", headline="Admission to listing", source_url="https://example.com/sol",
        sens_published_at=datetime(2026, 11, 19, 9),
    ))
    db_session.commit()
    calls = []
    monkeypatch.setattr(ipo, "YFinanceCollector", lambda: _yf({}, calls))

    update_ipo_listings(db_session, collector=_collector([]))

    assert db_session.query(UpcomingListing).one().status == "existing"
    assert calls == []


def test_bars_only_after_announcement_mark_listed(db_session, monkeypatch):
    db_session.add(UpcomingListing(
        symbol="NEW", name="Acme", headline="Pre-listing statement",
        source_url="https://example.com/new", sens_published_at=datetime(2026, 11, 1, 9),
    ))
    db_session.commit()
    monkeypatch.setattr(ipo, "YFinanceCollector", lambda: _yf({
        "NEW": _bars("2026-11-18", "2026-11-19"),
    }))

    update_ipo_listings(db_session, collector=_collector([]))

    assert db_session.query(UpcomingListing).one().status == "listed"
    assert db_session.query(Notification).filter(
        Notification.title.like("Now trading%")).count() == 1


def test_old_announcements_expire_without_probe(db_session, monkeypatch):
    db_session.add(UpcomingListing(
        symbol="GONE", headline="Intention to list", source_url="https://example.com/gone",
        sens_published_at=datetime.now() - ipo.ANNOUNCEMENT_EXPIRY - pd.Timedelta(days=1),
    ))
    db_session.commit()
    calls = []
    monkeypatch.setattr(ipo, "YFinanceCollector", lambda: _yf({}, calls))

    update_ipo_listings(db_session, collector=_collector([]))

    assert db_session.query(UpcomingListing).one().status == "expired"
    assert calls == []
