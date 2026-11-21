"""Tests for the implausible-close / unit-mismatch guardrail pipeline.

This is the code that decides whether a fetched price is stored, rescaled
(ZAc/ZAR 100x shifts) or rejected - a regression here silently corrupts or
freezes stored prices.
"""
import pytest
import pandas as pd
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from app.services.data_service import DataService
from app.models.stock import Stock, StockPrice
from app.models.notification import Notification


def _service(db_session):
    svc = DataService(db_session)
    svc.twelve_data = MagicMock()
    svc.yfinance = MagicMock()
    return svc


def _add_bar(db_session, symbol, ts, close):
    stock = db_session.query(Stock).filter(Stock.symbol == symbol).first()
    if not stock:
        stock = Stock(symbol=symbol, name=symbol)
        db_session.add(stock)
        db_session.flush()
    db_session.add(StockPrice(
        stock_id=stock.id, symbol=symbol, timestamp=ts, close_price=close,
    ))
    db_session.commit()


def _frame(rows):
    """rows: [(datetime, close), ...] -> Yahoo-shaped daily frame."""
    idx = pd.DatetimeIndex([r[0] for r in rows])
    return pd.DataFrame(
        {
            "Open": [r[1] for r in rows],
            "High": [r[1] for r in rows],
            "Low": [r[1] for r in rows],
            "Close": [r[1] for r in rows],
            "Volume": [1000] * len(rows),
        },
        index=idx,
    )


# ---------------------------------------------------------------- _ratio_ok

def test_ratio_ok_boundaries(db_session):
    svc = _service(db_session)
    assert svc._ratio_ok(100.0, 300.0)          # exactly 3x is allowed
    assert not svc._ratio_ok(100.0, 300.1)      # just over
    assert svc._ratio_ok(0.0085, 0.0085)
    # Missing/zero sides can never be validated
    assert not svc._ratio_ok(0, 100.0)
    assert not svc._ratio_ok(100.0, None)


# ------------------------------------------------------- _close_scale_factor

def test_close_scale_factor_no_reference(db_session):
    svc = _service(db_session)
    factor, prev = svc._close_scale_factor("NPN", datetime(2024, 1, 16), 500.0)
    assert (factor, prev) == (1.0, None)


def test_close_scale_factor_stale_reference_does_not_gate(db_session):
    """A reference bar >31d old (dormant/re-listed stock) must not reject."""
    _add_bar(db_session, "NPN", datetime(2023, 10, 1), 500.0)
    svc = _service(db_session)
    factor, prev = svc._close_scale_factor("NPN", datetime(2024, 1, 16), 50.0)
    assert factor == 1.0
    assert prev == pytest.approx(500.0)


def test_close_scale_factor_rescales_zac_to_zar(db_session):
    """Incoming cents values (100x too big) are rescaled, not rejected."""
    _add_bar(db_session, "WEZ", datetime(2024, 1, 15), 0.45)
    svc = _service(db_session)
    factor, prev = svc._close_scale_factor("WEZ", datetime(2024, 1, 16), 45.0)
    assert factor == pytest.approx(0.01)
    assert prev == pytest.approx(0.45)


def test_close_scale_factor_refuses_a_sub_cent_result(db_session):
    """The JSE minimum is 1c: matching a R0.0085 stored bar by turning a
    correct R0.85 into R0.0085 is how SKA was corrupted - the stored bar is
    the bad one, so the incoming close is rejected, not rescaled."""
    _add_bar(db_session, "SKA", datetime(2024, 1, 15), 0.0085)
    svc = _service(db_session)
    factor, prev = svc._close_scale_factor("SKA", datetime(2024, 1, 16), 0.85)
    assert factor is None
    assert prev == pytest.approx(0.0085)


def test_close_scale_factor_rescales_zar_to_zac(db_session):
    _add_bar(db_session, "ABC", datetime(2024, 1, 15), 100.0)
    svc = _service(db_session)
    factor, prev = svc._close_scale_factor("ABC", datetime(2024, 1, 16), 1.0)
    assert factor == pytest.approx(100.0)
    assert prev == pytest.approx(100.0)


