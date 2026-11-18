r"""Read-only sanity check: stored closes vs Yahoo Finance.

Compares every stored daily close in the last month against Yahoo's close for
the same trading day and reports symbols that disagree - wrong-instrument
captures, cents/Rand unit mismatches (~100x) and stale feeds. Nothing is
written; fix confirmed problems by hand (take a backup first:
`python scripts\run_updater.py --backup`).

Run from the repo root:
    python scripts\check_stored_prices.py          # updater-tracked symbols
    python scripts\check_stored_prices.py --all    # every active stock
"""
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from loguru import logger

from app.collectors.yfinance import YFinanceCollector
from app.models.base import SessionLocal
from app.models.stock import Stock, StockPrice
from app.services.updater import _get_tracked_symbols

# Stored vs Yahoo closes further apart than this are reported.
TOLERANCE = 0.05
# A ratio this close to 100x / 0.01x is reported as a unit mismatch.
UNIT_TOLERANCE = 0.1
# Stored data this many days behind Yahoo's latest bar is reported as stale.
STALE_DAYS = 5


def _classify(ratio: float) -> str:
    for factor, label in ((100.0, "stored 100x too HIGH (cents?)"), (0.01, "stored 100x too LOW")):
        if abs(ratio / factor - 1) <= UNIT_TOLERANCE:
            return label
    return "mismatch (wrong instrument?)"


def main() -> int:
    logger.remove()  # the collector's retry chatter would drown the report
    check_all = "--all" in sys.argv
    db = SessionLocal()
    try:
        if check_all:
            symbols = sorted(
                s for (s,) in db.query(Stock.symbol).filter(Stock.is_active == True).all()
                # AV_*/XAU_* snapshots and GC=F-style futures aren't JSE tickers.
                if "_" not in s and "=" not in s
            )
        else:
            symbols = _get_tracked_symbols(db)

        since = datetime.now() - timedelta(days=35)
        stored = defaultdict(dict)
        for symbol, ts, close in db.query(
            StockPrice.symbol, StockPrice.timestamp, StockPrice.close_price
        ).filter(StockPrice.symbol.in_(symbols), StockPrice.timestamp >= since):
            stored[symbol][ts.date()] = close
    finally:
        db.close()

    print(f"Checking {len(symbols)} symbol(s) against Yahoo Finance...")
    bars = YFinanceCollector().get_latest_bars(symbols, period="1mo")

    problems = 0
    for symbol in symbols:
        frame = bars.get(symbol)
        if frame is None or frame.empty:
            print(f"  {symbol:10} no Yahoo data - can't verify")
            continue
        yahoo = {
            idx.date(): float(close)
            for idx, close in frame["Close"].dropna().items()
            if close > 0
        }
        ours = stored.get(symbol, {})
        if not ours:
            print(f"  {symbol:10} no stored bars in the last 35 days")
            problems += 1
            continue

        worst = None
        for day, close in ours.items():
            if day in yahoo:
                ratio = close / yahoo[day]
                if worst is None or abs(ratio - 1) > abs(worst[1] - 1):
                    worst = (day, ratio, close, yahoo[day])
        if worst and abs(worst[1] - 1) > TOLERANCE:
            day, ratio, close, ref = worst
            print(
                f"  {symbol:10} {_classify(ratio)}: stored R{close:.4f} vs Yahoo R{ref:.4f} "
                f"on {day} ({ratio:.2f}x)"
            )
            problems += 1

        behind = (max(yahoo) - max(ours)).days if yahoo else 0
        if behind > STALE_DAYS:
            print(f"  {symbol:10} stale: latest stored bar {max(ours)}, Yahoo has {max(yahoo)}")
            problems += 1

    print(f"Done: {problems} problem(s) found." if problems else "Done: all stored closes agree with Yahoo.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
