"""Per-bar ZAc->Rand conversion of Yahoo JSE series, and the EE cross-check.

Series below are the real Yahoo patterns seen 2026-11-21 (trimmed).
"""
import pandas as pd
import pytest

from app.collectors.jse_units import frame_to_rand, rand_factors
from app.models.notification import Notification
from app.services.easyequities_sync import _ee_reconcile_units, _notify_unit_conflicts


def _rand(closes):
    series = pd.Series(closes, index=pd.date_range("2026-01-01", periods=len(closes)), dtype=float)
    return (series * rand_factors(series)).round(6).tolist()


def test_plain_cents_series_is_divided_by_100():
    assert _rand([8360.0, 8407.0, 8500.0]) == [83.60, 84.07, 85.0]


def test_rand_stretch_inside_cents_history_is_left_as_rand():
    """WEZ: 45c, then a stretch Yahoo served as 0.45 (Rand), then 45c again."""
    assert _rand([45.0, 45.0, 0.45, 0.45, 0.45, 45.0, 37.0]) == [0.45, 0.45, 0.45, 0.45, 0.45, 0.45, 0.37]


def test_lone_glitch_bar_is_fixed():
    """PAN: one bar at 8.66 among ~850c closes."""
    assert _rand([844.82, 8.6582, 856.82]) == pytest.approx([8.4482, 8.6582, 8.5682])


def test_latest_stretch_in_rand_is_detected_by_the_one_cent_minimum():
    """SKA: the newest bars are 0.85 among 85s - 0.85c is below the JSE's
    1c minimum, so 0.85 must be Rand (EasyEquities: R0.85)."""
    assert _rand([61.0, 85.0, 85.0, 0.85, 0.85]) == [0.61, 0.85, 0.85, 0.85, 0.85]


def test_whole_recent_history_sub_cent_is_rand():
    """EEL: 3c, then Yahoo switched to 0.03 for months (= R0.03)."""
    assert _rand([3.0, 3.0, 0.03, 0.03, 0.03]) == [0.03, 0.03, 0.03, 0.03, 0.03]


def test_older_rand_history_before_a_switch_to_cents():
    """ISO: R-values (217, 96) until Yahoo switched to cents (9100c)."""
    assert _rand([217.0, 96.37, 9100.0, 2030.0]) == [217.0, 96.37, 91.0, 20.30]


def test_ancient_sub_cent_junk_does_not_rescale_recent_prices():
    """RNG's 20y history has a 0.099 bar from 2007 among huge glitches; an
    all-history 1c check scaled today's 292c to R2.9M. MTU drifted from 0.8
    (2006) without a 100x step, so one stretch spans 20 years."""
    closes = [5_000_000.0, 0.099] + [180.0] * 300 + [292.0]
    assert _rand(closes)[-2:] == [1.80, 2.92]


def test_a_sub_cent_latest_stretch_moves_at_most_one_level():
    assert _rand([0.0003, 0.0003]) == [0.0003, 0.0003]


def test_ordinary_large_moves_are_not_unit_steps():
    """A 5x jump or 70% crash is a market move, not a unit change."""
    assert _rand([100.0, 500.0, 150.0]) == [1.0, 5.0, 1.5]


def test_frame_conversion_applies_the_close_factor_to_every_price_column():
    frame = pd.DataFrame(
        {"Open": [46.0, 0.46], "High": [47.0, 0.47], "Low": [44.0, 0.44], "Close": [45.0, 0.45], "Volume": [10, 20]},
        index=pd.date_range("2026-01-01", periods=2),
    )
    out = frame_to_rand(frame)
    assert out["Close"].tolist() == pytest.approx([0.45, 0.45])
    assert out["High"].tolist() == pytest.approx([0.47, 0.47])
    assert out["Volume"].tolist() == [10, 20]  # volumes untouched
    assert frame["Close"].tolist() == [45.0, 0.45]  # input not mutated


def test_missing_closes_do_not_break_conversion():
    series = pd.Series([45.0, float("nan"), 46.0], index=pd.date_range("2026-01-01", periods=3))
    assert rand_factors(series).tolist() == [0.01, 0.01, 0.01]


# --- EasyEquities cross-check ---------------------------------------------

def test_ee_values_are_never_rescaled_to_match_the_stored_close(db_session):
    """ISO: EE R20.30 vs a corrupted stored R0.203 - EE is kept and the
    conflict is flagged (it used to be "fixed" /100 to match)."""
    holding = {"symbol": "ISO", "current_price": 20.30, "purchase_price": 206.34,
               "current_value": 345.0, "purchase_value": 3507.0, "currency": "ZAR"}
    _ee_reconcile_units(db_session, holding, "ZAR", latest_closes={"ISO": 0.203})

    assert holding["current_price"] == 20.30 and holding["purchase_value"] == 3507.0
    assert holding["unit_conflict"][2] == pytest.approx(100.0)

    _notify_unit_conflicts(db_session, [holding])
    note = db_session.query(Notification).one()
    assert note.type == "system" and note.title == "Price unit conflict: ISO"


def test_ordinary_divergence_is_not_a_unit_conflict(db_session):
    holding = {"symbol": "ABC", "current_price": 10.0, "currency": "ZAR"}
    _ee_reconcile_units(db_session, holding, "ZAR", latest_closes={"ABC": 2.0})
    assert "unit_conflict" not in holding
    _notify_unit_conflicts(db_session, [holding])
    assert db_session.query(Notification).count() == 0
