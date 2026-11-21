"""SENS announcements for held shares: parsing, storing, alerting, status."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.collectors.moneyweb import parse_sens_rows
from app.models.base import utcnow_naive
from app.models.notification import Notification
from app.models.sens_announcement import SensAnnouncement
from app.services import holding_sens
from app.services.holding_sens import holding_statuses, is_corporate_action, refresh_holding_announcements

# Trimmed copy of Moneyweb's company page for SGP (2026-11-22).
_COMPANY_PAGE = """
<div class="sens-row"><div class="sens-row-date"><time datetime="2026-09-30T15:10:00+02:00">30.09.26</time></div>
<div class="sens-row-content"><a href="https://www.moneyweb.co.za/mny_sens/sail-quarterly-suspension-update-8/">
<strong>SAIL MINING GROUP LIMITED</strong> – Quarterly suspension update</a></div></div>
<div class="sens-row"><div class="sens-row-date"><time datetime="2025-12-12T15:00:00+02:00">12.12.25</time></div>
<div class="sens-row-content"><a href="https://www.moneyweb.co.za/mny_sens/sail-proposed-repurchase-offer-and-delisting/">
<strong>SAIL MINING GROUP LIMITED</strong> – Proposed Repurchase Offer and Delisting of Sail</a></div></div>
"""


def _row(url, headline, days_ago, symbol="SGP"):
    return {"source_url": url, "headline": headline, "name": "SAIL MINING GROUP LIMITED",
            "symbol": symbol, "published_at": datetime.now(timezone.utc) - timedelta(days=days_ago)}


def _collector(pages):
    calls = []
    def get_company_sens(symbol):
        calls.append(symbol)
        return pages.get(symbol)
    return SimpleNamespace(get_company_sens=get_company_sens, calls=calls)


def test_company_page_rows_get_the_page_symbol():
    rows = parse_sens_rows(_COMPANY_PAGE, default_symbol="SGP")
    assert [r["symbol"] for r in rows] == ["SGP", "SGP"]
    assert rows[0]["headline"] == "Quarterly suspension update"
    assert rows[1]["published_at"] == datetime(2025, 12, 12, 15, tzinfo=timezone(timedelta(hours=2)))


def test_corporate_action_phrases():
    assert is_corporate_action("Proposed Repurchase Offer and Delisting of Sail")
    assert is_corporate_action("Distribution of circular and notice of general meeting")
    assert not is_corporate_action("Notice of general meeting")  # routine on its own
    assert not is_corporate_action("Quarterly suspension update")
    assert not is_corporate_action("Dealings in securities by a director")


def test_first_fetch_backfills_history_but_alerts_only_recent(db_session):
    pages = {"SGP": [
        _row("u/old-offer", "Proposed Repurchase Offer and Delisting of Sail", 340),
        _row("u/new", "Quarterly suspension update", 1),
    ]}
    assert refresh_holding_announcements(db_session, _collector(pages), symbols=["SGP"], spacing=0) == 2
    assert db_session.query(SensAnnouncement).count() == 2
    notes = db_session.query(Notification).all()
    assert [n.title for n in notes] == ["SENS SGP: Quarterly suspension update"]
    assert notes[0].type == "sens"


def test_later_runs_alert_every_new_announcement_and_flag_actions(db_session):
    first = {"SGP": [_row("u/a", "Quarterly suspension update", 60)]}
    refresh_holding_announcements(db_session, _collector(first), symbols=["SGP"], spacing=0)
    later = {"SGP": [_row("u/a", "Quarterly suspension update", 60),
                     _row("u/b", "Revised timetable for the repurchase offer and delisting", 10)]}
    assert refresh_holding_announcements(db_session, _collector(later), symbols=["SGP"], spacing=0) == 1
    note = db_session.query(Notification).one()
    assert note.title.startswith("Action may be needed - SGP:")
    assert "u/b" in note.message


def test_all_fetches_failing_is_none_and_recorded(db_session):
    from app.services.source_health import source_statuses
    assert refresh_holding_announcements(db_session, _collector({}), symbols=["SGP", "ISO"], spacing=0) is None
    status = next(s for s in source_statuses(db_session) if s.key == "moneyweb_company_sens")
    assert not status.ok and "2/2" in status.last_detail


def _store(db, symbol, headline, days_ago, url):
    db.add(SensAnnouncement(symbol=symbol, headline=headline, source_url=url,
                            published_at=utcnow_naive() - timedelta(days=days_ago)))
    db.commit()


def test_status_suspended_with_pending_corporate_action(db_session):
    _store(db_session, "SGP", "Proposed Repurchase Offer and Delisting of Sail", 345, "u/1")
    _store(db_session, "SGP", "Quarterly suspension update", 53, "u/2")
    _store(db_session, "SGP", "Changes to the board of directors", 61, "u/3")
    status = holding_statuses(db_session, ["SGP"])["SGP"]
    assert status.suspended
    assert status.suspension.headline == "Quarterly suspension update"
    assert status.corporate_action.headline.startswith("Proposed Repurchase Offer")
    assert status.latest.headline == "Quarterly suspension update"


def test_mine_operations_and_suspensive_conditions_are_not_trading_suspensions(db_session):
    _store(db_session, "WEZ", "Operational update: temporary suspension of operations at Bakubung Platinum Mine", 5, "u/w")
    _store(db_session, "ENX", "Fulfillment of the suspensive conditions and revised salient dates", 5, "u/e")
    statuses = holding_statuses(db_session, ["WEZ", "ENX"])
    assert not statuses["WEZ"].suspended and not statuses["ENX"].suspended


def test_routine_agms_are_not_corporate_actions():
    assert not is_corporate_action("Results of Annual General Meeting and Salient Dividend Dates")
    assert not is_corporate_action("Notice of Annual General Meeting 2026")
    assert is_corporate_action("Distribution of circular and notice of general meeting")


def test_a_later_results_announcement_concludes_the_action(db_session):
    """RMH: 'Odd-lot Offer Price' (27 Oct) is closed by 'Results of the Odd-lot Offer' (16 Nov)."""
    _store(db_session, "RMH", "Odd-lot Offer Price and Dividends Tax", 26, "u/r1")
    _store(db_session, "RMH", "Results of the Odd-lot Offer", 6, "u/r2")
    _store(db_session, "APF", "Distribution of circular and notice of general meeting", 9, "u/p1")
    statuses = holding_statuses(db_session, ["RMH", "APF"])
    assert statuses["RMH"].corporate_action is None
    assert statuses["APF"].corporate_action.headline.startswith("Distribution of circular")


def test_suspension_lifted_or_stale_is_not_suspended(db_session):
    _store(db_session, "AAA", "Quarterly suspension update", 90, "u/a1")
    _store(db_session, "AAA", "Lifting of suspension of trading", 10, "u/a2")
    _store(db_session, "BBB", "Quarterly suspension update", 400, "u/b1")
    statuses = holding_statuses(db_session, ["AAA", "BBB", "CCC"])
    assert not statuses["AAA"].suspended
    assert not statuses["BBB"].suspended
    assert "CCC" not in statuses


def test_held_symbols_skip_pseudo_symbols(db_session, monkeypatch):
    monkeypatch.setattr("app.services.penny_scan.held_jse_symbols", lambda db: {"SGP", "^J203.JO", "AV_GOLD", "ISO"})
    assert holding_sens.held_sens_symbols(db_session) == ["ISO", "SGP"]