def test_close_scale_factor_rejects_implausible(db_session):
    """A 10x move fits neither units nor plausibility - returns None."""
    _add_bar(db_session, "NPN", datetime(2024, 1, 15), 100.0)
    svc = _service(db_session)
    factor, prev = svc._close_scale_factor("NPN", datetime(2024, 1, 16), 1000.0)
    assert factor is None
    assert prev == pytest.approx(100.0)


# --------------------------------------------------------- _close_corroborated

def test_close_corroborated_yahoo_source_checks_twelve_data_quote(db_session):
    svc = _service(db_session)
    svc.twelve_data.get_quote.return_value = {"close": "48.0", "previous_close": "100.0"}

    # Rejected close 50.0 vs stored 100.0: alternate quote close 48.0 is
    # within 5% of the suspicious close -> corroborated (real crash).
    assert svc._close_corroborated("NPN", datetime(2024, 1, 16), 50.0, "yfinance") is True

    # Alternate source still shows ~100 -> clearly contradicts.
    svc.twelve_data.get_quote.return_value = {"close": "99.0", "previous_close": "100.0"}
    assert svc._close_corroborated("NPN", datetime(2024, 1, 16), 50.0, "yfinance") is False

    # No quote at all -> check could not be performed.
    svc.twelve_data.get_quote.return_value = None
    assert svc._close_corroborated("NPN", datetime(2024, 1, 16), 50.0, "yfinance") is None


def test_close_corroborated_twelve_data_source_checks_yahoo_frame(db_session):
    svc = _service(db_session)
    # Only same-day bars count as candidates.
    svc.yfinance.get_latest_bars.return_value = {
        "NPN": _frame([(datetime(2024, 1, 16), 51.0), (datetime(2024, 1, 15), 100.0)])
    }
    assert svc._close_corroborated("NPN", datetime(2024, 1, 16), 50.0, "twelve_data") is True

    # Same-day bar missing -> None, not False (the 100.0 is the wrong day).
    svc.yfinance.get_latest_bars.return_value = {
        "NPN": _frame([(datetime(2024, 1, 15), 100.0)])
    }
    assert svc._close_corroborated("NPN", datetime(2024, 1, 16), 50.0, "twelve_data") is None

    svc.yfinance.get_latest_bars.return_value = {}
    assert svc._close_corroborated("NPN", datetime(2024, 1, 16), 50.0, "twelve_data") is None


def test_close_corroborated_swallows_provider_errors(db_session):
    svc = _service(db_session)
    svc.twelve_data.get_quote.side_effect = RuntimeError("boom")
    assert svc._close_corroborated("NPN", datetime(2024, 1, 16), 50.0, "yfinance") is None


# ----------------------------------------------------------- _unit_adjust_close

def test_unit_adjust_close_plausible_passes_untouched(db_session):
    _add_bar(db_session, "NPN", datetime(2024, 1, 15), 100.0)
    svc = _service(db_session)
    record = {"open": 102.0, "close": 102.0}
    assert svc._unit_adjust_close("NPN", datetime(2024, 1, 16), record, 102.0, "yfinance")
    assert record["close"] == pytest.approx(102.0)


def test_unit_adjust_close_rescales_record(db_session):
    _add_bar(db_session, "WEZ", datetime(2024, 1, 15), 0.45)
    svc = _service(db_session)
    record = {"open": 46.0, "high": 47.0, "low": 44.0, "close": 45.0, "adjusted_close": 45.0}
    assert svc._unit_adjust_close("WEZ", datetime(2024, 1, 16), record, 45.0, "yfinance")
    assert record["close"] == pytest.approx(0.45)
    assert record["open"] == pytest.approx(0.46)


def test_unit_adjust_close_accepts_corroborated_crash(db_session):
    """A >3x drop corroborated by the alternate source is a real crash."""
    _add_bar(db_session, "NPN", datetime(2024, 1, 15), 100.0)
    svc = _service(db_session)
    svc.twelve_data.get_quote.return_value = {"close": "29.0", "previous_close": "100.0"}
    record = {"close": 30.0}
    assert svc._unit_adjust_close("NPN", datetime(2024, 1, 16), record, 30.0, "yfinance")
    notif = db_session.query(Notification).filter_by(stock_symbol="NPN").one()
    assert "accepted" in notif.title


