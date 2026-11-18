"""Tests for crypto/bullion holding valuation and the shared totals helper."""
from types import SimpleNamespace

import pytest

from app.dashboard import crypto, krugerrands
from app.dashboard.krugerrands import PRODUCT_CATALOG, TROY_OZ_GRAMS
from app.dashboard.utils import holding_totals


# --- holding_totals ------------------------------------------------------

def test_totals_basic_gain():
    totals = holding_totals([(100.0, 150.0), (200.0, 180.0)])
    assert totals["total_cost"] == pytest.approx(300.0)
    assert totals["total_value"] == pytest.approx(330.0)
    assert totals["gain"] == pytest.approx(30.0)
    assert totals["gain_pct"] == pytest.approx(10.0)


def test_uncosted_holding_is_not_counted_as_gain():
    # The second holding has a value but no recorded price paid.
    totals = holding_totals([(100.0, 120.0), (None, 5000.0)])
    assert totals["total_value"] == pytest.approx(5120.0)
    assert totals["gain"] == pytest.approx(20.0)
    assert totals["gain_pct"] == pytest.approx(20.0)


def test_unpriced_holding_is_not_counted_as_loss():
    # The second holding has a cost but no live quote.
    totals = holding_totals([(100.0, 120.0), (900.0, None)])
    assert totals["total_cost"] == pytest.approx(1000.0)
    assert totals["gain"] == pytest.approx(20.0)


def test_totals_unknown_when_nothing_comparable():
    assert holding_totals([(None, 50.0)])["gain"] is None
    assert holding_totals([(10.0, None)])["total_value"] is None
    assert holding_totals([]) == {
        "total_cost": None, "total_value": None, "gain": None, "gain_pct": None,
    }


# --- crypto --------------------------------------------------------------

def test_crypto_valuation():
    btc = SimpleNamespace(quantity=0.0125, purchase_price=1_200_000.0)
    cost, value = crypto._holding_valuation(btc, 1_500_000.0)
    assert cost == pytest.approx(15_000.0)
    assert value == pytest.approx(18_750.0)


def test_crypto_valuation_unknowns():
    free = SimpleNamespace(quantity=2.0, purchase_price=0.0)  # "leave 0 if unknown"
    assert crypto._holding_valuation(free, 10.0) == (None, pytest.approx(20.0))
    assert crypto._holding_valuation(free, None) == (None, None)


# --- bullion -------------------------------------------------------------

def test_catalogue_sizes_are_fine_troy_ounces():
    gold_bars = PRODUCT_CATALOG["gold_bar"]["sizes"]
    assert gold_bars["1 oz"] == 1.0
    assert gold_bars["100 g"] == pytest.approx(100 / TROY_OZ_GRAMS)
    assert PRODUCT_CATALOG["silver_bar"]["sizes"]["1 kg"] == pytest.approx(32.1507, rel=1e-4)
    # A 22k 1/10 oz Krugerrand still holds 0.1 oz of FINE gold.
    assert PRODUCT_CATALOG["gold_krugerrand"]["sizes"]["1/10 oz"] == 0.1


def test_bullion_valued_by_metal_spot():
    spots = {"XAU": 50_000.0, "XAG": 600.0}
    coin = SimpleNamespace(metal="XAU", fine_oz_per_unit=0.25, quantity=2, purchase_price=11_000.0)
    bar = SimpleNamespace(metal="XAG", fine_oz_per_unit=100 / TROY_OZ_GRAMS, quantity=1, purchase_price=None)

    assert krugerrands._holding_valuation(coin, spots) == (pytest.approx(22_000.0), pytest.approx(25_000.0))
    cost, value = krugerrands._holding_valuation(bar, spots)
    assert cost is None
    assert value == pytest.approx(600.0 * 100 / TROY_OZ_GRAMS)


def test_bullion_without_spot_is_unvalued():
    coin = SimpleNamespace(metal="XAG", fine_oz_per_unit=1.0, quantity=1, purchase_price=500.0)
    assert krugerrands._holding_valuation(coin, {"XAU": 50_000.0, "XAG": 0.0}) == (pytest.approx(500.0), None)
