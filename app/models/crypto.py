from sqlalchemy import Column, Integer, String, Float, DateTime, Text
from .base import Base, utcnow_naive


class CryptoHolding(Base):
    """A purchased cryptocurrency position.

    `coin_id` is the CoinGecko id (e.g. "bitcoin", "solana") - the canonical
    key for price lookups; `symbol` is display-only. Prices are valued in
    ZAR via the CoinGecko markets endpoint.
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
    created_at = Column(DateTime, default=utcnow_naive)
    updated_at = Column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)

    def __repr__(self) -> str:
        return f"<CryptoHolding({self.quantity:g} {self.symbol})>"
