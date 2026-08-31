"""eBay search URL construction."""
from __future__ import annotations

from urllib.parse import urlencode

from .config import Seller, Settings


def sold_search_url(
    settings: Settings,
    seller: Seller | None = None,
    page: int = 1,
    keywords: str = "",
    category: int | None = None,
) -> str:
    """Build a sold+completed search URL.

    Mirrors the shape eBay itself produces, e.g.
    https://www.ebay.com/sch/i.html?_ssn=spartan_auto&store_name=spartautoparts
        &_oac=1&LH_Complete=1&LH_Sold=1&_pgn=1
    """
    if category is None:
        category = (seller.category if seller else None) or settings.default_category

    params: dict[str, str | int] = {
        "_nkw": keywords,
        "LH_Complete": 1,   # completed listings
        "LH_Sold": 1,       # ...that actually sold
        "_pgn": page,
        "_ipg": settings.items_per_page,
    }
    if seller is not None:
        params["_ssn"] = seller.user
        if seller.store:
            params["store_name"] = seller.store
        params["_oac"] = 1  # "only show items from this store"
    if category:
        params["_sacat"] = category
    if not keywords:
        params.pop("_nkw")

    return f"https://{settings.marketplace}/sch/i.html?{urlencode(params)}"


def category_browse_url(settings: Settings, category: int, page: int = 1) -> str:
    """Sold listings across a whole category -- used by `discover` to find sellers."""
    return sold_search_url(settings, seller=None, page=page, category=category)
