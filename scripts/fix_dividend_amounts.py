r"""One-time repair for dividend data stored incorrectly.

Two legacy problems fixed here:

1. Duplicate dividends: providers wrote ex-dates with different time-of-day
   components (e.g. 00:00 and 09:00 for the same dividend), so the same
   payment was stored twice and the exact-timestamp dedup missed it. Rows are
   merged per (symbol, calendar day); the surviving row is normalised to
   midnight and inherits any missing payment/record/declaration dates.

2. Cent-denominated amounts: Yahoo reports .JO dividend amounts in cents for
   some tickers while they were stored as "ZAR" (e.g. a R1.42 dividend stored
   as 142.37). An amount larger than half the share price around the ex-date
   is implausible for a cash dividend and is divided by 100. The price
   reference must be contemporaneous (within ~2 years of the ex-date) so a
   decade-old dividend is never judged against today's crashed share price;
   rows without a near-date close are reported but left untouched.

Run ONCE, from the repo root:
    python scripts\fix_dividend_amounts.py            # dry-run (shows changes)
    python scripts\fix_dividend_amounts.py --apply    # applies the fix
"""
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.models.base import SessionLocal
from app.models.dividend import Dividend
from app.models.stock import StockPrice

APPLY = "--apply" in sys.argv

# A cash dividend above this fraction of the share price is treated as cents.
IMPLAUSIBLE_YIELD = 0.5
# Only trust a price reference within this window of the ex-date.
MAX_REF_AGE = timedelta(days=730)


def _closes_by_symbol(db, symbols):
    """{symbol: [(timestamp, close)] ascending} for the given symbols."""
    rows = db.query(StockPrice.symbol, StockPrice.timestamp, StockPrice.close_price).filter(
        StockPrice.symbol.in_(symbols),
        StockPrice.close_price.isnot(None),
    ).order_by(StockPrice.symbol, StockPrice.timestamp.asc()).all()
    result = defaultdict(list)
    for symbol, ts, close in rows:
        result[symbol].append((ts, close))
    return result


def _close_near(closes, ts):
    """Close nearest to `ts` within MAX_REF_AGE either side, else None."""
    best = None
    for close_ts, close_price in closes:
        if abs(close_ts - ts) <= MAX_REF_AGE:
            if best is None or abs(close_ts - ts) < abs(best[0] - ts):
                best = (close_ts, close_price)
    return best[1] if best else None


def main():
    db = SessionLocal()
    try:
        dividends = db.query(Dividend).order_by(Dividend.id).all()
        print(f"{len(dividends)} dividend rows in total")

        # --- Pass 1: merge same-day duplicates ---------------------------
        by_key = defaultdict(list)
        for d in dividends:
            if d.ex_dividend_date:
                by_key[(d.symbol, d.ex_dividend_date.date())].append(d)

        merged = 0
        dropped_ids = set()
        for (symbol, day), rows in by_key.items():
            if len(rows) < 2:
                # Still normalise the timestamp if it is not midnight.
                keeper = rows[0]
                if keeper.ex_dividend_date != datetime.combine(day, datetime.min.time()):
                    print(f"  normalise {symbol} {day}: {keeper.ex_dividend_date} -> midnight")
                    merged += 1
                    if APPLY:
                        keeper.ex_dividend_date = datetime.combine(day, datetime.min.time())
                continue

            # Keep the row that already sits at midnight, else the earliest id.
            rows.sort(key=lambda r: (r.ex_dividend_date.time() != datetime.min.time(), r.id))
            keeper, dupes = rows[0], rows[1:]
            print(f"  merge {symbol} {day}: keeping id={keeper.id}, dropping {[d.id for d in dupes]}")
            merged += 1
            dropped_ids.update(d.id for d in dupes)
            if not APPLY:
                continue
            for dupe in dupes:
                for field in ("payment_date", "record_date", "declaration_date", "frequency"):
                    if getattr(keeper, field) is None and getattr(dupe, field) is not None:
                        setattr(keeper, field, getattr(dupe, field))
                db.delete(dupe)
            keeper.ex_dividend_date = datetime.combine(day, datetime.min.time())

        if APPLY and merged:
            db.commit()
        print(f"duplicates/timestamps normalised: {merged}")

        # --- Pass 2: rescale cent-denominated amounts --------------------
        survivors = db.query(Dividend).all() if APPLY else [
            d for d in dividends if d.id not in dropped_ids
        ]
        closes_by_symbol = _closes_by_symbol(db, {d.symbol for d in survivors})

        rescaled = 0
        skipped = []
        for d in survivors:
            if not d.ex_dividend_date or d.amount is None:
                continue
            close = _close_near(closes_by_symbol.get(d.symbol, []), d.ex_dividend_date)
            if close is None:
                skipped.append((d.symbol, d.ex_dividend_date.date(), d.amount, "no near-date price"))
                continue
            if d.amount > close * IMPLAUSIBLE_YIELD:
                print(
                    f"  rescale {d.symbol} {d.ex_dividend_date.date()}: "
                    f"{d.amount:.4f} -> {d.amount / 100:.4f} (close {close:.2f})"
                )
                if APPLY:
                    d.amount = d.amount / 100
                rescaled += 1

        if skipped:
            print("\nrows needing manual review (no contemporaneous price):")
            for symbol, day, amount, reason in skipped:
                print(f"  {symbol} {day} amount={amount:.4f} ({reason})")

        if APPLY:
            db.commit()
            print(f"\nApplied: {merged} merges/normalisations, {rescaled} amounts rescaled.")
        else:
            print(f"\nDry-run: {merged} merges/normalisations, {rescaled} amounts would be rescaled. Re-run with --apply.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
