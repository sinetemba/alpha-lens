"""Tests for the penny stock page's projection and fundamentals helpers."""
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.dashboard.penny_stocks import (
    _build_fundamentals,
    _apply_staleness,
    _drop_unit_glitches,
    _is_stale,
    _last_trade_date,
    _influences,
    _liquidity_penalty,
    _sane_high_low,
    _summarise_symbol,
    _format_rand_compact,
    _trailing_dividend_yield,
    _trend_projection,
)


# --- projection ------------------------------------------------------------

def test_projection_of_falling_stock_never_goes_negative():
    """A linear fit of 2.00 -> 0.50 over 90 days projected below zero."""
    closes = np.linspace(2.0, 0.5, 90)
    p = _trend_projection(closes)
    assert p is not None
    assert 0 < p["low"] < p["projected"] < p["high"]
    assert p["projected"] < 0.5
    assert p["daily_pct"] < 0


def test_projection_band_widens_with_noise():
    rng = np.random.default_rng(0)
    trend = np.linspace(1.0, 1.2, 90)
    calm = _trend_projection(trend * (1 + rng.normal(0, 0.005, 90)))
    noisy = _trend_projection(trend * (1 + rng.normal(0, 0.08, 90)))
    assert (noisy["high"] / noisy["low"]) > (calm["high"] / calm["low"])


def test_projection_ignores_bad_ticks_and_needs_history():
    closes = list(np.linspace(1.0, 1.1, 40)) + [0.0, float("nan")]
    p = _trend_projection(closes)
    assert p is not None and p["n"] == 40
    assert _trend_projection([1.0] * 10) is None


# --- unit glitches ---------------------------------------------------------

def test_drop_unit_glitches_removes_rand_ticks_among_cents():
    """PMV: zero-volume days served as 3.10 among ~300c closes."""
    closes = [300.0] * 40
    closes[10], closes[25] = 3.1, 2.9
    df = pd.DataFrame({"Close": closes}, index=pd.date_range("2026-01-01", periods=40))
    cleaned = _drop_unit_glitches(df)
    assert len(cleaned) == 38 and cleaned["Close"].min() == 300.0


def test_drop_unit_glitches_keeps_a_permanent_rerating():
    closes = [10.0] * 20 + [250.0] * 20
    df = pd.DataFrame({"Close": closes}, index=pd.date_range("2026-01-01", periods=40))
    assert len(_drop_unit_glitches(df)) == 40


def test_sane_high_low_replaces_impossible_intraday_values():
    df = pd.DataFrame({
        "Close": [300.0, 300.0, 300.0, 300.0, 300.0],
        "High":  [310.0, 31000.0, 290.0, float("nan"), 320.0],
        "Low":   [290.0, 3.0, 305.0, 295.0, float("nan")],
    })
    high, low = _sane_high_low(df)
    assert list(high) == [310.0, 300.0, 300.0, 300.0, 320.0]
    assert list(low) == [290.0, 300.0, 300.0, 295.0, 300.0]


def test_52w_low_ignores_intraday_glitch_but_keeps_real_lows():
    """HUG: a genuine 85c close stays the 52w low; a 3c intraday tick doesn't."""
    n = 60
    closes = [150.0] * n
    closes[20] = 85.0
    lows = [c - 5 for c in closes]
    lows[40] = 3.0  # Rand-unit tick on a bar with a normal close
    idx = pd.date_range("2026-01-01", periods=n)
    df = pd.DataFrame({
        "Open": closes, "High": [c + 5 for c in closes], "Low": lows,
        "Close": closes, "Volume": [1000] * n,
    }, index=idx)
    stats = _summarise_symbol(df, "HUG.JO", "HUG")
    assert stats["low_52w"] == pytest.approx(0.80)  # 85c bar's 80c intraday low
    assert stats["high_52w"] == pytest.approx(1.55)


# --- liquidity -------------------------------------------------------------

@pytest.mark.parametrize("traded, penalty", [
    (0.0, 30), (9_999, 30), (10_000, 15), (99_999, 15), (100_000, 0), (5e6, 0),
])
def test_liquidity_penalty_tiers(traded, penalty):
    assert _liquidity_penalty(traded) == penalty


def _bars(volumes, close_cents=200.0):
    n = len(volumes)
    closes = [close_cents] * n
    return pd.DataFrame({
        "Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": volumes,
    }, index=pd.bdate_range("2026-01-01", periods=n))


def test_dormant_stock_is_penalised_and_block_trade_does_not_rescue_it():
    """ISA-like: one big block trade, otherwise mostly no trades."""
    volumes = [100_000] * 40 + [0] * 12 + [10] * 7 + [5_000_000]
    liquid = _summarise_symbol(_bars([100_000] * 60), "X.JO", "X")
    dormant = _summarise_symbol(_bars(volumes), "Y.JO", "Y")

    assert liquid["traded_value"] == pytest.approx(200_000)  # 100k shares x R2
    assert liquid["liquidity_penalty"] == 0
    assert dormant["traded_value"] == 0  # median of 20 sessions, 12 of them R0
    assert dormant["zero_volume_days"] == 12
    assert dormant["liquidity_penalty"] == 30
    assert dormant["score"] <= max(0, liquid["score"] - 30) + 10  # only volume rule differs


