"""Tests for the Commodities page's Metals.dev snapshot persistence."""
from datetime import datetime

import pytest

from app.dashboard.commodities import _save_historical_prices, _save_latest_rates
from app.models.stock import Stock, StockPrice


def _close(db, symbol):
    return db.query(StockPrice.close_price).filter(StockPrice.symbol == symbol).one()[0]


def test_base_metals_are_stored_per_tonne_not_per_gram(db_session):
    """Metals.dev serves base metals per gram under unit=g; the market
    quotes per metric tonne, so the stored close is scaled x1e6."""
    _save_latest_rates(db_session, {"aluminum": 0.0494, "nickel": 0.2692})

    assert _close(db_session, "MD_ALUM") == pytest.approx(49_400.0)
    assert _close(db_session, "MD_NICKEL") == pytest.approx(269_200.0)


def test_precious_metals_are_stored_per_gram_as_served(db_session):
    _save_latest_rates(db_session, {"gold": 1984.24, "copper": 0.2149})

    assert _close(db_session, "AV_GOLD") == pytest.approx(1984.24)
    assert _close(db_session, "AV_COPPER") == pytest.approx(0.2149)


def test_untracked_metals_in_the_response_are_skipped(db_session):
    """LBMA/MCX/IBJA benchmark symbols are not tracked - they must not
    create Stock or StockPrice rows."""
    _save_latest_rates(db_session, {"lbma_gold_am": 2017.13, "mcx_silver": 27.89, "lead": 0.0322})

    assert db_session.query(Stock).count() == 1  # only lead's MD_LEAD row
    assert _close(db_session, "MD_LEAD") == pytest.approx(32_200.0)


def test_same_day_refetch_updates_the_bar_in_place(db_session):
    """Day-midnight bars are unique per (symbol, timestamp) - a second
    fetch today must overwrite, not duplicate."""
    _save_latest_rates(db_session, {"zinc": 0.0609})
    _save_latest_rates(db_session, {"zinc": 0.0620})

    rows = db_session.query(StockPrice).filter(StockPrice.symbol == "MD_ZINC").all()
    assert len(rows) == 1 and rows[0].close_price == pytest.approx(62_000.0)


def test_alpha_vantage_history_is_converted_to_zar_per_gram(db_session):
    """AV serves gold per troy oz in USD; the stored series is ZAR/gram, so
    the backfill must convert or it writes ~57x-away bars next to Metals.dev
    snapshots."""
    saved = _save_historical_prices(
        db_session,
        "gold",
        [{"timestamp": datetime(2026, 1, 5), "close": 2000.0}],
        fallback_rate=18.5,
    )

    assert saved == 1
    assert _close(db_session, "AV_GOLD") == pytest.approx(2000.0 * 18.5 / 31.1034768)


def test_alpha_vantage_copper_is_converted_from_usd_per_tonne(db_session):
    saved = _save_historical_prices(
        db_session,
        "copper",
        [{"timestamp": datetime(2026, 1, 5), "close": 9_000.0}],
        fallback_rate=18.5,
    )

    assert saved == 1
    assert _close(db_session, "AV_COPPER") == pytest.approx(9_000.0 * 18.5 / 1_000_000)


def test_alpha_vantage_history_does_not_overwrite_existing_bars(db_session):
    """The backfill only fills missing days - a Metals.dev snapshot for the
    same timestamp keeps its value."""
    _save_latest_rates(db_session, {"gold": 1999.0})
    today = db_session.query(StockPrice.timestamp).filter(
        StockPrice.symbol == "AV_GOLD"
    ).one()[0]

    saved = _save_historical_prices(
        db_session, "gold",
        [{"timestamp": today, "close": 5000.0}],
        fallback_rate=18.5,
    )

    assert saved == 0
    assert _close(db_session, "AV_GOLD") == pytest.approx(1999.0)


def test_alpha_vantage_history_uses_each_days_fx_rate(db_session):
    """A year of bars converted at today's rate embeds the year's FX move
    as price error - each bar must convert at its own day's USD/ZAR."""
    saved = _save_historical_prices(
        db_session,
        "gold",
        [
            {"timestamp": datetime(2026, 1, 5), "close": 2000.0},  # rate 15
            {"timestamp": datetime(2026, 1, 8), "close": 2000.0},  # rate 18
        ],
        zar_by_day={datetime(2026, 1, 5): 15.0, datetime(2026, 1, 8): 18.0},
        fallback_rate=99.0,  # must be ignored while a map exists
    )

    assert saved == 2
    closes = {
        row.timestamp: row.close_price
        for row in db_session.query(StockPrice).filter(StockPrice.symbol == "AV_GOLD")
    }
    assert closes[datetime(2026, 1, 5)] == pytest.approx(2000.0 * 15.0 / 31.1034768)
    assert closes[datetime(2026, 1, 8)] == pytest.approx(2000.0 * 18.0 / 31.1034768)


def test_alpha_vantage_fx_gap_takes_nearest_earlier_rate(db_session):
    """Bars on FX-holiday days use the latest earlier rate; days before the
    map use the earliest one (no bar silently dropped)."""
    saved = _save_historical_prices(
        db_session,
        "gold",
        [
            {"timestamp": datetime(2026, 1, 3), "close": 1000.0},  # before map
            {"timestamp": datetime(2026, 1, 6), "close": 1000.0},  # gap day
        ],
        zar_by_day={datetime(2026, 1, 5): 17.0, datetime(2026, 1, 7): 19.0},
    )

    assert saved == 2
    closes = {
        row.timestamp: row.close_price
        for row in db_session.query(StockPrice).filter(StockPrice.symbol == "AV_GOLD")
    }
    # Jan 3 precedes the map -> earliest rate (Jan 5); Jan 6 fills forward.
    assert closes[datetime(2026, 1, 3)] == pytest.approx(1000.0 * 17.0 / 31.1034768)
    assert closes[datetime(2026, 1, 6)] == pytest.approx(1000.0 * 17.0 / 31.1034768)
