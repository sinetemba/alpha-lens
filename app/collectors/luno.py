import time
from typing import Optional

import requests
from loguru import logger

from app.config.settings import settings


# Luno asset code -> (display symbol, name, CoinGecko id). Luno calls Bitcoin
# "XBT"; the CoinGecko id lets Luno-synced coins still show a 24h change.
# Covers every Luno ZAR pair as of 2026-11-18; unknown assets still sync,
# keyed "luno-<asset>" and priced by Luno alone.
LUNO_ASSETS = {
    "XBT": ("BTC", "Bitcoin", "bitcoin"),
    "ETH": ("ETH", "Ethereum", "ethereum"),
    "SOL": ("SOL", "Solana", "solana"),
    "XRP": ("XRP", "XRP", "ripple"),
    "ADA": ("ADA", "Cardano", "cardano"),
    "DOGE": ("DOGE", "Dogecoin", "dogecoin"),
    "TRX": ("TRX", "TRON", "tron"),
    "AVAX": ("AVAX", "Avalanche", "avalanche-2"),
    "LINK": ("LINK", "Chainlink", "chainlink"),
    "DOT": ("DOT", "Polkadot", "polkadot"),
    "LTC": ("LTC", "Litecoin", "litecoin"),
    "BCH": ("BCH", "Bitcoin Cash", "bitcoin-cash"),
    "XLM": ("XLM", "Stellar", "stellar"),
    "UNI": ("UNI", "Uniswap", "uniswap"),
    "AAVE": ("AAVE", "Aave", "aave"),
    "ALGO": ("ALGO", "Algorand", "algorand"),
    "ATOM": ("ATOM", "Cosmos", "cosmos"),
    "CRV": ("CRV", "Curve DAO", "curve-dao-token"),
    "INJ": ("INJ", "Injective", "injective-protocol"),
    "POL": ("POL", "Polygon", "polygon-ecosystem-token"),
    "SKY": ("SKY", "Sky", "sky"),
    "SNX": ("SNX", "Synthetix", "havven"),
    "SONIC": ("S", "Sonic", "sonic-3"),
    "USDC": ("USDC", "USD Coin", "usd-coin"),
    "USDT": ("USDT", "Tether", "tether"),
    # No ZAR pair - priced via BNBUSDT x USDTZAR (see get_zar_prices).
    "BNB": ("BNB", "BNB", "binancecoin"),
}

# Fiat balances held on Luno - cash, not a crypto holding.
FIAT_ASSETS = {"ZAR", "NGN", "MYR", "IDR", "UGX", "EUR", "GBP", "USD"}


def luno_coin_id(asset: str) -> str:
    """Stable holding key for a Luno asset (CoinGecko id when known)."""
    known = LUNO_ASSETS.get(asset)
    return known[2] if known else f"luno-{asset.lower()}"


def luno_asset_for(coin_id: str) -> str:
    """Inverse of `luno_coin_id`: the Luno asset code behind a holding key."""
    for asset, (_, _, gecko_id) in LUNO_ASSETS.items():
        if gecko_id == coin_id:
            return asset
    return coin_id.removeprefix("luno-").upper()