def test_influences_flag_thin_trading():
    row = _summarise_symbol(_bars([0] * 15 + [1_000] * 45), "Y.JO", "Y")
    row["zero_volume_days"] = 14
    text = " ".join(_influences(row))
    assert "Thinly traded" in text and "no trades on 14 of the last 20 days" in text


# --- stale prices ----------------------------------------------------------

def test_last_trade_date_skips_forward_filled_zero_volume_bars():
    vol = pd.Series([500, 0, 0], index=pd.to_datetime(["2026-11-13", "2026-11-16", "2026-11-17"]))
    assert _last_trade_date(vol) == "2026-11-13"
    assert _last_trade_date(vol * 0) is None


def _scan_row(last_bar, last_trade, day_change=1.5):
    return {"dates": ["2026-11-01", last_bar], "last_trade": last_trade, "day_change": day_change}


def test_apply_staleness_against_the_market_session():
    fresh = _scan_row("2026-11-19", "2026-11-19")            # traded today
    ffill = _scan_row("2026-11-19", "2026-11-10", 0.0)       # zero-volume bars since
    no_bars = _scan_row("2026-11-17", "2026-11-17", 2.0)      # Yahoo stopped serving bars
    dead = _scan_row("2026-11-19", None, 0.0)
    _apply_staleness([fresh, ffill, no_bars, dead])

    assert fresh["sessions_since_trade"] == 0 and fresh["day_change"] == 1.5
    assert ffill["sessions_since_trade"] == 7 and ffill["day_change"] is None
    assert no_bars["sessions_since_trade"] == 2 and no_bars["day_change"] is None
    assert dead["sessions_since_trade"] is None

    assert not _is_stale(fresh) and not _is_stale(no_bars)
    assert _is_stale(ffill) and _is_stale(dead)
    assert not _is_stale({"symbol": "OLD"})  # cached pre-staleness row


def test_apply_staleness_counts_weekdays_over_a_weekend():
    friday = _scan_row("2026-11-16", "2026-11-13")  # Fri trade, Mon session
    _apply_staleness([friday])
    assert friday["sessions_since_trade"] == 1


def test_influences_name_the_last_trade():
    row = _summarise_symbol(_bars([1_000] * 55 + [0] * 5), "Y.JO", "Y")
    _apply_staleness([row])
    text = " ".join(_influences(row))
    assert "Last traded" in text and "5 sessions ago" in text


# --- dividend yield --------------------------------------------------------

def _frame(dividends):
    idx = pd.date_range("2025-11-20", periods=len(dividends), freq="D")
    return pd.DataFrame({"Close": [134.0] * len(dividends), "Dividends": dividends}, index=idx)


def test_trailing_yield_uses_cents_scale():
    """MRF: 8c + 16c over 12 months at R1.34 = 17.9%."""
    divs = [0.0] * 365
    divs[100], divs[250] = 8.0, 16.0
    assert _trailing_dividend_yield(_frame(divs), 1.34) == pytest.approx(17.91, abs=0.01)


def test_trailing_yield_zero_unknown_and_implausible():
    assert _trailing_dividend_yield(_frame([0.0] * 30), 1.34) == 0.0
    assert _trailing_dividend_yield(pd.DataFrame({"Close": [1.0]}, index=[pd.Timestamp("2026-01-01")]), 1.0) is None
    divs = [0.0] * 30
    divs[5] = 100.0  # R1.00 on a R1.34 share - a unit glitch
    assert _trailing_dividend_yield(_frame(divs), 1.34) is None


# --- fundamentals ----------------------------------------------------------

def _stock(**kw):
    base = dict(shares_outstanding=None, market_cap=None, sector=None, industry=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_fundamentals_prefer_yahoo():
    f = _build_fundamentals(
        {"marketCap": 438728512, "trailingPE": 4.63, "trailingEps": 0.41,
         "priceToBook": 0.31, "sector": "Basic Materials", "industry": "Building Materials"},
        _stock(market_cap=1.0, sector="Old"), 1.90, 0.0,
    )
    assert f["market_cap"] == 438728512
    assert f["pe"] == 4.63 and not f["loss_making"]
    assert f["sector"] == "Basic Materials"


def test_fundamentals_fall_back_to_stored_shares_times_price():
    f = _build_fundamentals({}, _stock(shares_outstanding=1_000_000, market_cap=5.0, sector="Tech"), 2.0, None)
    assert f["market_cap"] == 2_000_000
    assert f["sector"] == "Tech"
    assert f["pe"] is None and f["pb"] is None and f["div_yield"] is None


def test_fundamentals_flag_loss_makers_and_reject_junk():
    f = _build_fundamentals(
        {"trailingPE": None, "trailingEps": -0.05, "priceToBook": float("nan"), "marketCap": True},
        None, 1.0, None,
    )
    assert f["loss_making"] and f["pe"] is None
    assert f["pb"] is None and f["market_cap"] is None


@pytest.mark.parametrize("value, expected", [
    (None, "n/a"),
    (438_728_512, "R 438.7m"),
    (3_348_829_952, "R 3.35bn"),
    (52_000, "R 52,000"),
])
def test_format_rand_compact(value, expected):
    assert _format_rand_compact(value) == expected
