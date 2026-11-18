"""
Net worth: JSE portfolio + crypto + bullion, in ZAR (Streamlit-free).

Shared by the Home page (live figure) and the headless updater (daily
snapshot), so both value every asset class the same way:
- JSE: the portfolio's revalued `current_value` (services/portfolio_valuation);
- crypto: Luno-synced coins at Luno's ZAR bid, other coins via CoinGecko;
  a Luno Bundle is just its coins' combined value;
- bullion: fine troy ounces x gold/silver spot - the Krugerrands page's
  GoldAPI-sourced stored bars when fresh, else Yahoo futures x USDZAR.
Each part is None when it couldn't be priced, so callers can show "JSE only"
instead of silently counting an asset class as zero.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.collectors.coingecko import CoinGeckoCollector
from app.collectors.luno import LunoCollector, luno_asset_for
from app.collectors.yfinance import YFinanceCollector
from app.models.base import utcnow_naive
from app.models.crypto import CryptoHolding
from app.models.krugerrand import KrugerrandHolding
from app.models.portfolio import Portfolio
from app.models.stock import StockPrice
from app.services.luno_sync import LUNO_SOURCE


@dataclass
class NetWorth:
    jse: Optional[float]
    crypto: Optional[float]
    bullion: Optional[float]

    @property
    def total(self) -> float:
        return sum(v for v in (self.jse, self.crypto, self.bullion) if v)

    @property
    def missing(self) -> list[str]:
        """Asset classes the user holds but that couldn't be priced."""
        return [name for name, v in (("crypto", self.crypto), ("bullion", self.bullion)) if v is None]


def crypto_price(holding, markets: dict, luno_prices: dict) -> Optional[float]:
    """ZAR price per coin: Luno's bid for Luno rows, CoinGecko otherwise.

    A Luno coin with no Luno ZAR route at all falls back to CoinGecko rather
    than showing no value.
    """
    gecko = (markets.get(holding.coin_id) or {}).get("current_price")
    if holding.coin_id == "luno-zar":
        return 1.0
    if holding.source == LUNO_SOURCE:
        return luno_prices.get(luno_asset_for(holding.coin_id)) or gecko
    return gecko


def crypto_value(holdings: list, markets: dict, luno_prices: dict) -> Optional[float]:
    """Combined ZAR value; 0.0 with no holdings, None if none could be priced."""
    if not holdings:
        return 0.0
    values = [
        h.quantity * price
        for h in holdings
        if (price := crypto_price(h, markets, luno_prices))
    ]
    return sum(values) if values else None


def bullion_value(holdings: list, spots: Optional[dict]) -> Optional[float]:
    """fine oz x qty x spot; 0.0 with no holdings, None without spot prices."""
    if not holdings:
        return 0.0
    if not spots:
        return None
    values = [
        h.fine_oz_per_unit * h.quantity * spots[h.metal]
        for h in holdings if spots.get(h.metal)
    ]
    return sum(values) if values else None


def fetch_crypto_prices(holdings: list) -> tuple[dict, dict]:
    """(CoinGecko markets, Luno ZAR prices) for the given holdings; {} on failure."""
    markets, luno_prices = {}, {}
    gecko_ids = sorted({h.coin_id for h in holdings if not h.coin_id.startswith("luno-")})
    if gecko_ids:
        markets = CoinGeckoCollector().get_markets(gecko_ids) or {}
    if any(h.source == LUNO_SOURCE for h in holdings):
        luno_prices = LunoCollector().get_zar_prices() or {}
    return markets, luno_prices


def _stored_metal_spots_zar(db: Session, max_age_hours: int = 48) -> dict:
    """Latest stored XAU/XAG ZAR spot bars written by the Krugerrands page's
    GoldAPI refreshes ({metal: ZAR/oz}) - kept only when fresh.

    Using the page's own source keeps net worth and the Krugerrands page
    agreeing; a stale/absent bar falls back to Yahoo in the caller.
    """
    spots = {}
    cutoff = utcnow_naive() - timedelta(hours=max_age_hours)
    for symbol, metal in (("XAU_ZAR", "XAU"), ("XAG_ZAR", "XAG")):
        row = db.query(StockPrice.close_price, StockPrice.fetched_at).filter(
            StockPrice.symbol == symbol
        ).order_by(StockPrice.fetched_at.desc()).first()
        if row and row.close_price and row.fetched_at and row.fetched_at >= cutoff:
            spots[metal] = row.close_price
    return spots


def compute_net_worth(db: Session, jse_value: Optional[float] = None) -> NetWorth:
    """Value every asset class now. `jse_value` defaults to the stored portfolio value.

    Network failures degrade a part to None (logged) - never raise.
    """
    if jse_value is None:
        portfolio = db.query(Portfolio).first()
        jse_value = portfolio.current_value if portfolio else None

    crypto = db.query(CryptoHolding).all()
    try:
        markets, luno_prices = fetch_crypto_prices(crypto)
        crypto_total = crypto_value(crypto, markets, luno_prices)
    except Exception as e:
        logger.warning(f"Crypto valuation failed: {e}")
        crypto_total = None

    bullion = db.query(KrugerrandHolding).all()
    try:
        spots = _stored_metal_spots_zar(db) if bullion else {}
        if bullion and set(spots) < {"XAU", "XAG"}:
            # Yahoo fills whichever metals lack a fresh stored GoldAPI spot.
            for metal, price in (YFinanceCollector().get_metal_spots_zar() or {}).items():
                spots.setdefault(metal, price)
        bullion_total = bullion_value(bullion, spots or None)
    except Exception as e:
        logger.warning(f"Bullion valuation failed: {e}")
        bullion_total = None

    return NetWorth(jse=jse_value, crypto=crypto_total, bullion=bullion_total)
