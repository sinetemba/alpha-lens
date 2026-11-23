"""Repair stored dividends that are cents recorded as Rand (100x too high).

Uses the same rule as new rows (DataService._dividend_amount_rand's median
check): an amount >= 50x the median of the symbol's OTHER dividends whose
/100 lands within 10x of that median is cents. Found 2026-11-23: STX40
R42.53 (2025-01-15) and R55.71 (2026-01-21), CTOP50 R19.01 (2025-01-15),
against usual payouts of R0.97-1.42 and R0.14-0.41.

- If the same symbol + ex-date already has the /100 amount (a correct twin
  was stored too), the 100x row is DELETED - rescaling it would create a
  duplicate. Otherwise the row is divided by 100.
- The same payout stored twice within 20 days (ex-date + another date) is
  deduplicated, keeping the earlier (ex-dividend) date.

Dry run by default; --apply writes. Back up first:
    python scripts\\run_updater.py --backup
    python scripts\\fix_dividend_cents.py            # show what would change
    python scripts\\fix_dividend_cents.py --apply
"""
import argparse
import bisect
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func  # noqa: E402

from app.models.base import SessionLocal  # noqa: E402
from app.models.dividend import Dividend  # noqa: E402
from app.models.stock import StockPrice  # noqa: E402


DUPLICATE_WINDOW_DAYS = 20   # same payout stored under ex-date AND another date


ERA_REFERENCE_PAYOUTS = 6   # first payouts once prices exist - the trusted regime
# 0.1c floor for a rescaled dividend. NOT the JSE's 1c minimum - that is a
# PRICE tick; dividends can be fractions of a cent (PMV paid 0.5c, R0.005).
MIN_DIVIDEND = 0.001
YIELD_CEILING = 0.12         # above 12% of the price AND 15x the median -> cents


