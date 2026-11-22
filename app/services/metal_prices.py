"""Live gold/silver spot: Yahoo first, GoldAPI as a fallback.

Yahoo (COMEX futures x USD/ZAR) is free and quota-less and matches GoldAPI's
spot to ~0.01-0.1%. GoldAPI (100 requests/month) only fills a metal Yahoo
didn't return, and the collector skips it for hours after an auth/quota
error. Streamlit-free; outcomes are recorded as source-health heartbeats.
"""
from loguru import logger

from app.collectors.gold_api import GoldAPICollector
from app.collectors.yfinance import YFinanceCollector

YAHOO_SOURCE = "Yahoo Finance (COMEX futures x USD/ZAR)"
GOLDAPI_SOURCE = "GoldAPI"


def _record(key: str, ok: bool, detail=None) -> None:
    try:
        from app.models.base import SessionLocal
        from app.services.source_health import record_source
        db = SessionLocal()
        try:
            record_source(db, key, ok, detail)
        finally:
            db.close()
    except Exception as e:   # health recording must never break pricing
        logger.debug(f"Could not record metal source health: {e}")


def live_metal_spots(yahoo=None, gold_api=None) -> dict:
    """{"XAU"/"XAG": {"usd": float, "zar": float, "source": str}}; {} if all failed."""
    yahoo = yahoo or YFinanceCollector()
    spots = {}
    fetched = yahoo.get_metal_spots() or {}
    for metal, prices in fetched.items():
        spots[metal] = {**prices, "source": YAHOO_SOURCE}
    _record("yahoo_metals", bool(fetched), None if fetched else "gold/silver futures download failed")

    missing = [m for m in ("XAU", "XAG") if m not in spots]
    gold_api = gold_api or GoldAPICollector()
    if missing and gold_api.enabled and not gold_api.blocked():
        for metal in missing:
            usd = gold_api.get_live_price(metal=metal, currency="USD")
            zar = gold_api.get_live_price(metal=metal, currency="ZAR")
            try:
                usd_p, zar_p = float(usd["price"]), float(zar["price"])
            except (TypeError, KeyError, ValueError):
                continue
            if usd_p > 0 and zar_p > 0:
                spots[metal] = {"usd": usd_p, "zar": zar_p, "source": GOLDAPI_SOURCE}
        _record("goldapi", all(m in spots for m in missing), gold_api.last_error)
    return spots
