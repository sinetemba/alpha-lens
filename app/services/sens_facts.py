"""Pull the decision-relevant facts out of a SENS announcement's text.

Rule-based and free: JSE corporate-action announcements follow a standard
layout - a "salient dates and times" table, prices as "R 630 000 000" or
"7.2 South African cents per Offer Share", irrevocable undertakings as a
percentage. extract_facts() returns short labelled facts such as
["General meeting Mon 14 Dec 10:00", "Last day to trade Tue 1 Dec",
"Consideration R630m"]; format_facts() joins them for a notification.
It does not explain what a deal means - only the dates and numbers.
"""
import re
from typing import Optional

_WEEKDAYS = "Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
_MONTHS = ("January|February|March|April|May|June|July|August|September|October|"
           "November|December")
# "Tuesday, 1 December", "Monday, 14 December 2026", "15 December 2026"
_DATE = re.compile(
    rf"(?:(?P<wd>{_WEEKDAYS}),?\s+)?(?P<day>\d{{1,2}})\s+(?P<mon>{_MONTHS})(?:\s+(?P<yr>20\d\d))?"
)
_TIME = re.compile(r"\b(?:at\s+)?(?P<t>\d{1,2}:\d{2})\b")

# Salient-date labels: (label, must contain, must not contain, also exclude
# on the NEXT line). Table labels wrap - "Record date to determine which
# Shareholders are entitled / to receive the Circular" is the circular's
# record date, not the voting one - so some exclusions look one line ahead.
_DATE_LABELS = (
    ("General meeting", ("general meeting",), ("results", "forms of proxy", "form of proxy",
                                               "last day", "record date", "circular", "notice of"), False),
    ("Last day to trade", ("last day to trade",), (), False),
    ("Record date", ("record date",), ("receive", "circular"), True),
    ("Proxy deadline", ("proxy",), ("chairperson",), False),
    ("Offer opens", ("offer opens", "opening date of the offer"), (), False),
    ("Offer closes", ("offer closes", "closing date of the offer", "offer will close"), (), False),
    ("Payment date", ("payment date", "paid on"), ("circular",), False),
)
# Display priority when trimming to MAX_FACTS.
_PRIORITY = ("General meeting", "Last day to trade", "Offer closes", "Offer price", "Consideration",
             "Purchase price", "Irrevocable support", "Record date", "Proxy deadline",
             "Offer opens", "Payment date")
MAX_FACTS = 5
_GM_PROSE = re.compile(
    rf"general meeting (?:will|is to) be held[^.]{{0,160}}?(?P<t>\d{{1,2}}:\d{{2}})?[^.]{{0,40}}?"
    rf"on\s+(?P<date>(?:{_WEEKDAYS}),?\s+\d{{1,2}}\s+(?:{_MONTHS})(?:\s+20\d\d)?)",
    re.I | re.S,
)
_RAND = re.compile(
    r"(?P<ctx>consideration|purchase price|offer price)[^.]{0,80}?"
    r"R\s?(?P<amt>\d[\d\s,]*(?:\.\d+)?)\s*(?P<unit>million|billion|bn|m)?\b",
    re.I | re.S,
)
_CENTS = re.compile(
    r"(?P<c>\d+(?:[.,]\d+)?)\s*(?:South African\s+)?cents\s+per\s+(?:\w+\s+)?share", re.I
)
_IRREVOCABLE = re.compile(
    r"irrevocable undertakings?[^.]{0,250}?(?P<pct>\d{1,2}(?:[.,]\d+)?)\s?%", re.I | re.S
)


def _short_date(m: re.Match) -> str:
    wd = (m.group("wd") or "")[:3]
    return f"{wd + ' ' if wd else ''}{int(m.group('day'))} {m.group('mon')[:3]}" + (
        f" {m.group('yr')}" if m.group("yr") else "")


def _rand_text(amount: str, unit: Optional[str]) -> Optional[str]:
    try:
        value = float(re.sub(r"[\s,]", "", amount))
    except ValueError:
        return None
    unit = (unit or "").lower()
    value *= {"million": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9}.get(unit, 1)
    if value < 1e6:   # deal sizes only - per-share prices come from _CENTS
        return None
    for size, suffix in ((1e9, "bn"), (1e6, "m")):
        if value >= size:
            return f"R{value / size:,.1f}".rstrip("0").rstrip(".") + suffix
    return f"R{value:,.0f}"


def _salient_dates(lines: list[str]) -> list[str]:
    facts, used = [], set()
    for i, line in enumerate(lines):
        low = line.lower()
        nxt = lines[i + 1].lower() if i + 1 < len(lines) else ""
        for label, must, must_not, look_ahead in _DATE_LABELS:
            if label in used or not any(k in low for k in must) or any(k in low for k in must_not):
                continue
            if look_ahead and any(k in nxt for k in must_not):
                continue
            # The date sits on the same line or one of the next 2 (wrapped
            # tables) - searched LINE BY LINE so a year is only taken when it
            # is on the date's own line, never borrowed from the next row.
            m = None
            for candidate in lines[i:i + 3]:
                m = _DATE.search(candidate)
                if m:
                    break
            if not m:
                continue
            fact = f"{label} {_short_date(m)}"
            t = _TIME.search(" ".join(lines[i:i + 2]))
            if label in ("General meeting", "Proxy deadline") and t:
                fact += f" {t.group('t')}"
            facts.append(fact)
            used.add(label)
            break
    return facts


def _rank(fact: str) -> int:
    return next((i for i, p in enumerate(_PRIORITY) if fact.startswith(p)), len(_PRIORITY))


def extract_facts(text: Optional[str]) -> list[str]:
    """Labelled key facts from an announcement's text ([] if none found)."""
    if not text:
        return []
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.splitlines() if l.strip()]
    facts = _salient_dates(lines)
    flat = re.sub(r"\s+", " ", text)

    if not any(f.startswith("General meeting") for f in facts):
        m = _GM_PROSE.search(flat)
        if m:
            d = _DATE.search(m.group("date"))
            facts.insert(0, f"General meeting {_short_date(d)}" + (f" {m.group('t')}" if m.group("t") else ""))

    m = _CENTS.search(flat)
    if m:
        facts.append(f"Offer price {m.group('c').replace(',', '.')}c per share")
    m = _RAND.search(flat)
    if m:
        amount = _rand_text(m.group("amt"), m.group("unit"))
        if amount:
            facts.append(f"{m.group('ctx').capitalize()} {amount}")
    m = _IRREVOCABLE.search(flat)
    if m:
        facts.append(f"Irrevocable support {m.group('pct').replace(',', '.')}%")
    return sorted(facts, key=_rank)[:MAX_FACTS]


def price_facts(facts: list[str]) -> list[str]:
    """Only the money/undertaking facts - for borrowing from an earlier
    announcement whose timetable may since have been withdrawn."""
    return [f for f in facts if f.startswith(("Offer price", "Consideration", "Purchase price",
                                              "Irrevocable"))]


def format_facts(facts: list[str]) -> str:
    return " · ".join(facts)
