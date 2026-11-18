"""
Mirror Luno balances into `crypto_holdings` (Streamlit-free).

Luno is the source of truth for quantities of `source == "luno"` rows: one
row per held asset, created/updated from the balance API and deleted when
the balance drops to zero. Cost basis (`purchase_price`) is user-entered and
survives every sync. Manual rows are never touched.
"""

from dataclasses import dataclass, field
from typing import Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.collectors.luno import FIAT_ASSETS, LUNO_ASSETS, LunoCollector, luno_coin_id
from app.models.base import utcnow_naive
from app.models.crypto import CryptoHolding

LUNO_SOURCE = "luno"

# Dust below this is treated as "not held" (Luno keeps tiny remainders
# after sells, e.g. 0.00000001 BTC).
MIN_BALANCE = 1e-8


@dataclass
class LunoSyncResult:
    synced: int = 0
    removed: int = 0
    fiat: dict = field(default_factory=dict)   # e.g. {"ZAR": 152.30} cash on Luno


def sync_luno_holdings(db: Session, collector: Optional[LunoCollector] = None) -> Optional[LunoSyncResult]:
    """Upsert Luno-sourced holdings from live balances; None on fetch failure.

    A failed balance call returns None and changes nothing - an API outage
    must never wipe holdings. Rows are only deleted for assets Luno reports
    with a zero balance or no longer reports at all, and only when the call
    succeeded.
    """
    collector = collector or LunoCollector()
    balances = collector.get_balances()
    if balances is None:
        return None

    result = LunoSyncResult()
    held = {}
    for asset, amount in balances.items():
        if asset in FIAT_ASSETS:
            if amount > 0:
                result.fiat[asset] = amount
        elif amount >= MIN_BALANCE:
            held[asset] = amount

    existing = {
        h.coin_id: h for h in db.query(CryptoHolding).filter(CryptoHolding.source == LUNO_SOURCE)
    }
    now = utcnow_naive()
    seen = set()
    for asset, amount in held.items():
        coin_id = luno_coin_id(asset)
        seen.add(coin_id)
        symbol, name, _ = LUNO_ASSETS.get(asset, (asset, asset, None))
        row = existing.get(coin_id)
        if row is None:
            row = CryptoHolding(
                coin_id=coin_id, symbol=symbol, name=name, source=LUNO_SOURCE,
                quantity=amount, notes=f"Synced from Luno ({asset})",
            )
            db.add(row)
        row.quantity = amount
        row.last_synced_at = now
        result.synced += 1

    for coin_id, row in existing.items():
        if coin_id not in seen:
            db.delete(row)
            result.removed += 1

    db.commit()
    logger.info(
        f"Luno sync: {result.synced} holding(s)"
        + (f", removed {result.removed}" if result.removed else "")
    )
    return result


def last_luno_sync(db: Session):
    """Newest Luno sync time (naive UTC), or None if never synced."""
    row = db.query(CryptoHolding.last_synced_at).filter(
        CryptoHolding.source == LUNO_SOURCE, CryptoHolding.last_synced_at.isnot(None)
    ).order_by(CryptoHolding.last_synced_at.desc()).first()
    return row[0] if row else None