class LunoCollector:
    """Read-only adapter for the Luno exchange REST API.

    Balances need an API key (HTTP basic auth: key id + secret); tickers are
    public. Only GET endpoints are used - a key with balance-read permission
    is enough, and nothing here can trade or withdraw.
    """

    BASE_URL = "https://api.luno.com/api/1"

    def __init__(self):
        self.key_id = settings.luno_api_key_id
        self.secret = settings.luno_api_secret
        self.last_error: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return bool(self.key_id and self.secret)

    def _get(self, path: str, auth: bool, max_retries: int = 2) -> Optional[dict]:
        """GET a Luno endpoint; None on failure with `last_error` set.

        Auth errors are not retried. Messages never include the credentials
        (they travel in the Authorization header, not the URL).
        """
        self.last_error = None
        for attempt in range(max_retries + 1):
            try:
                response = requests.get(
                    f"{self.BASE_URL}/{path}",
                    auth=(self.key_id, self.secret) if auth else None,
                    timeout=20,
                )
            except requests.exceptions.RequestException as e:
                self.last_error = f"Luno unreachable ({e.__class__.__name__})"
            else:
                if response.status_code in (401, 403):
                    try:
                        code = response.json().get("error_code", "")
                    except ValueError:
                        code = ""
                    self.last_error = (
                        "Luno rejected the API key - check LUNO_API_KEY_ID / LUNO_API_SECRET"
                        + (f" ({code})" if code else "")
                    )
                    logger.error(self.last_error)
                    return None
                if response.status_code < 500:
                    try:
                        response.raise_for_status()
                        return response.json()
                    except (requests.exceptions.HTTPError, ValueError) as e:
                        self.last_error = f"Luno request failed ({e.__class__.__name__})"
                        logger.error(f"Luno {path}: {self.last_error}")
                        return None
                self.last_error = f"Luno returned HTTP {response.status_code}"
            if attempt < max_retries:
                time.sleep(0.5 * (2 ** attempt))
        logger.error(f"Luno {path}: {self.last_error}")
        return None

    def get_account_balances(self) -> Optional[list[tuple[str, str, float]]]:
        """(asset, account_type, balance) per Luno account; None on failure.

        `account_type` is "TRANSACTIONAL" for normal wallets and "BUNDLE" for
        coins held inside a Luno Bundle - the same asset can have one of
        each. `balance` already includes funds reserved in open orders. Zero
        balances are kept so callers can tell "sold out" from "missing".
        """
        if not self.enabled:
            self.last_error = "Luno API key not configured"
            return None
        data = self._get("balance", auth=True)
        if data is None:
            return None
        accounts = []
        for account in data.get("balance") or []:
            asset = (account.get("asset") or "").upper()
            if not asset:
                continue
            try:
                amount = float(account.get("balance") or 0)
            except (TypeError, ValueError):
                continue
            accounts.append((asset, (account.get("account_type") or "TRANSACTIONAL").upper(), amount))
        return accounts

    def get_balances(self) -> Optional[dict[str, float]]:
        """Total balance per asset across all accounts (wallet + bundle)."""
        accounts = self.get_account_balances()
        if accounts is None:
            return None
        totals: dict[str, float] = {}
        for asset, _, amount in accounts:
            totals[asset] = totals.get(asset, 0.0) + amount
        return totals

    def get_zar_prices(self) -> Optional[dict[str, float]]:
        """ZAR price per asset from Luno's public tickers; None on failure.

        Valued at the best bid - what Luno would pay right now. The last
        trade is unreliable on thin pairs (DOTZAR last traded at R3 with a
        R12 bid and no 24h volume), so it is only a fallback.

        Assets with no ZAR pair but a USDT one (e.g. BNB) are priced at the
        cross rate XXXUSDT bid x USDTZAR bid - selling via USDT is how you'd
        actually cash them out on Luno.
        """
        data = self._get("tickers", auth=False)
        if data is None:
            return None
        zar: dict[str, float] = {}
        usdt: dict[str, float] = {}
        for ticker in data.get("tickers") or []:
            pair = ticker.get("pair") or ""
            if ticker.get("status") not in (None, "ACTIVE"):
                continue
            if pair.endswith("ZAR"):
                book, asset = zar, pair[:-3]
            elif pair.endswith("USDT"):
                book, asset = usdt, pair[:-4]
            else:
                continue
            for field in ("bid", "last_trade"):
                try:
                    price = float(ticker.get(field) or 0)
                except (TypeError, ValueError):
                    price = 0.0
                if price > 0:
                    book[asset] = price
                    break
        usdt_zar = zar.get("USDT")
        if usdt_zar:
            for asset, price in usdt.items():
                zar.setdefault(asset, price * usdt_zar)
        return zar
