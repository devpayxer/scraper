"""Turn the raw text scraped off a listing card into typed values.

Everything here is pure string -> value, no I/O, so it is directly testable
against real strings copied out of eBay HTML.
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata

# ------------------------------------------------------------------ money

# "$84.15", "US $1,234.56", "C $45.00", "GBP 12.00", "EUR 9,99"
_MONEY_RE = re.compile(
    r"(?P<cur>US\s*\$|C\s*\$|AU\s*\$|GBP|EUR|CAD|AUD|USD|[$£€])\s*"
    r"(?P<amt>\d[\d.,]*)",
    re.IGNORECASE,
)

_CURRENCY_MAP = {
    "$": "USD", "us $": "USD", "usd": "USD",
    "c $": "CAD", "cad": "CAD",
    "au $": "AUD", "aud": "AUD",
    "£": "GBP", "gbp": "GBP",
    "€": "EUR", "eur": "EUR",
}


def clean_text(value: str | None) -> str:
    """Collapse whitespace and normalise the unicode eBay sprinkles in."""
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("​", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def _to_float(amount: str) -> float | None:
    """Parse a localized number. Handles 1,234.56 and 1.234,56."""
    amount = amount.strip().rstrip(".,")
    if not amount:
        return None
    if "," in amount and "." in amount:
        # whichever separator is last is the decimal one
        dec = "," if amount.rfind(",") > amount.rfind(".") else "."
        thou = "." if dec == "," else ","
        amount = amount.replace(thou, "").replace(dec, ".")
    elif "," in amount:
        head, _, tail = amount.rpartition(",")
        # "1,50" is decimal; "1,500" is thousands
        amount = f"{head}.{tail}" if len(tail) == 2 and head else amount.replace(",", "")
    try:
        return round(float(amount), 2)
    except ValueError:
        return None


def parse_money(text: str | None) -> tuple[float | None, str | None]:
    """Extract the first money amount from `text`.

    >>> parse_money("$84.15")
    (84.15, 'USD')
    >>> parse_money("US $1,234.56")
    (1234.56, 'USD')
    """
    text = clean_text(text)
    if not text:
        return None, None
    match = _MONEY_RE.search(text)
    if not match:
        return None, None
    cur_raw = re.sub(r"\s+", " ", match.group("cur").strip().lower())
    return _to_float(match.group("amt")), _CURRENCY_MAP.get(cur_raw, cur_raw.upper())


def parse_price_range(text: str | None) -> tuple[float | None, str | None]:
    """Sold price. Auction ranges ("$10.00 to $20.00") collapse to the low end."""
    return parse_money(text)


# --------------------------------------------------------------- shipping

_FREE_RE = re.compile(r"\bfree\b.{0,20}\b(shipping|delivery|postage)\b", re.IGNORECASE)
_UNSPECIFIED_RE = re.compile(
    r"(shipping|delivery)\s+not\s+specified|does\s+not\s+ship|local\s+pickup|freight",
    re.IGNORECASE,
)


def parse_shipping(text: str | None) -> tuple[float | None, str]:
    """Return (cost, status).

    status is one of: free | paid | unspecified | unknown.
    `cost` is 0.0 for free, None when eBay did not say.

    >>> parse_shipping("+$56.99 delivery in 2-4 days")
    (56.99, 'paid')
    >>> parse_shipping("Free delivery")
    (0.0, 'free')
    >>> parse_shipping("Shipping not specified")
    (None, 'unspecified')
    """
    text = clean_text(text)
    if not text:
        return None, "unknown"
    if _FREE_RE.search(text):
        return 0.0, "free"
    amount, _ = parse_money(text)
    if amount is not None:
        return amount, "paid"
    if _UNSPECIFIED_RE.search(text):
        return None, "unspecified"
    return None, "unknown"


# ------------------------------------------------------------------- dates

_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1
    )
}
_MONTHS.update({
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7,
    "august": 8, "september": 9, "sept": 9, "october": 10, "november": 11, "december": 12,
})

# "Sold Aug 31, 2026" / "Sold  31 Aug 2026" / "Sold Aug 31"
_DATE_MDY_RE = re.compile(
    r"(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:\s*,\s*(?P<year>\d{4}))?"
)
_DATE_DMY_RE = re.compile(
    r"(?P<day>\d{1,2})\s+(?P<mon>[A-Za-z]{3,9})\.?(?:\s+(?P<year>\d{4}))?"
)
_ISO_RE = re.compile(r"(?P<year>\d{4})-(?P<mon>\d{2})-(?P<day>\d{2})")


def parse_sold_date(text: str | None, today: dt.date | None = None) -> dt.date | None:
    """Parse eBay's "Sold Aug 31, 2026" label into a date.

    When eBay omits the year (it does for recent sales in some layouts) the most
    recent past occurrence of that month/day is assumed -- never a future date.

    >>> parse_sold_date("Sold Aug 31, 2026")
    datetime.date(2026, 8, 31)
    """
    text = clean_text(text)
    if not text:
        return None
    today = today or dt.date.today()

    iso = _ISO_RE.search(text)
    if iso:
        try:
            return dt.date(int(iso["year"]), int(iso["mon"]), int(iso["day"]))
        except ValueError:
            return None

    for pattern in (_DATE_MDY_RE, _DATE_DMY_RE):
        match = pattern.search(text)
        if not match:
            continue
        month = _MONTHS.get(match["mon"].lower())
        if not month:
            continue
        day = int(match["day"])
        if match["year"]:
            try:
                return dt.date(int(match["year"]), month, day)
            except ValueError:
                return None
        for year in (today.year, today.year - 1):
            try:
                candidate = dt.date(year, month, day)
            except ValueError:
                continue
            if candidate <= today:
                return candidate
    return None


# ------------------------------------------------------------- misc fields

_QTY_RE = re.compile(r"(\d[\d,]*)\s*sold", re.IGNORECASE)


def parse_quantity_sold(text: str | None) -> int:
    """"12 sold" -> 12. Absent means a single unit."""
    text = clean_text(text)
    match = _QTY_RE.search(text or "")
    if match:
        try:
            return max(1, int(match.group(1).replace(",", "")))
        except ValueError:
            return 1
    return 1


_CONDITIONS = [
    ("New (other)", ["new (other)", "open box", "new other"]),
    ("New", ["brand new", "new with tags", "new without", "new"]),
    ("Remanufactured", ["remanufactured", "rebuilt", "refurbished", "reman"]),
    ("For parts / not working", ["for parts", "not working", "parts only", "as-is", "as is"]),
    ("Pre-Owned", ["pre-owned", "preowned", "used", "very good", "good", "acceptable"]),
]


def normalize_condition(text: str | None) -> str | None:
    text = clean_text(text).lower()
    if not text:
        return None
    for label, needles in _CONDITIONS:
        if any(n in text for n in needles):
            return label
    return clean_text(text).title() or None


_ITEM_ID_RE = re.compile(r"/itm/(?:[^/]+/)?(\d{9,15})")


def extract_item_id(url: str | None) -> str | None:
    """Pull the numeric item id out of an eBay item URL."""
    if not url:
        return None
    match = _ITEM_ID_RE.search(url)
    if match:
        return match.group(1)
    match = re.search(r"[?&](?:item|itemId)=(\d{9,15})", url)
    return match.group(1) if match else None


def clean_url(url: str | None) -> str | None:
    """Drop eBay's tracking query string so the same item hashes consistently."""
    if not url:
        return None
    return url.split("?", 1)[0].split("#", 1)[0]
