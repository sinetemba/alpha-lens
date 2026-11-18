"""
Mirror Luno balances into `crypto_holdings` (Streamlit-free).

Luno is the source of truth for quantities of `source == "luno"` rows: one
row per held (asset, wallet-or-bundle), created/updated from the balance API
and deleted when the balance drops to zero. Coins inside a Luno Bundle come
from BUNDLE-type accounts and get `bundle = BUNDLE_NAME`, so wallet XRP and
bundle XRP stay separate rows. Cost basis (`purchase_price`, and the
bundle's total on `CryptoBundle`) is user-entered and survives every sync.
Manual rows are never touched.
"""

from dataclasses import dataclass, field
from typing import Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.collectors.luno import FIAT_ASSETS, LUNO_ASSETS, LunoCollector, luno_coin_id
from app.models.base import utcnow_naive
from app.models.crypto import CryptoBundle, CryptoHolding

LUNO_SOURCE = "luno"
# Luno's API doesn't expose the bundle's product name, only that an account
# belongs to a bundle - all BUNDLE accounts are grouped under this name.
BUNDLE_NAME = "Luno Bundle"

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
    accounts = collector.get_account_balances()
    if accounts is None:
        return None

    result = LunoSyncResult()
    held: dict[tuple[str, Optional[str]], float] = {}
    for asset, account_type, amount in accounts:
        if asset in FIAT_ASSETS:
            if amount > 0:
                result.fiat[asset] = result.fiat.get(asset, 0.0) + amount
            continue
        key = (asset, BUNDLE_NAME if account_type == "BUNDLE" else None)
        held[key] = held.get(key, 0.0) + amount
    held = {key: amount for key, amount in held.items() if amount >= MIN_BALANCE}

    existing = {
        (h.coin_id, h.bundle): h
        for h in db.query(CryptoHolding).filter(CryptoHolding.source == LUNO_SOURCE)
    }
    now = utcnow_naive()
    seen = set()
    for (asset, bundle), amount in held.items():
        key = (luno_coin_id(asset), bundle)
        seen.add(key)
        symbol, name, _ = LUNO_ASSETS.get(asset, (asset, asset, None))
        row = existing.get(key)
        if row is None:
            row = CryptoHolding(
                coin_id=key[0], symbol=symbol, name=name, source=LUNO_SOURCE, bundle=bundle,
                quantity=amount,
                notes=f"Synced from Luno ({asset}{', bundle' if bundle else ''})",
            )
            db.add(row)
        row.quantity = amount
        row.last_synced_at = now
        result.synced += 1

    for key, row in existing.items():
        if key not in seen:
            db.delete(row)
            result.removed += 1

    if any(bundle for _, bundle in held) and not db.query(CryptoBundle).filter_by(name=BUNDLE_NAME).first():
        db.add(CryptoBundle(name=BUNDLE_NAME))

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
