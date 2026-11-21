"""Tests for Yahoo energy/agriculture futures and the categorised Commodities page."""
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from app.dashboard.commodities import CATEGORY_ORDER, build_price_rows, commodity_catalog
from app.models.stock import Stock, StockPrice
from app.services.commodity_futures import FUTURES, refresh_futures, save_zar_closes, zar_closes


def test_pseudo_symbols_fit_the_symbol_columns():
    """Postgres enforces VARCHAR(10) on symbol columns (SQLite, used by the
    tests, does not) - YF_GASOLINE crashed the page on Postgres."""
    limit = min(Stock.__table__.c.symbol.type.length, StockPrice.__table__.c.symbol.type.length)
    for db_symbol in (c["db_symbol"] for c in commodity_catalog().values()):
        assert len(db_symbol) <= limit, db_symbol


def test_save_failure_is_reported_and_rolled_back(db_session, monkeypatch):
    from sqlalchemy.exc import DataError

    from app.services import commodity_futures

    def boom(db, series):
        raise DataError("INSERT", {}, Exception("value too long"))

    monkeypatch.setattr(commodity_futures, "save_zar_closes", boom)
    collector = SimpleNamespace(get_futures_closes=lambda tickers, period: _frame(
        {"CL=F": [80.0], "ZAR=X": [18.0]}, ["2026-11-20"]
    ))

    assert refresh_futures(db_session, collector) is None
    db_session.query(Stock).count()  # session still usable after rollback


def _frame(rows: dict, index):
    return pd.DataFrame(rows, index=pd.DatetimeIndex(index))


def test_usd_quotes_convert_at_that_days_rate():
    closes = _frame({"CL=F": [80.0, 82.0], "ZAR=X": [18.0, 17.5]}, ["2026-11-19", "2026-11-20"])

    bars = zar_closes(closes)["YF_WTI"]

    assert bars[datetime(2026, 11, 19)] == pytest.approx(80.0 * 18.0)
    assert bars[datetime(2026, 11, 20)] == pytest.approx(82.0 * 17.5)


def test_us_cent_quotes_are_scaled_to_dollars():
    """Yahoo quotes grains/softs/livestock in USX (US cents) - wheat at
    574.25 is $5.7425/bushel, not $574."""
    closes = _frame({"ZW=F": [574.25], "ZAR=X": [17.6]}, ["2026-11-20"])

    assert zar_closes(closes)["YF_WHEAT"][datetime(2026, 11, 20)] == pytest.approx(5.7425 * 17.6)


def test_fx_gaps_are_forward_filled_and_missing_closes_skipped():
    closes = _frame(
        {"NG=F": [3.0, 3.2, float("nan")], "ZAR=X": [18.0, float("nan"), 18.2]},
        ["2026-11-18", "2026-11-19", "2026-11-20"],
    )

    bars = zar_closes(closes)["YF_NATGAS"]

    assert bars == {
        datetime(2026, 11, 18): pytest.approx(54.0),
        datetime(2026, 11, 19): pytest.approx(3.2 * 18.0),
    }


def test_refetch_overwrites_bars_in_place(db_session):
    day = datetime(2026, 11, 20)
    assert save_zar_closes(db_session, {"YF_WTI": {day: 1400.0}}) == 1
    assert save_zar_closes(db_session, {"YF_WTI": {day: 1450.0}}) == 1

    rows = db_session.query(StockPrice).filter(StockPrice.symbol == "YF_WTI").all()
    assert len(rows) == 1 and rows[0].close_price == pytest.approx(1450.0)
    assert db_session.query(Stock).filter(Stock.symbol == "YF_WTI").one().name == "WTI Crude Oil"


def test_refresh_backfills_a_year_then_goes_incremental(db_session):
    periods = []

    def fake_closes(tickers, period):
        periods.append(period)
        return _frame({t: [10.0] for t in tickers} | {"ZAR=X": [18.0]}, [datetime.now().date()])

    collector = SimpleNamespace(get_futures_closes=fake_closes)
    assert refresh_futures(db_session, collector) == len(FUTURES)
    refresh_futures(db_session, collector)

    assert periods == ["1y", "1mo"]


def test_refresh_reports_failure_as_none(db_session):
    collector = SimpleNamespace(get_futures_closes=lambda tickers, period: None)
    assert refresh_futures(db_session, collector) is None


def test_every_commodity_has_a_known_category():
    assert {c["category"] for c in commodity_catalog().values()} <= set(CATEGORY_ORDER)


def test_rows_are_grouped_by_category_with_change():
    snap = lambda close: SimpleNamespace(close_price=close)  # noqa: E731
    snapshots = {
        "YF_WTI": (snap(1500.0), snap(1400.0)),
        "YF_CORN": (snap(77.0), None),
        "MD_ALUM": (snap(49_400.0), None),
    }

    rows = build_price_rows({"gold": 2000.0}, snapshots)

    assert rows["Energy"][0]["Price (R)"] == pytest.approx(1500.0)
    assert rows["Energy"][0]["Change %"] == pytest.approx(100 / 14)
    assert rows["Agriculture"][0]["Unit"] == "per bushel"
    assert rows["Precious Metals"][0]["Source"] == "Metals.dev"
    assert rows["Industrial Metals"][0]["Price (R)"] == pytest.approx(49_400.0)


def test_copper_is_shown_per_tonne_with_unchanged_change_pct():
    """Copper is stored per gram but sits in the per-tonne table; the
    display scaling must not distort its day change."""
    snap = lambda close: SimpleNamespace(close_price=close)  # noqa: E731
    rows = build_price_rows({}, {"AV_COPPER": (snap(0.22), snap(0.20))})

    row = rows["Industrial Metals"][0]
    assert row["Price (R)"] == pytest.approx(220_000.0)
    assert row["Unit"] == "per tonne"
    assert row["Change %"] == pytest.approx(10.0)
