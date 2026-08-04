import json
import time
from typing import Any, List, Dict, Optional
from app.config.settings import settings
from loguru import logger


# EasyEquities contract codes look like "EQU.ZA.NPN" or "ETF.ZA.SYCP".
# The first segment is a rough instrument-class indicator.
_INSTRUMENT_TYPE_MAP = {
    "EQU": "Equity",
    "ETF": "ETF",
    "FND": "Fund",
    "PRO": "Property",
    "BND": "Bond",
    "CASH": "Cash",
}


class EasyEquitiesCollector:
    """Collector for pulling real holdings from a user's EasyEquities account.

    EasyEquities has no official public API, so this drives the same HTTP
    endpoints the web platform itself uses (via the unofficial
    `easy-equities-client` package), authenticated with the account's own
    login credentials.

    This class acts as a thin, defensive adapter: it retries transient
    failures, masks credentials in logs, and surfaces clear error messages.
    """

    def __init__(self, max_retries: int = 3, base_delay: float = 1.0):
        self.username = settings.easyequities_username
        self.password = settings.easyequities_password
        self.enabled = bool(self.username and self.password)
        self.last_error: str | None = None
        self.max_retries = max_retries
        self.base_delay = base_delay
        self._last_funds_raw: Dict[str, Any] = {}

    def _call_with_retry(self, label: str, fn):
        """Run a callable with exponential-backoff retries.

        Catches all Exceptions because the third-party client can raise a
        variety of transport/parsing errors. Logs are careful not to expose
        credentials.
        """
        last_exception: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                result = fn()
                if attempt > 1:
                    logger.info(f"{label} succeeded on attempt {attempt}")
                self.last_error = None
                return result
            except Exception as e:
                last_exception = e
                logger.warning(f"{label} failed on attempt {attempt}: {e}")
                if attempt < self.max_retries:
                    sleep_seconds = self.base_delay * (2 ** (attempt - 1))
                    logger.info(f"Retrying {label} in {sleep_seconds:.1f}s")
                    time.sleep(sleep_seconds)

        self.last_error = str(last_exception) if last_exception else "unknown error"
        logger.error(f"{label} failed after {self.max_retries} attempts")
        return None

    def _get_client(self):
        """Log in and return an authenticated client."""
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
            return client
        except Exception as e:
            logger.error(f"Error creating EasyEquities client: {e}")
            return None

    def _login(self, client) -> bool:
        """Authenticate the client. Returns True on success."""
        def _do_login():
            client.login(username=self.username, password=self.password)
            return True

        return bool(self._call_with_retry("EasyEquities login", _do_login))

    def list_accounts(self) -> Optional[List[Dict]]:
        """List the trust accounts (e.g. ZAR, TFSA, USD) on the EasyEquities profile."""
        client = self._get_client()
        if not client or not self._login(client):
            return None

        def _do_list():
            accounts = client.accounts.list()
            parsed = [
                {"id": a.id, "name": a.name, "trading_currency_id": a.trading_currency_id}
                for a in accounts
            ]
            logger.info(f"EasyEquities returned {len(parsed)} account(s)")
            return parsed

        return self._call_with_retry("EasyEquities list accounts", _do_list)

    def get_holdings(self, account_id: str) -> Optional[List[Dict]]:
        """Get holdings for a given account, with amounts parsed to floats."""
        client = self._get_client()
        if not client or not self._login(client):
            return None

        def _do_get():
            holdings = client.accounts.holdings(account_id, include_shares=True)
            parsed = [self._parse_holding(h) for h in holdings]
            logger.info(f"EasyEquities returned {len(parsed)} holding(s) for account {account_id}")
            return parsed

        return self._call_with_retry(f"EasyEquities get holdings ({account_id})", _do_get)

    def get_all_holdings(self) -> Optional[List[Dict]]:
        """Get holdings for every account and tag each row with its account name."""
        client = self._get_client()
        if not client or not self._login(client):
            return None

        def _do_get_all():
            parsed_holdings = []
            for account in client.accounts.list():
                account_name = account.name or ""
                holdings = client.accounts.holdings(account.id, include_shares=True)
                for holding in holdings:
                    parsed = self._parse_holding(holding)
                    parsed["account_name"] = account_name
                    parsed_holdings.append(parsed)
            logger.info(f"EasyEquities returned {len(parsed_holdings)} holding(s) across all accounts")
            return parsed_holdings

        return self._call_with_retry("EasyEquities get all holdings", _do_get_all)

    def get_funds_to_invest_by_account(self) -> Optional[Dict[str, float]]:
        """Return a mapping of account name -> Funds to Invest value (ZAR) for each account."""
        client = self._get_client()
        if not client or not self._login(client):
            return None

        accounts = self._call_with_retry("EasyEquities list accounts for funds", client.accounts.list)
        if not accounts:
            return None

        from easy_equities_client import constants

        funds: Dict[str, float] = {}
        self._last_funds_raw = {}
        for account in accounts:
            def _fetch(account=account):
                client.accounts._switch_account(account.id)
                response = client.session.get(
                    client.accounts._url(
                        constants.PLATFORM_ACCOUNT_VALUATIONS_PATH,
                        query=f"trustAccountId={account.id}",
                    )
                )
                response.raise_for_status()
                data = response.json()
                # Some ASP.NET endpoints wrap the JSON payload in a JSON string.
                if isinstance(data, str):
                    data = json.loads(data)
                self._last_funds_raw[account.name] = data
                return self._extract_funds_to_invest(data)

            value = self._call_with_retry(
                f"EasyEquities funds to invest for {account.name}", _fetch
            )
            if value is not None:
                funds[account.name] = value
        return funds

    _FUNDS_KEYWORDS = (
        "funds to invest",
        "funds available",
        "available to invest",
        "buying power",
        "cash",
        "free cash",
    )

    @staticmethod
    def _parse_funds_value(value) -> Optional[float]:
        """Turn a string/number/currency value into a float, or None if not parseable."""
        if isinstance(value, (int, float)):
            return float(value)
        if not isinstance(value, str) or not value.strip():
            return None

        text = value.strip().replace(",", "")
        # Try the package's currency parser first (e.g. "R 1 234.56")
        try:
            from easy_equities_client.accounts.parsers import get_amount_and_currency_from_string
            parsed = get_amount_and_currency_from_string(text)
            numeric = parsed[2] if len(parsed) > 2 and parsed[2] is not None else None
            return float(numeric) if numeric is not None else None
        except Exception:
            pass

        # Fallback: plain number like "1234.56" or "1234.56 ZAR"
        try:
            return float(text)
        except ValueError:
            return None

    @staticmethod
    def _extract_funds_to_invest(data) -> Optional[float]:
        """Recursively search a valuations JSON for a 'Funds to Invest' style line item."""
        if isinstance(data, dict):
            # Some endpoints return { "Funds to Invest": "R 123.45" }
            for key, value in data.items():
                if isinstance(key, str) and any(k in key.lower() for k in EasyEquitiesCollector._FUNDS_KEYWORDS):
                    parsed = EasyEquitiesCollector._parse_funds_value(value)
                    if parsed is not None:
                        return parsed

            # Others return [ { "Label": "Funds to Invest", "Value": "R 123.45" } ]
            label = data.get("Label", "")
            value = data.get("Value")
            if label and value is not None and any(k in str(label).lower() for k in EasyEquitiesCollector._FUNDS_KEYWORDS):
                parsed = EasyEquitiesCollector._parse_funds_value(value)
                if parsed is not None:
                    return parsed

            for value in data.values():
                result = EasyEquitiesCollector._extract_funds_to_invest(value)
                if result is not None:
                    return result
        elif isinstance(data, list):
            for item in data:
                result = EasyEquitiesCollector._extract_funds_to_invest(item)
                if result is not None:
                    return result
        return None

    @staticmethod
    def _parse_holding(holding: Dict) -> Dict:
        """Convert a raw holding dict (string amounts, image-derived contract code) into numeric fields."""
        from easy_equities_client.accounts.parsers import get_amount_and_currency_from_string

        def parse_amount(value: str) -> float:
            if not value:
                return 0.0
            try:
                parsed = get_amount_and_currency_from_string(value)
                numeric = parsed[2] if len(parsed) > 2 and parsed[2] is not None else None
                return float(numeric) if numeric is not None else 0.0
            except Exception as e:
                logger.warning(f"Could not parse EasyEquities amount '{value}': {e}")
                return 0.0

        contract_code = holding.get("contract_code", "")
        # Contract codes look like "EQU.ZA.NPN" - the JSE ticker is usually the last segment.
        symbol = contract_code.split(".")[-1] if contract_code else ""
        instrument_prefix = contract_code.split(".")[0] if contract_code else ""
        instrument_type = _INSTRUMENT_TYPE_MAP.get(instrument_prefix, instrument_prefix or "Unknown")

        shares_str = (holding.get("shares") or "0").replace(",", "").strip()
        try:
            shares = float(shares_str)
        except ValueError:
            logger.warning(f"Could not parse EasyEquities shares '{shares_str}'")
            shares = 0.0

        purchase_value = parse_amount(holding.get("purchase_value", ""))
        current_price = parse_amount(holding.get("current_price", ""))
        current_value = parse_amount(holding.get("current_value", ""))

        return {
            "symbol": symbol,
            "name": holding.get("name", ""),
            "contract_code": contract_code,
            "instrument_type": instrument_type,
            "isin": holding.get("isin", ""),
            "shares": shares,
            "purchase_value": purchase_value,
            "purchase_price": (purchase_value / shares) if shares else 0.0,
            "current_price": current_price,
            "current_value": current_value,
        }
