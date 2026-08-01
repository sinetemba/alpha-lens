import pandas as pd
import numpy as np
import pytest

from app.analytics.technical_indicators import TechnicalIndicators


def test_sma_basic():
    data = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    sma = TechnicalIndicators.calculate_sma(data, 3)
    assert pd.isna(sma.iloc[0])
    assert pd.isna(sma.iloc[1])
    assert sma.iloc[2] == pytest.approx(2.0)
    assert sma.iloc[-1] == pytest.approx(4.0)


def test_ema_non_nan_after_initial_period():
    data = pd.Series([1.0] * 15 + [2.0] * 5)
    ema = TechnicalIndicators.calculate_ema(data, 12)
    assert ema.iloc[-1] > 1.0
    assert pd.notna(ema.iloc[-1])


def test_rsi_high_for_consistently_increasing():
    data = pd.Series(np.arange(1, 21, dtype=float))
    rsi = TechnicalIndicators.calculate_rsi(data, 14)
    assert rsi.iloc[-1] > 50
    assert pd.notna(rsi.iloc[-1])


def test_calculate_all_indicators_populates_columns():
    df = pd.DataFrame({
        "open": np.linspace(100, 200, 250),
        "high": np.linspace(101, 201, 250),
        "low": np.linspace(99, 199, 250),
        "close": np.linspace(100, 200, 250),
        "volume": np.full(250, 1000),
    })
    result = TechnicalIndicators.calculate_all_indicators(df)
    expected = [
        "sma_20", "sma_50", "sma_200",
        "ema_12", "ema_26",
        "rsi",
        "macd", "macd_signal", "macd_hist",
        "bollinger_upper", "bollinger_middle", "bollinger_lower",
        "atr", "obv", "vwap",
        "stoch_k", "stoch_d",
        "adx", "plus_di", "minus_di",
        "cci",
    ]
    for col in expected:
        assert col in result.columns


def test_bollinger_bands_relationships():
    data = pd.Series([10.0, 12.0, 14.0, 16.0, 18.0])
    bb = TechnicalIndicators.calculate_bollinger_bands(data, period=2)
    valid = bb["middle"].dropna().index
    for i in valid:
        assert bb["upper"].iloc[i] >= bb["middle"].iloc[i] >= bb["lower"].iloc[i]


def test_atr_is_non_negative_after_warmup():
    high = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0])
    low = pd.Series([9.0, 10.0, 11.0, 12.0, 13.0])
    close = pd.Series([9.5, 10.5, 11.5, 12.5, 13.5])
    atr = TechnicalIndicators.calculate_atr(high, low, close, 2)
    assert atr.iloc[-1] >= 0
    assert pd.notna(atr.iloc[-1])


def test_obv_cumulates_with_price_direction():
    close = pd.Series([10.0, 11.0, 10.0, 12.0])
    volume = pd.Series([100, 100, 100, 100])
    obv = TechnicalIndicators.calculate_obv(close, volume)
    # First NaN is treated as 0, then +100, -100, +100
    assert obv.iloc[-1] == 100


def test_trading_signals_bullish_overall():
    df = pd.DataFrame({
        "close": [120.0],
        "sma_20": [110.0],
        "sma_50": [100.0],
        "rsi": [40.0],
        "macd": [2.0],
        "macd_signal": [1.0],
        "bollinger_upper": [130.0],
        "bollinger_lower": [90.0],
    })
    signals = TechnicalIndicators.get_trading_signals(df)
    assert signals["trend"] == "bullish"
    assert signals["momentum"] == "bullish"
    assert signals["overall"] == "buy"


def test_trading_signals_bearish_overall():
    df = pd.DataFrame({
        "close": [80.0],
        "sma_20": [90.0],
        "sma_50": [100.0],
        "rsi": [80.0],
        "macd": [-2.0],
        "macd_signal": [-1.0],
        "bollinger_upper": [130.0],
        "bollinger_lower": [90.0],
    })
    signals = TechnicalIndicators.get_trading_signals(df)
    assert signals["trend"] == "bearish"
    assert signals["momentum"] == "overbought"
    assert signals["overall"] == "sell"
