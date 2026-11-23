"""One-off: review losing portfolio holdings for comeback potential."""
from app.database.init_db import SessionLocal
from app.models.portfolio import (
    PortfolioHolding, EXCLUDED_ACCOUNT_TYPES, EXCLUDED_PORTFOLIO_SYMBOLS,
    is_jse_tracked_account,
)
from sqlalchemy import func

db = SessionLocal()

rows = db.query(
    PortfolioHolding.symbol,
    PortfolioHolding.account_type,
    func.sum(PortfolioHolding.quantity),
    func.sum(PortfolioHolding.quantity * PortfolioHolding.purchase_price),
    func.sum(PortfolioHolding.current_value),
    func.max(PortfolioHolding.current_price),
).filter(
    ~PortfolioHolding.symbol.in_(EXCLUDED_PORTFOLIO_SYMBOLS),
    ~PortfolioHolding.account_type.in_(EXCLUDED_ACCOUNT_TYPES),
).group_by(PortfolioHolding.symbol, PortfolioHolding.account_type).all()

print(f"{'SYM':6} {'ACCT':14} {'QTY':>12} {'COST':>12} {'VALUE':>12} {'PRICE':>10} {'P/L%':>8}")
for r in sorted(rows, key=lambda r: (r[4] or 0) - (r[3] or 0)):
    sym, acct, qty, cost, value, price = r
    cost = cost or 0.0
    value = value or 0.0
    pl = (value - cost) / cost * 100 if cost else 0.0
    print(f"{sym:6} {acct or '':14} {qty:>12.2f} {cost:>12.2f} {value:>12.2f} {(price or 0):>10.4f} {pl:>7.1f}%")

# Per-symbol Action via the same function the page uses
from app.dashboard.portfolio import _holding_action, _holding_price_stats
from app.models.watchlist import WatchlistItem

jse_syms = [r[0] for r in rows if is_jse_tracked_account(r[1])]
stats_map = _holding_price_stats(db, jse_syms)
watched = {w.symbol: w for w in db.query(WatchlistItem).filter(
    WatchlistItem.symbol.in_(jse_syms), WatchlistItem.is_active == True).all()}

print("\n--- Action column per holding ---")
for sym, acct, qty, cost, value, price in sorted(rows, key=lambda r: (r[4] or 0) - (r[3] or 0)):
    cost = cost or 0.0
    value = value or 0.0
    pl = (value - cost) / cost * 100 if cost else 0.0
    if not is_jse_tracked_account(acct):
        print(f"{sym:6} {pl:>7.1f}%  —")
        continue
    if sym not in stats_map:
        print(f"{sym:6} {pl:>7.1f}%  Sell — no prices")
        continue
    action = _holding_action(pl if cost else None, stats_map[sym], watched.get(sym))
    s = stats_map[sym]
    print(f"{sym:6} {pl:>7.1f}%  {action:22} (px {s['price']:.4f}, mom3m {s['mom_3m']:+.1f}%, "
          f"vs20 {100*(s['price']/s['sma20']-1):+.1f}%, vs50 {100*(s['price']/s['sma50']-1):+.1f}%)")
