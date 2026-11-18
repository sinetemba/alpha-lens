from sqlalchemy import Column, Integer, String, Float, DateTime, Text
from .base import Base, utcnow_naive


class KrugerrandHolding(Base):
    """A purchased bullion item: gold/silver Krugerrand coin or minted bar.

    `fine_oz_per_unit` (fine troy ounces of metal per item) is stored
    denormalised so valuation never depends on the dashboard's product
    catalogue - a recorded purchase keeps its metal content even if catalogue
    labels change. `metal` is the GoldAPI code ("XAU"/"XAG").
    """

    __tablename__ = "krugerrand_holdings"

    id = Column(Integer, primary_key=True, index=True)
    product = Column(String(30), nullable=False)   # catalogue key, e.g. "gold_bar"
    metal = Column(String(10), nullable=False)     # "XAU" | "XAG"
    label = Column(String(80), nullable=False)     # e.g. "1 oz", "10 g"
    fine_oz_per_unit = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False, default=1)
    purchase_price = Column(Float)                 # ZAR paid per item
    purchase_date = Column(DateTime)               # naive midnight (app convention)
    notes = Column(Text)
    created_at = Column(DateTime, default=utcnow_naive)
    updated_at = Column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)

    def __repr__(self) -> str:
        return f"<KrugerrandHolding({self.quantity}x {self.label} {self.product})>"