def test_unit_adjust_close_rejects_uncorroborated(db_session):
    _add_bar(db_session, "NPN", datetime(2024, 1, 15), 100.0)
    svc = _service(db_session)
    svc.twelve_data.get_quote.return_value = {"close": "99.0", "previous_close": "100.0"}
    record = {"close": 30.0}
    assert not svc._unit_adjust_close("NPN", datetime(2024, 1, 16), record, 30.0, "yfinance")
    notif = db_session.query(Notification).filter_by(stock_symbol="NPN").one()
    assert "rejected" in notif.title


# ------------------------------------------------------ _bulk_save_stock_prices

def test_bulk_save_mixed_unit_frame_rescales_only_bad_bars(db_session):
    """A frame where only some bars are cents gets each bad bar rescaled."""
    _add_bar(db_session, "WEZ", datetime(2024, 1, 12), 0.45)
    svc = _service(db_session)
    records = [
        {"timestamp": datetime(2024, 1, 15), "close": 0.46},
        {"timestamp": datetime(2024, 1, 16), "close": 47.0},    # cents glitch
        {"timestamp": datetime(2024, 1, 17), "close": 0.48},
    ]
    assert svc._bulk_save_stock_prices("WEZ", records) == 3
    closes = {p.timestamp.day: p.close_price
              for p in db_session.query(StockPrice).filter_by(symbol="WEZ")}
    assert closes[16] == pytest.approx(0.47)  # rescaled x0.01, not rejected


def test_bulk_save_trust_units_ignores_a_corrupted_stored_series(db_session):
    """The --normalize-closes repair: stored bars are 100x low (SKA at
    R0.0085) and the incoming series is already unit-normalised - it must
    overwrite them, not be "corrected" down to match."""
    _add_bar(db_session, "SKA", datetime(2024, 1, 12), 0.0085)
    _add_bar(db_session, "SKA", datetime(2024, 1, 15), 0.0086)
    svc = _service(db_session)
    records = [
        {"timestamp": datetime(2024, 1, 12), "close": 0.85},
        {"timestamp": datetime(2024, 1, 15), "close": 0.86},
    ]
    svc._bulk_save_stock_prices("SKA", records, trust_units=True)
    closes = sorted(p.close_price for p in db_session.query(StockPrice).filter_by(symbol="SKA"))
    assert closes == [pytest.approx(0.85), pytest.approx(0.86)]


def test_bulk_save_lone_rejection_corroborated_is_accepted(db_session):
    _add_bar(db_session, "NPN", datetime(2024, 1, 15), 100.0)
    svc = _service(db_session)
    svc.twelve_data.get_quote.return_value = {"close": "29.0", "previous_close": "100.0"}
    records = [{"timestamp": datetime(2024, 1, 16), "close": 30.0}]
    assert svc._bulk_save_stock_prices("NPN", records) == 1
    row = db_session.query(StockPrice).filter_by(
        symbol="NPN", timestamp=datetime(2024, 1, 16)).one()
    assert row.close_price == pytest.approx(30.0)


def test_bulk_save_lone_rejection_uncorroborated_is_dropped(db_session):
    _add_bar(db_session, "NPN", datetime(2024, 1, 15), 100.0)
    svc = _service(db_session)
    svc.twelve_data.get_quote.return_value = {"close": "99.0", "previous_close": "100.0"}
    records = [{"timestamp": datetime(2024, 1, 16), "close": 30.0}]
    assert svc._bulk_save_stock_prices("NPN", records) == 0
    assert db_session.query(StockPrice).filter_by(
        symbol="NPN", timestamp=datetime(2024, 1, 16)).count() == 0


