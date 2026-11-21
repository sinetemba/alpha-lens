"""Tests for SAFEX grain prices (Grain SA scrape) and the SA-vs-CBOT gap."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.collectors.safex import contract_month, parse_safex_table
from app.dashboard.commodities import SA_GRAINS, build_price_rows
from app.models.stock import StockPrice
from app.services.safex_prices import (
    BUSHELS_PER_TONNE_CORN,
    BUSHELS_PER_TONNE_WHEAT,
    front_contract,
    refresh_safex,
)

# Trimmed copy of the real Grain SA table (2026-11-20).
_HEAD = (
    "<tr><th>Instrument</th><th>Contract</th><th>Last Traded Time</th><th>Last Traded Price</th>"
    "<th>Difference</th><th>High Price</th><th>Low Price</th><th>Volume</th><th>Open Interest</th></tr>"
)


def _row(inst, contract, last, high="0", low="0", vol="0", when="2026-11-20 12:45:00"):
    cells = [inst, contract, when, last, "0", high, low, vol, "0"]
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


PAGE = (
    "<html><body><table><tr><td>Last Updated: 20 Nov 2026 12:45:00</td></tr>" + _HEAD
    + _row("WMAZ", "NOV26", "4069", "4134.6", "4065", "96")
    + _row("WMAZ", "DEC26", "4078", "4180", "4069", "3055")
    + _row("WMAZ", "MAR27", "4215", "4300", "4203.8", "3217")
    + _row("YMAZ", "DEC26", "4029.8", "4090", "4023.2", "2536")
    + _row("WEAT", "DEC26", "5930", "5959", "5929", "2038")
    + _row("WEAT", "JAN27", "6017")
    + _row("SOYB", "DEC26", "9285")
    + "</table></body></html>"
)


def test_contract_month_parsing():
    assert contract_month("DEC26") == (2026, 12)
    assert contract_month("mar27") == (2027, 3)
    assert contract_month("Instrument") is None


def test_table_parsing_skips_headers_and_treats_zero_high_low_as_no_trade():
    rows = parse_safex_table(PAGE)

    assert len(rows) == 7
    jan = next(r for r in rows if r["instrument"] == "WEAT" and r["contract"] == "JAN27")
    assert jan["last"] == 6017.0 and jan["high"] is None and jan["volume"] == 0
    assert rows[0]["traded_at"] == datetime(2026, 11, 20, 12, 45)


def test_front_contract_skips_the_delivery_month():
    """In November the NOV26 contract is in delivery (96 lots vs DEC26's
    3,055) - DEC26 is the price."""
    assert front_contract(parse_safex_table(PAGE), "WMAZ")["contract"] == "DEC26"


def test_refresh_stores_front_contracts_and_overwrites_same_day(db_session):
    collector = SimpleNamespace(get_contracts=lambda: parse_safex_table(PAGE))
    assert refresh_safex(db_session, collector) == 3
    assert refresh_safex(db_session, collector) == 3

    bars = {p.symbol: p for p in db_session.query(StockPrice).all()}
    assert set(bars) == {"SFX_WMAZ", "SFX_YMAZ", "SFX_WEAT"}
    assert bars["SFX_WMAZ"].close_price == 4078.0
    assert bars["SFX_WMAZ"].high_price == 4180.0 and bars["SFX_WMAZ"].volume == 3055
    assert bars["SFX_WEAT"].timestamp == datetime(2026, 11, 20)


def test_refresh_failure_is_none(db_session):
    assert refresh_safex(db_session, SimpleNamespace(get_contracts=lambda: None)) is None


def test_gap_against_cbot_in_rand_per_tonne():
    snap = lambda close: SimpleNamespace(close_price=close)  # noqa: E731
    snapshots = {
        "SFX_YMAZ": (snap(4030.0), None),
        "SFX_WEAT": (snap(5930.0), None),
        "SFX_WMAZ": (snap(4078.0), None),
        "YF_CORN": (snap(76.0), None),     # R/bushel
        "YF_WHEAT": (snap(100.0), None),
    }

    rows = {r["Commodity"]: r for r in build_price_rows({}, snapshots)[SA_GRAINS]}

    corn_t = 76.0 * BUSHELS_PER_TONNE_CORN
    assert rows["Yellow Maize"]["CBOT (R)"] == pytest.approx(corn_t)
    assert rows["Yellow Maize"]["vs CBOT"] == pytest.approx((4030.0 / corn_t - 1) * 100)
    assert rows["White Maize"]["CBOT (R)"] == pytest.approx(corn_t)
    assert rows["Wheat"]["vs CBOT"] == pytest.approx((5930.0 / (100.0 * BUSHELS_PER_TONNE_WHEAT) - 1) * 100)


def test_gap_is_blank_without_a_cbot_price():
    snap = SimpleNamespace(close_price=4030.0)
    row = build_price_rows({}, {"SFX_YMAZ": (snap, None)})[SA_GRAINS][0]
    assert row["CBOT (R)"] is None and row["vs CBOT"] is None
