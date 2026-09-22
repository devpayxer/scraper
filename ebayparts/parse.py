"""Turn a search-results page into listing records.

eBay has shipped several search layouts and A/B tests them, so nothing here
depends on a single selector. For each field we try, in order:

  1. the classic `s-item__*` selectors
  2. the newer `s-card__*` / `su-styled-text` selectors
  3. a text fallback that scans the card's own lines for the shape of the value
     ("Sold <date>", "$<amount>", "+$<amount> delivery")

That last tier is what keeps the scraper alive across eBay redesigns. If a
layout ever defeats all three, `parse-file --debug` shows exactly which fields
came back empty.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from bs4 import BeautifulSoup, Tag

from .enrich import enrich_title
from .normalize import (
    clean_text,
    clean_url,
    extract_item_id,
    normalize_condition,
    parse_money,
    parse_quantity_sold,
    parse_shipping,
    parse_sold_date,
)

log = logging.getLogger(__name__)

CARD_SELECTORS = [
    "li.s-item",
    "li.s-card",
    "div.s-item",
    "div.s-card",
    "li[data-viewport]",
    "div.brwrvr__item-card",
]

TITLE_SELECTORS = [
    ".s-item__title", ".s-card__title", "[role=heading]",
    ".su-styled-text.primary", "h3.s-item__title", ".brwrvr__item-card__title",
]
PRICE_SELECTORS = [
    ".s-item__price", ".s-card__price", ".su-styled-text.s-card__price",
    ".brwrvr__item-card__price",
]
STRIKE_SELECTORS = [
    ".STRIKETHROUGH", ".s-item__trending-price .STRIKETHROUGH",
    ".su-styled-text--strikethrough", "[class*=strikethrough]", ".s-card__price--original",
]
SHIPPING_SELECTORS = [
    ".s-item__shipping", ".s-item__logisticsCost", ".s-card__logisticsCost",
    ".s-card__attribute-row", ".brwrvr__item-card__shipping",
]
CAPTION_SELECTORS = [
    ".s-item__caption", ".s-item__caption--signal", ".s-item__title--tag",
    ".s-card__caption", ".s-item__ended-date", ".s-card__subtitle",
]
CONDITION_SELECTORS = [
    ".SECONDARY_INFO", ".s-item__subtitle", ".s-card__subtitle",
    ".s-item__condition", ".clipped",
]
SELLER_SELECTORS = [".s-item__seller-info-text", ".s-card__seller-info", ".s-item__seller-info"]
LOCATION_SELECTORS = [".s-item__location", ".s-item__itemLocation", ".s-card__location"]

_SOLD_LINE_RE = re.compile(r"\bsold\b\s+([A-Za-z]{3,9}\.?\s+\d{1,2}(?:,\s*\d{4})?)", re.IGNORECASE)
_SHIP_LINE_RE = re.compile(
    r"(free\s+(?:shipping|delivery|postage)|[+]\s*[^\n]{0,40}?(?:shipping|delivery|postage)"
    r"|shipping\s+not\s+specified)",
    re.IGNORECASE,
)
_PRICE_LINE_RE = re.compile(r"^(?:US\s*)?[$£€]\s*\d")
_FEEDBACK_RE = re.compile(r"([\d.]+)%\s*positive", re.IGNORECASE)
_JUNK_TITLES = {"shop on ebay", "new listing", ""}

# Condition as eBay writes it on its own line, for layouts with no condition class.
_CONDITION_LINES = {
    "pre-owned", "preowned", "used", "new", "brand new", "new (other)", "new other",
    "open box", "refurbished", "remanufactured", "rebuilt", "certified refurbished",
    "seller refurbished", "for parts or not working", "parts only", "very good",
    "good", "acceptable", "new with tags", "new without tags",
}


@dataclass
class Listing:
    item_id: str
    title: str
    url: str | None = None
    seller: str | None = None
    seller_feedback_pct: float | None = None
    sold_date: dt.date | None = None
    price: float | None = None
    currency: str | None = None
    original_price: float | None = None
    shipping_cost: float | None = None
    shipping_status: str = "unknown"
    total_price: float | None = None
    quantity_sold: int = 1
    condition: str | None = None
    location: str | None = None
    image_url: str | None = None

    # derived from the title
    year_from: int | None = None
    year_to: int | None = None
    make: str | None = None
    model: str | None = None
    part_category: str | None = None
    part_group: str | None = None
    is_oem: bool = False
    side: str | None = None
    position: str | None = None

    source: str = "sold_page"
    confidence: str = "observed"
    source_url: str | None = None
    scraped_at: dt.datetime = field(default_factory=dt.datetime.utcnow)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["sold_date"] = self.sold_date.isoformat() if self.sold_date else None
        data["scraped_at"] = self.scraped_at.isoformat(timespec="seconds")
        return data


def _first_text(card: Tag, selectors: Iterable[str]) -> str:
    for selector in selectors:
        node = card.select_one(selector)
        if node:
            text = clean_text(node.get_text(" ", strip=True))
            if text:
                return text
    return ""


def _card_lines(card: Tag) -> list[str]:
    raw = card.get_text("\n", strip=True)
    return [clean_text(line) for line in raw.split("\n") if clean_text(line)]


def _extract_title(card: Tag) -> str:
    title = _first_text(card, TITLE_SELECTORS)
    if not title:
        link = card.select_one("a[href*='/itm/']")
        if link:
            title = clean_text(link.get("aria-label") or link.get_text(" ", strip=True))
    # eBay prefixes "New Listing" onto the visible title
    title = re.sub(r"^new\s+listing\s*", "", title, flags=re.IGNORECASE).strip()
    # some layouts duplicate the title inside an accessibility span
    half = len(title) // 2
    if half > 12 and title[:half].strip() == title[half:].strip():
        title = title[:half].strip()
    return title


def _extract_price(card: Tag) -> tuple[float | None, str | None]:
    text = _first_text(card, PRICE_SELECTORS)
    if text:
        price, currency = parse_money(text)
        if price is not None:
            return price, currency
    for line in _card_lines(card):
        if _PRICE_LINE_RE.match(line):
            price, currency = parse_money(line)
            if price is not None:
                return price, currency
    return None, None


def _extract_original_price(card: Tag, sold_price: float | None) -> float | None:
    for selector in STRIKE_SELECTORS:
        for node in card.select(selector):
            price, _ = parse_money(node.get_text(" ", strip=True))
            if price is not None and (sold_price is None or price > sold_price):
                return price
    return None


def _extract_shipping(card: Tag) -> tuple[float | None, str]:
    for selector in SHIPPING_SELECTORS:
        for node in card.select(selector):
            text = clean_text(node.get_text(" ", strip=True))
            if text and _SHIP_LINE_RE.search(text):
                return parse_shipping(text)
    for line in _card_lines(card):
        if _SHIP_LINE_RE.search(line):
            return parse_shipping(line)
    return None, "unknown"


def _extract_sold_date(card: Tag, today: dt.date | None = None) -> dt.date | None:
    text = _first_text(card, CAPTION_SELECTORS)
    if text and "sold" in text.lower():
        parsed = parse_sold_date(text, today=today)
        if parsed:
            return parsed
    for line in _card_lines(card):
        match = _SOLD_LINE_RE.search(line)
        if match:
            parsed = parse_sold_date(match.group(1), today=today)
            if parsed:
                return parsed
    return None


def _extract_seller(card: Tag) -> tuple[str | None, float | None]:
    text = _first_text(card, SELLER_SELECTORS)
    if not text:
        for line in _card_lines(card):
            if _FEEDBACK_RE.search(line):
                text = line
                break
    if not text:
        return None, None
    feedback = _FEEDBACK_RE.search(text)
    pct = float(feedback.group(1)) if feedback else None
    name = clean_text(text.split("(")[0].split()[0]) if text.split() else None
    return name, pct


def _extract_condition(card: Tag) -> str | None:
    text = _first_text(card, CONDITION_SELECTORS)
    if not text:
        for line in _card_lines(card):
            if line.lower() in _CONDITION_LINES:
                text = line
                break
    return normalize_condition(text)


def _extract_image(card: Tag) -> str | None:
    for node in card.select("img"):
        src = node.get("src") or node.get("data-src") or node.get("data-defer-load")
        if src and src.startswith("http") and "s-l" in src:
            return src
        if src and src.startswith("http") and not src.endswith(".gif"):
            return src
    return None


def parse_card(card: Tag, *, source_url: str | None = None,
               today: dt.date | None = None) -> Listing | None:
    """Parse one result card. Returns None for ads and layout scaffolding."""
    title = _extract_title(card)
    if title.lower() in _JUNK_TITLES:
        return None

    link = card.select_one("a.s-item__link, a.s-card__link, a[href*='/itm/'], a[href]")
    href = link.get("href") if link else None
    item_id = extract_item_id(href)
    if not item_id:
        return None
    if not title:
        return None

    price, currency = _extract_price(card)
    shipping_cost, shipping_status = _extract_shipping(card)
    seller, feedback = _extract_seller(card)
    enriched = enrich_title(title)

    total = None
    if price is not None:
        total = round(price + (shipping_cost or 0.0), 2)

    return Listing(
        item_id=item_id,
        title=title,
        url=clean_url(href),
        seller=seller,
        seller_feedback_pct=feedback,
        sold_date=_extract_sold_date(card, today=today),
        price=price,
        currency=currency,
        original_price=_extract_original_price(card, price),
        shipping_cost=shipping_cost,
        shipping_status=shipping_status,
        total_price=total,
        quantity_sold=parse_quantity_sold(card.get_text(" ", strip=True)),
        condition=_extract_condition(card),
        location=_first_text(card, LOCATION_SELECTORS) or None,
        image_url=_extract_image(card),
        year_from=enriched.year_from,
        year_to=enriched.year_to,
        make=enriched.make,
        model=enriched.model,
        part_category=enriched.part_category,
        part_group=enriched.part_group,
        is_oem=enriched.is_oem,
        side=enriched.side,
        position=enriched.position,
        source_url=source_url,
    )


def find_cards(soup: BeautifulSoup) -> list[Tag]:
    """Locate result cards, trying each known layout then falling back."""
    for selector in CARD_SELECTORS:
        cards = soup.select(selector)
        if len(cards) >= 2:
            return cards
    # last resort: any list item that owns an item link
    fallback = [
        node for node in soup.select("li, div.s-item-wrapper, div[data-testid]")
        if node.select_one("a[href*='/itm/']")
    ]
    # keep only the innermost containers so cards are not nested inside each other
    return [n for n in fallback if not any(n is not o and o in n.parents for o in fallback)]


def parse_search_page(
    html: str, *, source_url: str | None = None, today: dt.date | None = None
) -> list[Listing]:
    """Parse a whole sold-search results page."""
    soup = BeautifulSoup(html, "lxml")
    listings: list[Listing] = []
    seen: set[str] = set()
    for card in find_cards(soup):
        try:
            listing = parse_card(card, source_url=source_url, today=today)
        except Exception:  # one weird card must never kill a page
            log.exception("failed to parse a card")
            continue
        if listing and listing.item_id not in seen:
            seen.add(listing.item_id)
            listings.append(listing)
    return listings


def result_count(html: str) -> int | None:
    """The "1,234 results" figure, when eBay prints it."""
    text = clean_text(BeautifulSoup(html, "lxml").get_text(" ", strip=True))
    match = re.search(r"([\d,]+)\+?\s*results?\b", text, re.IGNORECASE)
    if match:
        try:
            return int(match.group(1).replace(",", ""))
        except ValueError:
            return None
    return None