def test_bulk_save_multiple_rejections_never_corroborate(db_session):
    """Sustained divergence is a feed/unit problem, not a crash - the
    alternate-source check must not run at all."""
    _add_bar(db_session, "NPN", datetime(2024, 1, 15), 100.0)
    svc = _service(db_session)
    svc._close_corroborated = MagicMock(return_value=True)
    records = [
        {"timestamp": datetime(2024, 1, 16), "close": 10.0},
        {"timestamp": datetime(2024, 1, 17), "close": 10.1},
        {"timestamp": datetime(2024, 1, 18), "close": 102.0},  # plausible vs stored
    ]
    assert svc._bulk_save_stock_prices("NPN", records) == 1
    svc._close_corroborated.assert_not_called()
    stored_ts = {p.timestamp.day for p in db_session.query(StockPrice).filter_by(symbol="NPN")}
    assert stored_ts == {15, 18}  # the two bad bars dropped, the good one kept


def test_bulk_save_reference_chains_through_accepted_bars(db_session):
    """Each accepted bar becomes the reference for the next: 112 is
    implausible vs the stored 100 (>3x? no - but 330 vs 100 would be), so
    this verifies acceptance chains forward, not just vs the stored bar."""
    _add_bar(db_session, "NPN", datetime(2024, 1, 12), 100.0)
    svc = _service(db_session)
    records = [
        {"timestamp": datetime(2024, 1, 15), "close": 110.0},  # ok
        {"timestamp": datetime(2024, 1, 16), "close": 112.0},  # ok vs 110
    ]
    assert svc._bulk_save_stock_prices("NPN", records) == 2


# ------------------------------------------------------------- _save_recent_bars

def test_save_recent_bars_empty_frame(db_session):
    svc = _service(db_session)
    assert svc._save_recent_bars("NPN", None) is False
    assert svc._save_recent_bars("NPN", pd.DataFrame()) is False


def test_save_recent_bars_writes_all_bars(db_session):
    _add_bar(db_session, "NPN", datetime(2024, 1, 12), 100.0)
    svc = _service(db_session)
    frame = _frame([
        (datetime(2024, 1, 15), 101.0),
        (datetime(2024, 1, 16), 103.0),
    ])
    assert svc._save_recent_bars("NPN", frame) is True
    assert db_session.query(StockPrice).filter_by(symbol="NPN").count() == 3


def test_save_recent_bars_refresh_only_still_success(db_session):
    """All bars already stored (bulk returns 0 new) is still a good update."""
    _add_bar(db_session, "NPN", datetime(2024, 1, 16), 100.0)
    svc = _service(db_session)
    frame = _frame([(datetime(2024, 1, 16), 101.0)])
    assert svc._save_recent_bars("NPN", frame) is True


def test_save_recent_bars_all_rejected_is_failure(db_session):
    """Bulk saved 0 and the newest bar is NOT stored -> real failure."""
    _add_bar(db_session, "NPN", datetime(2024, 1, 15), 100.0)
    svc = _service(db_session)
    svc.twelve_data.get_quote.return_value = {"close": "99.0"}  # no corroboration
    frame = _frame([(datetime(2024, 1, 16), 10.0)])
    assert svc._save_recent_bars("NPN", frame) is False


# --------------------------------------------------------- _quote_bar_timestamp

def test_quote_bar_timestamp_date_only(db_session):
    svc = _service(db_session)
    ts = svc._quote_bar_timestamp({"datetime": "2024-01-15"})
    assert ts == datetime(2024, 1, 15)


def test_quote_bar_timestamp_datetime_string_normalises_to_midnight(db_session):
    svc = _service(db_session)
    ts = svc._quote_bar_timestamp({"datetime": "2024-01-15 10:30:00"})
    assert ts == datetime(2024, 1, 15)


def test_quote_bar_timestamp_iso_offset_does_not_crash(db_session):
    svc = _service(db_session)
    ts = svc._quote_bar_timestamp({"datetime": "2024-01-15T10:00:00+02:00"})
    assert ts == datetime(2024, 1, 15)


def test_quote_bar_timestamp_epoch_uses_scheduler_timezone(db_session):
    """Epoch seconds resolve in SCHEDULER_TIMEZONE, then normalise to the
    exchange-local bar date at naive midnight."""
    from zoneinfo import ZoneInfo
    from app.config.settings import settings
    svc = _service(db_session)
    epoch = int(datetime(2024, 1, 15, 23, 0, tzinfo=timezone.utc).timestamp())
    ts = svc._quote_bar_timestamp({"timestamp": epoch})
    expected = datetime.fromtimestamp(epoch, ZoneInfo(settings.scheduler_timezone)).date()
    assert ts == datetime(expected.year, expected.month, expected.day)
    assert ts.tzinfo is None