def find_cents_rows(dividends, first_bar=None, close_at=None):
    """[(row, action, detail)] - action is "delete", "rescale" or "duplicate".

    1) Twins: the same symbol + ex-date stored twice, one ~100x the other -
       the larger is a cents copy (unambiguous).
    2) The median rule, REPEATED on the corrected amounts until stable: bad
       rows inflate a symbol's median, which hid STX40's R42.53/R55.71 and
       its pre-2024 R20-R70 rows from a single pass.
    3) Eras: Yahoo served whole pre-2005 stretches in cents (SUR 8.5-16 in
       2000-04, then 0.20+; RMH/PPC alternating). A row before the symbol's
       first stored price, >= 20x the median of its first
       ERA_REFERENCE_PAYOUTS payouts once prices exist, is cents. The median
       rule can't see a cluster that is wrong together.
    4) Yield: where a price exists, > YIELD_CEILING of it AND >= 15x the
       median is cents (STX40 2016 R9.20 = a 20% "yield" on an index ETF);
       a genuine special (enX R2 ~ 9x) is left alone.
    5) The same payout stored under two dates <= DUPLICATE_WINDOW_DAYS apart
       (an ex-date and a payment date from different sources) - the later
       row is a duplicate and would count the income twice.

    `first_bar` = {symbol: first stored price datetime}; `close_at(symbol,
    datetime)` = the close on/before that day or None. Both optional.
    """
    by_symbol = defaultdict(list)
    for d in dividends:
        if d.amount and d.amount > 0:
            by_symbol[d.symbol].append(d)
    findings = []
    for symbol, rows in by_symbol.items():
        gone = set()
        for d in rows:
            match = next((o for o in rows if o is not d
                          and o.ex_dividend_date.date() == d.ex_dividend_date.date()
                          and abs(o.amount - d.amount / 100) <= 0.01 * (d.amount / 100)), None)
            if match is not None:
                findings.append((d, "delete", f"R{match.amount:.4f} already stored for that date"))
                gone.add(id(d))

        amount = {id(d): d.amount for d in rows if id(d) not in gone}
        rescaled = set()
        for _ in range(10):
            changed = False
            for d in rows:
                if id(d) not in amount or id(d) in rescaled:
                    continue
                others = [a for k, a in amount.items() if k != id(d)]
                if len(others) < 3:
                    continue
                median = statistics.median(others)
                scaled = amount[id(d)] / 100
                # 30x / within 5x - looser than the save-time guard's 50x /
                # 10x: a 100x cents value of a below-median payout is <50x
                # the median (STX40 2014: R20.84 vs a R0.42 median = 49.6x).
                # Never below MIN_DIVIDEND (a 1000x error, not a unit slip).
                if (amount[id(d)] >= 30 * median and median / 5 <= scaled <= median * 5
                        and scaled >= MIN_DIVIDEND):
                    amount[id(d)] = scaled
                    rescaled.add(id(d))
                    changed = True
            if not changed:
                break
        for d in rows:
            if id(d) in rescaled:
                findings.append((d, "rescale", f"-> R{amount[id(d)]:.4f}"))

        start = (first_bar or {}).get(symbol)
        live = sorted((d for d in rows if id(d) in amount), key=lambda d: d.ex_dividend_date)
        if start is not None:
            reference = [amount[id(d)] for d in live if d.ex_dividend_date >= start][:ERA_REFERENCE_PAYOUTS]
            if len(reference) >= 3:
                ref = statistics.median(reference)
                for d in live:
                    value = amount[id(d)]
                    # No price before `start`, so no yield check to lean on:
                    # 8x the first validated payouts whose /100 fits that
                    # regime (AEG 2004 R2,110 vs R218 = 9.7x; PPC 2000 5.99
                    # = 8x) is far likelier cents than an 8x shrinking payout.
                    if (d.ex_dividend_date < start and id(d) not in rescaled
                            and value >= 8 * ref and ref / 20 <= value / 100 <= ref * 10 and value / 100 >= MIN_DIVIDEND):
                        amount[id(d)] = value / 100
                        rescaled.add(id(d))
                        findings.append((d, "rescale", f"-> R{value / 100:.4f} (pre-2005 cents era; ref R{ref:.4f})"))
        if close_at is not None and len(amount) >= 4:
            for d in live:
                if id(d) in rescaled:
                    continue
                value = amount[id(d)]
                close = close_at(symbol, d.ex_dividend_date)
                others = [a for k, a in amount.items() if k != id(d)]
                median = statistics.median(others)
                if (close and value > YIELD_CEILING * close and value >= 15 * median
                        and value / 100 >= MIN_DIVIDEND):
                    amount[id(d)] = value / 100
                    rescaled.add(id(d))
                    findings.append((d, "rescale", f"-> R{value / 100:.4f} ({100 * value / close:.0f}% yield on R{close:.2f})"))

        ordered = sorted((d for d in rows if id(d) in amount), key=lambda d: d.ex_dividend_date)
        last = None   # compare against the last KEPT row, not the previous one
        for b in ordered:
            if last is not None:
                a = last
                same = abs(amount[id(a)] - amount[id(b)]) <= 0.005 * max(amount[id(a)], amount[id(b)])
                if same and (b.ex_dividend_date - a.ex_dividend_date).days <= DUPLICATE_WINDOW_DAYS:
                    findings.append((b, "duplicate", f"same R{amount[id(a)]:.4f} as {a.ex_dividend_date:%Y-%m-%d}"))
                    # A rescale on a row that is now deleted as a duplicate is moot.
                    findings = [f for f in findings if not (f[0] is b and f[1] == "rescale")]
                    continue
            last = b
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="Write the changes (default: dry run).")
    args = parser.parse_args()
    db = SessionLocal()
    try:
        dividends = db.query(Dividend).all()
        symbols = {d.symbol for d in dividends}
        first_bar = dict(db.query(StockPrice.symbol, func.min(StockPrice.timestamp))
                         .filter(StockPrice.symbol.in_(symbols)).group_by(StockPrice.symbol).all())
        closes = defaultdict(list)
        for sym, ts, close in (db.query(StockPrice.symbol, StockPrice.timestamp, StockPrice.close_price)
                               .filter(StockPrice.symbol.in_(symbols)).order_by(StockPrice.timestamp)):
            closes[sym].append((ts, close))

        def close_at(symbol, when):
            series = closes.get(symbol) or []
            i = bisect.bisect_right(series, (when, float("inf"))) - 1
            return series[i][1] if i >= 0 and (when - series[i][0]).days <= 10 else None

        findings = find_cents_rows(dividends, first_bar, close_at)
        if not findings:
            print("No cents-as-Rand dividends found.")
            return 0
        for d, action, detail in sorted(findings, key=lambda f: (f[0].symbol, f[0].ex_dividend_date)):
            print(f"{d.symbol:8} {d.ex_dividend_date:%Y-%m-%d}  R{d.amount:<10.4f} {action:9} {detail}")
            if args.apply:
                if action in ("delete", "duplicate"):
                    db.delete(d)
                else:
                    d.amount = d.amount / 100
        if args.apply:
            db.commit()
            print(f"\nApplied {len(findings)} change(s).")
        else:
            print(f"\nDry run - {len(findings)} change(s). Re-run with --apply (back up first).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
