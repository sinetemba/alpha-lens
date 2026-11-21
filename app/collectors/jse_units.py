"""Convert Yahoo's JSE price series from cents (ZAc) to Rand, per bar.

Yahoo quotes .JO tickers in ZAc, but its history for thinner shares
contains stretches served in Rand instead - a clean ~100x step down that
later steps back up (seen 2026-11-21: WEZ 45 -> 0.45 -> 45, SKA's latest
bars 0.85 among 85s, ISO's whole pre-2026-05-18 history at R-values like
217 vs 9100 after). Dividing every bar by 100 turned those stretches into
prices 100x too low, and the save-time "correct vs the previous stored
bar" check then propagated the error into good data.

Rule, per series:
  1. Split it into stretches at consecutive-bar steps of ~100x
     (UNIT_STEP_LOG10, i.e. 50x-200x). Each step up or down moves the
     stretch one unit level (cents above Rand).
  2. Assume the LATEST stretch is in cents - unless that would put any
     bar below 1c. The JSE's minimum price/tick is 1c, so a sub-cent
     "cents" value can only be a Rand value (SKA 0.85 = R0.85, EEL 0.03 =
     R0.03).
  3. Convert each stretch with its own factor.

A genuine 50x-200x move in one day would be read as a unit step; on the
JSE that is a corporate action, which auto_adjust already smooths.
"""
import numpy as np
import pandas as pd

UNIT_STEP_LOG10 = (1.7, 2.3)   # |log10(ratio)| between consecutive closes
MIN_CENTS = 0.95               # JSE minimum is 1c; slack for dividend-adjusted closes
RECENT_BARS = 250              # ~1y of sessions: the window the 1c rule tests
PRICE_COLUMNS = ("Open", "High", "Low", "Close", "Adj Close")


def rand_factors(close: pd.Series) -> pd.Series:
    """Per-bar multiplier taking a raw Yahoo JSE close to Rand (0.01 or 1)."""
    valid = close.where(close > 0)
    factors = pd.Series(0.01, index=close.index)
    if valid.notna().sum() == 0:
        return factors

    log = np.log10(valid.dropna())
    steps = log.diff().fillna(0.0)
    lo, hi = UNIT_STEP_LOG10
    # +1 = next bar 100x larger = one unit level up (towards cents).
    level_steps = np.where((steps.abs() >= lo) & (steps.abs() <= hi), np.sign(steps), 0.0)
    levels = pd.Series(np.cumsum(level_steps), index=log.index)

    # Cents level: the latest stretch's - unless that stretch sits below 1c,
    # in which case it was served in Rand (one level, never more). Only the
    # latest contiguous stretch is tested: decades-old junk bars (RNG has a
    # 0.099 from 2007 among 10^4-10^6 values) must not re-scale today's
    # prices - an all-history minimum stored RNG at R2.9M.
    cents_level = levels.iloc[-1]
    raw = valid.dropna()
    changed = levels.ne(levels.shift()).cumsum()
    # ...and only its last year: MTU drifted from 0.8 (2006) to 47,878
    # without one 100x step, so its "latest stretch" spans 20 years.
    latest_stretch = raw[changed == changed.iloc[-1]].iloc[-RECENT_BARS:]
    if latest_stretch.min() < MIN_CENTS:
        cents_level += 1

    # Rand = raw x 100^(cents_level - level) / 100
    per_bar = 100.0 ** (cents_level - levels - 1)
    factors.loc[per_bar.index] = per_bar
    # Bars without a valid close inherit the nearest preceding factor.
    return factors.where(close > 0).ffill().bfill().fillna(0.01)


def frame_to_rand(frame: pd.DataFrame) -> pd.DataFrame:
    """Copy of a Yahoo OHLC frame converted to Rand bar by bar."""
    if frame is None or frame.empty or "Close" not in frame.columns:
        return frame
    out = frame.copy()
    factors = rand_factors(out["Close"])
    for column in PRICE_COLUMNS:
        if column in out.columns:
            out[column] = out[column] * factors
    return out
