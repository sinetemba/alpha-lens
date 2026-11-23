"""The stored-dividend repair (scripts/fix_dividend_cents.py)."""
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from fix_dividend_cents import find_cents_rows  # noqa: E402


def _d(symbol, day, amount):
    return SimpleNamespace(symbol=symbol, ex_dividend_date=datetime.strptime(day, "%Y-%m-%d"), amount=amount)


def _actions(rows):
    return sorted((f[0].ex_dividend_date.strftime("%Y-%m-%d"), f[0].amount, f[1]) for f in find_cents_rows(rows))


def test_a_100x_twin_on_the_same_date_is_deleted():
    rows = [_d("STX40", "2025-01-15", 0.4253), _d("STX40", "2025-01-15", 42.53),
            _d("STX40", "2025-04-15", 0.33), _d("STX40", "2025-07-15", 0.98), _d("STX40", "2025-10-15", 1.42)]
    assert _actions(rows) == [("2025-01-15", 42.53, "delete")]


def test_a_lone_cents_value_is_rescaled_even_below_50x_the_median():
    """STX40 2014: R20.84 is only 49.6x its R0.42 median."""
    rows = [_d("STX40", f"2014-0{m}-20", a) for m, a in ((1, 0.40), (4, 0.42), (7, 0.45), (9, 0.43))]
    rows.append(_d("STX40", "2014-03-24", 20.84))
    assert _actions(rows) == [("2014-03-24", 20.84, "rescale")]


def test_the_same_payout_under_two_dates_keeps_the_earlier():
    rows = [_d("STX40", "2024-06-28", 0.8624), _d("STX40", "2024-07-17", 0.8624),
            _d("STX40", "2024-03-28", 0.37), _d("STX40", "2024-09-30", 0.70)]
    assert _actions(rows) == [("2024-07-17", 0.8624, "duplicate")]


def test_a_genuine_special_is_left_alone_and_fractional_cents_are_allowed():
    specials = [_d("ENX", "2022-01-10", 0.22), _d("ENX", "2022-06-08", 2.00),
                _d("ENX", "2022-09-10", 0.25), _d("ENX", "2023-01-10", 0.21)]
    assert _actions(specials) == []                       # R2 special: ~9x, not cents
    # Fractions of a cent are real dividends (PMV paid 0.5c) - only a
    # rescale that would land below 0.1c is refused.
    penny = [_d("PMV", "2004-01-05", 0.50), _d("PMV", "2005-01-05", 0.006),
             _d("PMV", "2006-01-05", 0.007), _d("PMV", "2007-01-05", 0.008)]
    assert _actions(penny) == [("2004-01-05", 0.50, "rescale")]
    tiny = [_d("XYZ", "2004-01-05", 0.05), _d("XYZ", "2005-01-05", 0.0006),
            _d("XYZ", "2006-01-05", 0.0007), _d("XYZ", "2007-01-05", 0.0008)]
    assert _actions(tiny) == []                           # /100 = R0.0005 < 0.1c
