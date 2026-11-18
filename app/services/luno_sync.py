"""
Mirror Luno balances into `crypto_holdings` (Streamlit-free).

Luno is the source of truth for quantities of `source == "luno"` rows: one
row per held (asset, wallet-or-bundle), created/updated from the balance API
and deleted when the balance drops to zero. Coins inside a Luno Bundle come
from BUNDLE-type accounts and get `bundle = BUNDLE_NAME`, so wallet XRP and
bundle XRP stay separate rows. Manual rows are never touched.

Cost basis is derived from Luno's transaction history on each sync:
- wallet coins: average cost of the coins still held (ZAR paid for buys;
  sells/sends release cost pro rata; fees reduce coins, not cost);
- a bundle: net Rand invested (bundle buys minus bundle sells, across ALL
  its accounts - rebalances move coins between them and can empty one).
A cost the user enters by hand is `cost_source = "manual"` and wins: syncs
never overwrite it.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.collectors.luno import FIAT_ASSETS, LUNO_ASSETS, LunoCollector, account_type, luno_coin_id
from app.models.base import utcnow_naive
from app.models.crypto import CryptoBundle, CryptoHolding

LUNO_SOURCE = "luno"
# All BUNDLE accounts are grouped under this key; Luno's own product name
# (e.g. "LLC bundle", from purchase descriptions) is stored as the label.
BUNDLE_NAME = "Luno Bundle"

# Dust below this is treated as "not held" (Luno keeps tiny remainders
# after sells, e.g. 0.00000001 BTC).
MIN_BALANCE = 1e-8

# "Bought XLM 180.9 for R 1,000.00" / "Sold BTC 0.004 for ZAR 563.55"
# (Luno separates amounts with non-breaking spaces).
_ZAR_IN_TEXT = re.compile(r"for (?:R|ZAR) ?([\d,]+(?:\.\d+)?)")
_APPROX_ZAR = re.compile(r"~R ?([\d,]+(?:\.\d+)?)")
_BUNDLE_LABEL = re.compile(r"as part of (.+?)\s*$")


@dataclass
class LunoSyncResult:
    synced: int = 0
    removed: int = 0
    fiat: dict = field(default_factory=dict)   # e.g. {"ZAR": 152.30} cash on Luno
    cost_error: Optional[str] = None           # cost history couldn't be read


def zar_amount(tx: dict) -> Optional[float]:
    """Rand value of an exchange transaction, from its text or Luno's estimate.

    Crypto-for-crypto trades ("Bought ETH .. for BTC ..") have no Rand in the
    text; Luno's "Approximate value" (market value at the time) is used.
    """
    text = (tx.get("description") or "").replace("\xa0", " ")
    match = _ZAR_IN_TEXT.search(text)
    if not match:
        approx = ((tx.get("details") or {}).get("Approximate value") or "").replace("\xa0", " ")
        match = _APPROX_ZAR.search(approx)
    return float(match.group(1).replace(",", "")) if match else None


def _is_fee(tx: dict) -> bool:
    return tx.get("kind") == "FEE" or "fee" in (tx.get("description") or "").lower()


def wallet_cost(transactions: list[dict]) -> tuple[float, float, Optional[datetime]]:
    """(cost of coins still held, coins held, first buy time) - average-cost method."""
    qty = cost = 0.0
    first_buy = None
    for tx in sorted(transactions, key=lambda t: t.get("row_index", 0)):
        delta = float(tx.get("balance_delta") or 0)
        if delta > 0:
            if tx.get("kind") == "EXCHANGE":
                cost += zar_amount(tx) or 0.0
                if first_buy is None and tx.get("timestamp"):
                    first_buy = datetime.fromtimestamp(tx["timestamp"] / 1000, tz=timezone.utc)
            qty += delta
        elif delta < 0:
            if not _is_fee(tx) and qty > 0:
                cost -= cost * min(-delta / qty, 1.0)
            qty += delta
    return max(cost, 0.0), max(qty, 0.0), first_buy


def bundle_invested(transactions: list[dict]) -> tuple[float, Optional[str]]:
    """(net Rand invested in the bundle, Luno's bundle name) across all its accounts.

    Rebalances (TRANSFER) and tracking fees move or shave coins inside the
    bundle - they are not new money, so only EXCHANGE rows count.
    """
    invested = 0.0
    label = None
    for tx in transactions:
        if tx.get("kind") != "EXCHANGE":
            continue
        amount = zar_amount(tx) or 0.0
        delta = float(tx.get("balance_delta") or 0)
        invested += amount if delta > 0 else -amount
        found = _BUNDLE_LABEL.search((tx.get("description") or "").replace("\xa0", " "))
        if found and not label:
            label = found.group(1)
    return max(invested, 0.0), label


def sync_luno_holdings(db: Session, collector: Optional[LunoCollector] = None) -> Optional[LunoSyncResult]:
    """Upsert Luno-sourced holdings from live balances; None on fetch failure.

    A failed balance call returns None and changes nothing - an API outage
    must never wipe holdings. Rows are only deleted for assets Luno reports
    with a zero balance or no longer reports at all, and only when the call
    succeeded. A failed history call keeps the existing cost basis.
    """
    collector = collector or LunoCollector()
    accounts = collector.get_accounts()
    if accounts is None:
        return None

    result = LunoSyncResult()
    held: dict[tuple[str, Optional[str]], float] = {}
    wallet_ids: dict[str, list[str]] = {}
    bundle_ids: list[str] = []
    for account in accounts:
        asset = (account.get("asset") or "").upper()
        try:
            amount = float(account.get("balance") or 0)
        except (TypeError, ValueError):
            continue
        if not asset:
            continue
        if asset in FIAT_ASSETS:
            if amount > 0:
                result.fiat[asset] = result.fiat.get(asset, 0.0) + amount
            continue
        in_bundle = account_type(account) == "BUNDLE"
        if in_bundle:
            bundle_ids.append(account.get("account_id"))
        else:
            wallet_ids.setdefault(asset, []).append(account.get("account_id"))
        key = (asset, BUNDLE_NAME if in_bundle else None)
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

        if bundle is None and row.cost_source != "manual":
            _apply_wallet_cost(collector, row, wallet_ids.get(asset, []), result)

    for key, row in existing.items():
        if key not in seen:
            db.delete(row)
            result.removed += 1

    if any(bundle for _, bundle in held):
        record = db.query(CryptoBundle).filter_by(name=BUNDLE_NAME).first()
        if record is None:
            record = CryptoBundle(name=BUNDLE_NAME)
            db.add(record)
        if record.cost_source != "manual":
            _apply_bundle_cost(collector, record, bundle_ids, result)

    db.commit()
    logger.info(
        f"Luno sync: {result.synced} holding(s)"
        + (f", removed {result.removed}" if result.removed else "")
        + (f"; cost history unavailable: {result.cost_error}" if result.cost_error else "")
    )
    return result


def _history(collector: LunoCollector, account_ids: list, result: LunoSyncResult) -> Optional[list]:
    """Combined transactions for some accounts; None (and cost_error) on failure."""
    rows: list[dict] = []
    for account_id in account_ids:
        txs = collector.get_transactions(account_id)
        if txs is None:
            result.cost_error = result.cost_error or collector.last_error
            return None
        rows.extend(txs)
    return rows


def _apply_wallet_cost(collector, row: CryptoHolding, account_ids: list, result: LunoSyncResult) -> None:
    txs = _history(collector, account_ids, result)
    if not txs:
        return
    cost, _, first_buy = wallet_cost(txs)
    if cost > 0 and row.quantity:
        row.purchase_price = cost / row.quantity
        row.cost_source = "luno"
        if first_buy and row.purchase_date is None:
            row.purchase_date = first_buy.replace(tzinfo=None).replace(hour=0, minute=0, second=0, microsecond=0)


def _apply_bundle_cost(collector, record: CryptoBundle, account_ids: list, result: LunoSyncResult) -> None:
    txs = _history(collector, account_ids, result)
    if not txs:
        return
    invested, label = bundle_invested(txs)
    if invested > 0:
        record.purchase_cost = invested
        record.cost_source = "luno"
    if label:
        record.label = label


def last_luno_sync(db: Session):
    """Newest Luno sync time (naive UTC), or None if never synced."""
    row = db.query(CryptoHolding.last_synced_at).filter(
        CryptoHolding.source == LUNO_SOURCE, CryptoHolding.last_synced_at.isnot(None)
    ).order_by(CryptoHolding.last_synced_at.desc()).first()
    return row[0] if row else None
