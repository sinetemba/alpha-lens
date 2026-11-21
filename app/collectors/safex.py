"""SAFEX (JSE commodity derivatives) grain prices, scraped from Grain SA.

The JSE's own site rejects scripted requests (403). Grain SA's public
"SAFEX Feeds" page renders the day's prices for every grain contract as a
plain server-side HTML table (robots.txt allows it), refreshed after the
12:00 SAST close. Columns: Instrument | Contract | Last Traded Time | Last
Traded Price | Difference | High Price | Low Price | Volume | Open Interest.
Prices are Rand per tonne. High/Low/Volume are 0 for a contract that did
not trade that day (its Last Traded Price is then the mark-to-market).
"""
from datetime import datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup
from loguru import logger

SAFEX_FEED_URL = "https://grainsa.co.za/applied-economics/safex-feeds/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}
_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1
)}


def contract_month(code: str) -> Optional[tuple[int, int]]:
    """"DEC26" -> (2026, 12); None if unparseable."""
    code = (code or "").strip().upper()
    if len(code) != 5 or code[:3] not in _MONTHS or not code[3:].isdigit():
        return None
    return 2000 + int(code[3:]), _MONTHS[code[:3]]


def _number(text: str) -> Optional[float]:
    try:
        return float(text.replace(",", "").replace("\xa0", "").strip())
    except (AttributeError, ValueError):
        return None


def parse_safex_table(html: str) -> list[dict]:
    """Every contract row on the page as a dict; [] when no table matches."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
        if len(cells) < 8 or cells[0] == "Instrument" or contract_month(cells[1]) is None:
            continue
        try:
            traded_at = datetime.strptime(cells[2], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        last = _number(cells[3])
        if not last or last <= 0:
            continue
        rows.append({
            "instrument": cells[0].upper(),
            "contract": cells[1].upper(),
            "traded_at": traded_at,
            "last": last,
            "high": _number(cells[5]) or None,   # 0 = no trade today
            "low": _number(cells[6]) or None,
            "volume": int(_number(cells[7]) or 0),
        })
    return rows


class SafexCollector:
    """Fetches the Grain SA SAFEX table - one request covers every grain."""

    def get_contracts(self) -> Optional[list[dict]]:
        """All contract rows, or None on a fetch/parse failure (so callers
        can tell "source down / page changed" from an empty day)."""
        try:
            response = requests.get(SAFEX_FEED_URL, headers=_HEADERS, timeout=30)
            response.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"SAFEX feed fetch failed: {e}")
            return None
        rows = parse_safex_table(response.text)
        if not rows:
            logger.error("SAFEX feed page had no parseable price table - layout changed?")
            return None
        return rows
