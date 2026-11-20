"""Tests for the near-52-week-low penny stock alerts (services/penny_scan)."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.models.base import utcnow_naive
from app.models.notification import Notification
from app.services import penny_scan
from app.services.penny_scan import (
    check_near_low_alerts,
    near_low_candidates,
    near_low_title,
    run_near_low_alerts,
)


@pytest.fixture(autouse=True)
def quiet_notifier(monkeypatch):
    """No Apprise channels, no external sends - whatever .env says."""
    monkeypatch.setattr(penny_scan.NotificationService, "_setup_notification_channels", lambda self: None)
    sent = []
    monkeypatch.setattr(
        penny_scan.NotificationService, "send_pending_notifications", lambda self: sent.append(True)
    )
    return sent


def _row(symbol="GPL", price=1.40, low=1.36, high=2.44, sessions=0, **kw):
    row = {
        "symbol": symbol, "price": price, "low_52w": low, "high_52w": high,
        "off_low": (price - low) / low * 100, "sessions_since_trade": sessions,
        "last_trade": "2026-11-19", "traded_value": 3_668.0, "liquidity_penalty": 30,
    }
    row.update(kw)
    return row


def test_scannable_db_stocks_skips_non_jse_symbols(db_session):
    from app.models.stock import Stock
    for symbol in ("SOL", "NG=F", "GC=F", "XAU_ZAR", "^J203.JO", "ZAR=X", "OLD"):
        db_session.add(Stock(symbol=symbol, name=symbol, is_active=symbol != "OLD"))
    db_session.commit()
    stocks = penny_scan.scannable_db_stocks(db_session)
    assert "SOL" in stocks
    assert not {"NG=F", "GC=F", "XAU_ZAR", "^J203.JO", "ZAR=X", "OLD"} & set(stocks)


def test_candidates_within_threshold_and_not_stale():
    near = _row("GPL", price=1.40)            # 2.9% above low
    far = _row("CLH", price=4.32, low=2.80)   # 54% above
    stale = _row("PMV", price=2.55, low=2.50, sessions=7)
    dead = _row("VUN", price=1.05, low=1.05, sessions=None)
    picked = near_low_candidates([near, far, stale, dead], 10.0)
    assert [r["symbol"] for r in picked] == ["GPL"]


def test_alert_message_and_metadata(db_session, quiet_notifier):
    created = check_near_low_alerts(db_session, [_row()], 10.0, {"GPL": "Grand Parade Investments"})
    assert created == 1 and quiet_notifier  # external delivery attempted

    note = db_session.query(Notification).filter_by(stock_symbol="GPL").one()
    assert note.type == "price" and note.title == near_low_title("GPL")
    assert "Grand Parade Investments (GPL) closed at R 1.40 on 2026-11-19" in note.message
    assert "2.9% above its 52-week low of R 1.36" in note.message
    assert "thinly traded" in note.message
    assert '"alert": "near_52w_low"' in note.meta_data


def test_at_the_low_and_sub_rand_wording(db_session):
    check_near_low_alerts(db_session, [_row("SKA", price=0.0085, low=0.0085, high=0.02,
                                            liquidity_penalty=0)], 10.0)
    msg = db_session.query(Notification).filter_by(stock_symbol="SKA").one().message
    assert "closed at R 0.0085" in msg and "at its 52-week low of R 0.0085" in msg
    assert "thinly traded" not in msg


def test_cooldown_suppresses_repeats_then_rearms(db_session):
    assert check_near_low_alerts(db_session, [_row()], 10.0) == 1
    assert check_near_low_alerts(db_session, [_row()], 10.0) == 0  # tomorrow, still near

    note = db_session.query(Notification).filter_by(stock_symbol="GPL").one()
    note.created_at = utcnow_naive() - penny_scan.NEAR_LOW_COOLDOWN - timedelta(days=1)
    db_session.commit()
    assert check_near_low_alerts(db_session, [_row()], 10.0) == 1


def test_run_is_a_noop_when_disabled(db_session, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not scan")
    monkeypatch.setattr(penny_scan, "scan_penny_stocks", boom)
    monkeypatch.setattr(penny_scan, "settings", SimpleNamespace(
        penny_low_alert_enabled=False, yfinance_enabled=True, penny_low_alert_pct=10.0))
    assert run_near_low_alerts(db_session) == 0


def test_run_scans_and_alerts(db_session, monkeypatch):
    monkeypatch.setattr(penny_scan, "settings", SimpleNamespace(
        penny_low_alert_enabled=True, yfinance_enabled=True, penny_low_alert_pct=5.0))
    scanned = {}

    def fake_scan(extra):
        scanned["extra"] = extra
        return [_row("GPL", price=1.40), _row("HUG", price=2.40, low=0.85)], None, None, []
    monkeypatch.setattr(penny_scan, "scan_penny_stocks", fake_scan)

    assert run_near_low_alerts(db_session) == 1
    note = db_session.query(Notification).one()
    assert note.stock_symbol == "GPL" and "Grand Parade Investments" in note.message
    assert all(s not in penny_scan.JSE_PENNY_CANDIDATES for s in scanned["extra"])


def test_run_raises_on_scan_failure(db_session, monkeypatch):
    monkeypatch.setattr(penny_scan, "settings", SimpleNamespace(
        penny_low_alert_enabled=True, yfinance_enabled=True, penny_low_alert_pct=10.0))
    monkeypatch.setattr(penny_scan, "scan_penny_stocks", lambda extra: ([], None, "Yahoo down", []))
    with pytest.raises(RuntimeError, match="Yahoo down"):
        run_near_low_alerts(db_session)
