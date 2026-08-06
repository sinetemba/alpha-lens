import time
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
import requests
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session
from app.config.settings import settings
from app.models.dividend import MoneywebDividendWatch
from loguru import logger


class MoneywebCollector:
    """Collector for scraping authenticated Moneyweb data pages.

    Moneyweb does not expose an official API for subscriber-only data, so this
    logs in through the public WordPress /wp-login.php form and reuses the
    resulting session cookie for subsequent requests.
    """

    def __init__(self, max_retries: int = 3, base_delay: float = 1.0):
        self.username = settings.moneyweb_username
        self.password = settings.moneyweb_password
        self.enabled = bool(self.username and self.password)
        self.last_error: Optional[str] = None
        self.max_retries = max_retries
        self.base_delay = base_delay
        self._session: Optional[requests.Session] = None
        self._authenticated = False

    def _get_session(self) -> requests.Session:
        """Return a reusable requests Session."""
        if self._session is not None:
            return self._session

        s = requests.Session()
        s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.5",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        })
        self._session = s
        return s

    def _call_with_retry(self, label: str, fn) -> Optional[Any]:
        """Run a callable with exponential-backoff retries."""
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

    def _login(self) -> bool:
        """Log in to Moneyweb and store the authenticated session."""
        if not self.enabled:
            logger.warning("Moneyweb credentials not configured")
            return False

        if self._authenticated:
            return True

        def _do_login():
            s = self._get_session()
            login_url = "https://www.moneyweb.co.za/wp-login.php"

            # Hit the login page first so the WordPress test cookie is set.
            s.get(login_url, timeout=20)

            data = {
                "log": self.username,
                "pwd": self.password,
                "wp-submit": "Log In",
                "redirect_to": "https://www.moneyweb.co.za/wp-admin/",
                "testcookie": "1",
                "rememberme": "forever",
            }
            r = s.post(login_url, data=data, timeout=20, allow_redirects=False)

            # WordPress returns a 302 redirect on successful login.
            if r.status_code in (302, 303):
                self._authenticated = True
                return True

            text = r.text.lower()
            if "login_error" in text or "invalid" in text or "incorrect" in text:
                raise Exception("Login failed: invalid credentials")
            if r.status_code >= 400:
                raise Exception(f"Login failed with HTTP {r.status_code}")
            raise Exception("Login failed: unexpected response")

        return bool(self._call_with_retry("Moneyweb login", _do_login))

    def get_dividend_watch(self) -> Optional[List[Dict[str, Any]]]:
        """Return the authenticated Dividend Watch table as a list of dicts."""
        if not self._login():
            return None

        def _do_fetch():
            s = self._get_session()
            url = "https://www.moneyweb.co.za/tools-and-data/dividend-watch/"
            r = s.get(url, timeout=30)
            r.raise_for_status()

            soup = BeautifulSoup(r.text, "html.parser")
            table = soup.find("table", {"id": "dividend-watch-table"})
            if not table:
                raise Exception("Dividend watch table not found in page")

            thead = table.find("thead")
            tbody = table.find("tbody")
            if not thead or not tbody:
                raise Exception("Dividend watch table missing thead/tbody")

            headers = [th.get_text(strip=True) for th in thead.find_all("th")]
            rows = []
            for tr in tbody.find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all("td")]
                if not cells or not any(cells):
                    continue
                row = {h: v for h, v in zip(headers, cells) if h}
                rows.append(row)

            return rows

        return self._call_with_retry("Moneyweb dividend watch", _do_fetch)

    def save_dividend_watch(self, db: Session) -> int:
        """Fetch the latest Moneyweb Dividend Watch and persist it to the DB."""
        rows = self.get_dividend_watch()
        if rows is None:
            return 0

        now = datetime.now(timezone.utc)
        watch_rows = []
        for row in rows:
            instrument = str(row.get("Instrument", "")).strip()
            if not instrument:
                continue

            watch_rows.append(MoneywebDividendWatch(
                instrument=instrument,
                declared_date=self._parse_date(row.get("Declared date")),
                last_day_to_trade=self._parse_date(row.get("Last day to trade")),
                pay_date=self._parse_date(row.get("Pay date")),
                dividend_type=row.get("Type"),
                value=row.get("Value"),
                fetched_at=now,
            ))

        if not watch_rows:
            return 0

        db.query(MoneywebDividendWatch).delete()
        db.add_all(watch_rows)
        db.commit()
        return len(watch_rows)

    @staticmethod
    def _parse_date(value: Optional[Any]) -> Optional[date]:
        """Parse a Moneyweb date string (yyyy/mm/dd) into a Python date."""
        if not value:
            return None
        try:
            return datetime.strptime(str(value).strip(), "%Y/%m/%d").date()
        except ValueError:
            return None

    def get_winners_and_losers(self) -> Optional[Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]]:
        """Return the daily top winners and losers from Moneyweb as (winners, losers)."""
        if not self._login():
            return None

        def _do_fetch():
            s = self._get_session()
            url = "https://cache.moneyweb.co.za/mny-mover-tables.php"
            headers = {
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://www.moneyweb.co.za/tools-and-data/latest-winners-and-losers/",
                "Origin": "https://www.moneyweb.co.za",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            }

            winners = self._fetch_mover_table(s, url, "winnersfull", "winners-feed-table", headers)
            losers = self._fetch_mover_table(s, url, "losersfull", "losers-feed-table", headers)
            return winners, losers

        return self._call_with_retry("Moneyweb winners and losers", _do_fetch)

    def _fetch_mover_table(
        self,
        s: requests.Session,
        url: str,
        feed_id: str,
        table_id: str,
        headers: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        r = s.post(
            url,
            data={
                "action": "mover_tables",
                "tableId": table_id,
                "records": "all",
                "tableType": "data-feed",
                "act": feed_id,
            },
            headers=headers,
            timeout=30,
        )
        r.raise_for_status()

        raw_rows = r.json()
        rows = []
        for row in raw_rows:
            if len(row) < 4:
                continue

            share = self._parse_share_cell(row[0])
            rows.append({
                "Symbol": share["symbol"],
                "Name": share["name"],
                "Price": self._parse_number(row[1]),
                "Move": self._parse_number(row[2]),
                "Change": self._parse_percent(row[3]),
            })
        return rows

    @staticmethod
    def _parse_share_cell(html: str) -> Dict[str, str]:
        """Extract the share code and name from the Moneyweb HTML share cell."""
        soup = BeautifulSoup(html, "html.parser")
        a = soup.find("a")
        if a:
            href = a.get("href", "")
            symbol = href.rstrip("/").split("/")[-1] if href else ""
            name = a.get_text(strip=True)
            return {"symbol": symbol, "name": name}
        text = soup.get_text(strip=True)
        return {"symbol": text, "name": text}

    @staticmethod
    def _clean_html_text(html: str) -> str:
        """Strip HTML tags and extra whitespace from a cell value."""
        return BeautifulSoup(str(html), "html.parser").get_text(strip=True)

    @staticmethod
    def _parse_number(value: Any) -> Optional[float]:
        """Parse a Moneyweb numeric cell into a float."""
        text = MoneywebCollector._clean_html_text(value).replace(",", "").strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    @staticmethod
    def _parse_percent(value: Any) -> Optional[float]:
        """Parse a Moneyweb percentage cell like '13.76%' into a float."""
        text = MoneywebCollector._clean_html_text(value).replace("%", "").replace(",", "").strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
