from typing import List, Dict, Optional
from app.config.settings import settings
from loguru import logger


class EasyEquitiesCollector:
    """Collector for pulling real holdings from a user's EasyEquities account.

    EasyEquities has no official public API, so this drives the same HTTP
    endpoints the web platform itself uses (via the unofficial
    `easy-equities-client` package), authenticated with the account's own
    login credentials.
    """

    def __init__(self):
        self.username = settings.easyequities_username
        self.password = settings.easyequities_password
        self.enabled = bool(self.username and self.password)

    def _get_client(self):
        if not self.enabled:
            logger.warning("EasyEquities credentials not configured")
            return None

        try:
            from easy_equities_client.clients import EasyEquitiesClient
            client = EasyEquitiesClient()
            # EasyEquities' CDN (CloudFront) returns 403 for requests with no/bare
            # User-Agent header (e.g. the default "python-requests/x.x"), so a
            # browser-like one is required to get past it.
            client.session.headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
            client.login(username=self.username, password=self.password)
            return client
        except Exception as e:
            logger.error(f"Error logging into EasyEquities: {e}")
            return None

    def list_accounts(self) -> Optional[List[Dict]]:
        """List the trust accounts (e.g. ZAR, TFSA, USD) on the EasyEquities profile."""
        client = self._get_client()
        if not client:
            return None

        try:
            accounts = client.accounts.list()
            return [
                {"id": a.id, "name": a.name, "trading_currency_id": a.trading_currency_id}
                for a in accounts
            ]
        except Exception as e:
            logger.error(f"Error listing EasyEquities accounts: {e}")
            return None

    def get_holdings(self, account_id: str) -> Optional[List[Dict]]:
        """Get holdings for a given account, with amounts parsed to floats."""
        client = self._get_client()
        if not client:
            return None

        try:
            holdings = client.accounts.holdings(account_id, include_shares=True)
            return [self._parse_holding(h) for h in holdings]
        except Exception as e:
            logger.error(f"Error fetching EasyEquities holdings for account {account_id}: {e}")
            return None

    @staticmethod
    def _parse_holding(holding: Dict) -> Dict:
        """Convert a raw holding dict (string amounts, image-derived contract code) into numeric fields."""
        from easy_equities_client.accounts.parsers import get_amount_and_currency_from_string

        def parse_amount(value: str) -> float:
            try:
                return get_amount_and_currency_from_string(value)[2]
            except Exception:
                return 0.0

        contract_code = holding.get("contract_code", "")
        # Contract codes look like "EQU.ZA.NPN" - the JSE ticker is usually the last segment.
        symbol = contract_code.split(".")[-1] if contract_code else ""

        shares_str = (holding.get("shares") or "0").replace(",", "").strip()
        try:
            shares = float(shares_str)
        except ValueError:
            shares = 0.0

        purchase_value = parse_amount(holding.get("purchase_value", ""))

        return {
            "symbol": symbol,
            "name": holding.get("name", ""),
            "contract_code": contract_code,
            "isin": holding.get("isin", ""),
            "shares": shares,
            "purchase_value": purchase_value,
            "purchase_price": (purchase_value / shares) if shares else 0.0,
            "current_price": parse_amount(holding.get("current_price", "")),
            "current_value": parse_amount(holding.get("current_value", "")),
        }