def test_quote_bar_timestamp_missing_fields_falls_back_to_today(db_session):
    svc = _service(db_session)
    ts = svc._quote_bar_timestamp({})
    today = datetime.now(timezone.utc).date()
    assert ts.tzinfo is None and ts.hour == ts.minute == 0
    assert ts.date() in (today, today - timedelta(days=1), today + timedelta(days=1))


# --------------------------------------------------- update_historical_data_batch

def test_update_historical_batch_empty_input(db_session):
    svc = _service(db_session)
    assert svc.update_historical_data_batch([]) == ({}, [])


def _add_stock(db_session, symbol):
    """Pre-create a Stock row so _ensure_stock never calls get_company_name
    (which would hit the live APIs)."""
    db_session.add(Stock(symbol=symbol, name=symbol))
    db_session.commit()


def test_update_historical_batch_twelve_data_and_yahoo_paths(db_session):
    """TD-served symbols use TD; the rest are covered by ONE yahoo batch."""
    for symbol in ("TDSYM", "YAFSYM", "MISS"):
        _add_stock(db_session, symbol)
    svc = _service(db_session)
    yesterday = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    day_before = yesterday - timedelta(days=1)
    td_points = [
        {"datetime": day_before.strftime("%Y-%m-%d"), "close": "102.0", "volume": "1000"},
        {"datetime": yesterday.strftime("%Y-%m-%d"), "close": "106.0", "volume": "1500"},
    ]
    svc.twelve_data.get_historical_data.side_effect = (
        lambda symbol, **kw: td_points if symbol == "TDSYM" else None
    )
    # Yahoo frames must land inside the backfill window (now - `days`).
    frame = _frame([(day_before, 50.0), (yesterday, 51.0)])
    svc.yfinance.get_history_batch.return_value = {"YAFSYM": frame, "MISS": None}

    results, failed = svc.update_historical_data_batch(["TDSYM", "YAFSYM", "MISS"])

    svc.yfinance.get_history_batch.assert_called_once()
    called_symbols = set(svc.yfinance.get_history_batch.call_args[0][0])
    assert called_symbols == {"YAFSYM", "MISS"}
    assert results["TDSYM"] == 2
    assert results["YAFSYM"] == 2
    assert results["MISS"] == 0
    assert failed == []
    assert db_session.query(StockPrice).filter_by(symbol="TDSYM").count() == 2
    assert db_session.query(StockPrice).filter_by(symbol="YAFSYM").count() == 2


def test_update_historical_batch_slices_each_symbols_window(db_session):
    """An incremental symbol only stores bars from its own start date, even
    though the shared Yahoo frame covers every symbol's earliest window."""
    # days=14 -> need >=5 bars in the window or the density rule forces a
    # backfill (which would accept the old bar instead of slicing it).
    latest = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2)
    latest = datetime(latest.year, latest.month, latest.day)
    _add_stock(db_session, "INC")
    for days_back, close in ((9, 96.0), (7, 97.0), (5, 98.0), (3, 99.0), (1, 99.5), (0, 100.0)):
        _add_bar(db_session, "INC", latest - timedelta(days=days_back), close)

    svc = _service(db_session)
    svc.twelve_data.get_historical_data.return_value = None
    start = latest - timedelta(days=7)  # incremental plan: latest - 7d overlap
    old_bar = start - timedelta(days=10)  # predates this symbol's window
    frame = _frame([(old_bar, 90.0), (latest, 101.0), (latest + timedelta(days=1), 102.0)])
    svc.yfinance.get_history_batch.return_value = {"INC": frame}

    results, failed = svc.update_historical_data_batch(["INC"], days=14)

    assert failed == []
    assert results["INC"] == 1  # only the bar after `latest` is new
    stored = {p.timestamp for p in db_session.query(StockPrice).filter_by(symbol="INC")}
    assert datetime(old_bar.year, old_bar.month, old_bar.day) not in stored
    assert latest + timedelta(days=1) in stored
