from sqlalchemy import Column, Integer, String, Float, DateTime, Text, text
from .base import Base, utcnow_naive


class CryptoHolding(Base):
    """A purchased cryptocurrency position.

    `coin_id` is the CoinGecko id (e.g. "bitcoin", "solana") - the canonical
    key for price lookups; `symbol` is display-only. Manual rows are valued
    in ZAR via CoinGecko.

    `source == "luno"` rows mirror Luno balances (services/luno_sync.py):
    the sync owns `quantity` and deletes rows for coins no longer held, but
    never touches `purchase_price` - Luno's balance API has no cost basis,
    so the user enters it once. They are valued at Luno's own ZAR bid.
    """

    __tablename__ = "crypto_holdings"

    id = Column(Integer, primary_key=True, index=True)
    coin_id = Column(String(60), nullable=False, index=True)
    symbol = Column(String(15), nullable=False)
    name = Column(String(100))
    quantity = Column(Float, nullable=False)
    purchase_price = Column(Float)                 # ZAR paid per coin
    purchase_date = Column(DateTime)               # naive midnight (app convention)
    notes = Column(Text)
    source = Column(String(20), nullable=False, default="manual", server_default=text("'manual'"))
    last_synced_at = Column(DateTime)              # Luno rows: last balance sync (UTC)
    created_at = Column(DateTime, default=utcnow_naive)
    updated_at = Column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)

    def __repr__(self) -> str:
        return f"<CryptoHolding({self.quantity:g} {self.symbol})>"
