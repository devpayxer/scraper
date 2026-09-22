"""eBay's OFFICIAL Browse API — sanctioned, free, and impossible to block.

What your developer account actually grants
-------------------------------------------
The developer account gives you the **Browse API**: active listings, via OAuth,
5,000 calls/day, fully sanctioned. No captcha, no 403, no ToS grey area.

It does NOT give sold data. The old Finding API call `findCompletedItems` was
decommissioned in February 2025, and sold history now lives only in the
Marketplace Insights API, which is a restricted release.

So how do we get sales out of an active-listings API?
-----------------------------------------------------
By watching. A used car part is almost always a quantity-1 listing: when it
sells, the listing disappears. So we snapshot what is listed each day and diff
the snapshots. A listing that was there yesterday and is gone today ended — and
for single-quantity used parts, "ended" is overwhelmingly "sold".

We can do better than guessing, too. Browse returns `itemEndDate`. A listing
that vanishes **well before** its end date almost certainly sold; one that
vanishes **at** its end date probably just expired. That gives each inferred
sale a confidence level instead of a coin flip.

The trade-off, stated plainly: this builds history forward from today. Day one
gives you supply and asking prices; the sales signal accumulates from there.
That is the same deal as scraping a 90-day window, except it never breaks.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterator

log = logging.getLogger(__name__)

PRODUCTION = "https://api.ebay.com"
SANDBOX = "https://api.sandbox.ebay.com"
OAUTH_PATH = "/identity/v1/oauth2/token"
SEARCH_PATH = "/buy/browse/v1/item_summary/search"
PUBLIC_SCOPE = "https://api.ebay.com/oauth/api_scope"

# Browse allows 5,000 calls/day. Leave headroom so a bug cannot burn the quota.
DEFAULT_DAILY_CALL_BUDGET = 4000
MAX_LIMIT = 200  # eBay's per-page maximum for item_summary/search


class EbayAPIError(RuntimeError):
    pass


class EbayCredentialsMissing(EbayAPIError):
    pass


@dataclass
class EbayCredentials:
    app_id: str
    cert_id: str
    marketplace_id: str = "EBAY_US"
    sandbox: bool = False

    @classmethod
    def from_env(cls, *, app_id_env: str = "EBAY_APP_ID",
                 cert_id_env: str = "EBAY_CERT_ID",
                 marketplace_id: str = "EBAY_US",
                 sandbox: bool = False) -> "EbayCredentials":
        app_id = os.environ.get(app_id_env, "").strip()
        cert_id = os.environ.get(cert_id_env, "").strip()
        if not app_id or not cert_id:
            raise EbayCredentialsMissing(
                "eBay API credentials not found.\n"
                f"  Set {app_id_env} to your App ID (Client ID) and\n"
                f"      {cert_id_env} to your Cert ID (Client Secret).\n"
                "  Get them at developer.ebay.com -> Application Keys (Production).\n\n"
                "  Windows (PowerShell), then open a NEW terminal:\n"
                f'    setx {app_id_env} "YourApp-xxxxx-PRD-xxxxxxxxx-xxxxxxxx"\n'
                f'    setx {cert_id_env} "PRD-xxxxxxxxxxxx-xxxx-xxxx-xxxx-xxxx"'
            )
        return cls(app_id=app_id, cert_id=cert_id, marketplace_id=marketplace_id,
                   sandbox=sandbox)

    @property
    def base_url(self) -> str:
        # EBAY_API_BASE lets the test suite point at a local stand-in; it is not
        # something you need to set in normal use.
        override = os.environ.get("EBAY_API_BASE", "").strip()
        if override:
            return override.rstrip("/")
        return SANDBOX if self.sandbox else PRODUCTION

    @property
    def basic_auth(self) -> str:
        raw = f"{self.app_id}:{self.cert_id}".encode("utf-8")
        return base64.b64encode(raw).decode("ascii")


@dataclass
class ItemSummary:
    """One active listing as Browse returns it."""
    item_id: str
    title: str
    price: float | None = None
    currency: str | None = None
    condition: str | None = None
    seller: str | None = None
    seller_feedback_pct: float | None = None
    item_end_date: dt.datetime | None = None
    item_web_url: str | None = None
    image_url: str | None = None
    shipping_cost: float | None = None
    location: str | None = None
    categories: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _parse_iso(value: Any) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def map_item_summary(raw: dict[str, Any]) -> ItemSummary | None:
    item_id = raw.get("itemId") or raw.get("legacyItemId")
    title = (raw.get("title") or "").strip()
    if not item_id or not title:
        return None

    price = raw.get("price") or {}
    seller = raw.get("seller") or {}
    image = raw.get("image") or {}
    shipping = (raw.get("shippingOptions") or [{}])[0].get("shippingCost") or {}
    location = (raw.get("itemLocation") or {}).get("country")
    categories = ", ".join(
        c.get("categoryName", "") for c in (raw.get("categories") or []) if c.get("categoryName")
    ) or None

    return ItemSummary(
        item_id=str(item_id),
        title=title,
        price=_f(price.get("value")),
        currency=price.get("currency"),
        condition=raw.get("condition"),
        seller=seller.get("username"),
        seller_feedback_pct=_f(seller.get("feedbackPercentage")),
        item_end_date=_parse_iso(raw.get("itemEndDate")),
        item_web_url=raw.get("itemWebUrl"),
        image_url=image.get("imageUrl"),
        shipping_cost=_f(shipping.get("value")),
        location=location,
        categories=categories,
        raw=raw,
    )


class EbayBrowseClient:
    """Minimal, dependency-free client for the Browse API."""

    def __init__(self, credentials: EbayCredentials, *, timeout: int = 40,
                 call_budget: int = DEFAULT_DAILY_CALL_BUDGET):
        self.credentials = credentials
        self.timeout = timeout
        self.call_budget = call_budget
        self.calls_made = 0
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # ------------------------------------------------------------- oauth
    def _mint_token(self) -> str:
        creds = self.credentials
        url = creds.base_url + OAUTH_PATH
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "scope": PUBLIC_SCOPE,
        }).encode("ascii")
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "Authorization": f"Basic {creds.basic_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300] if exc.fp else ""
            if exc.code in (400, 401):
                raise EbayAPIError(
                    f"eBay rejected the credentials (HTTP {exc.code}). Check that you "
                    f"used the PRODUCTION App ID and Cert ID, not the Sandbox pair.\n"
                    f"  {detail}"
                ) from exc
            raise EbayAPIError(f"token request failed: HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise EbayAPIError(f"could not reach eBay: {exc.reason}") from exc

        token = payload.get("access_token")
        if not token:
            raise EbayAPIError(f"no access_token in eBay's reply: {payload}")
        # Refresh a minute early so a long run never uses an expiring token.
        self._token_expires_at = time.time() + int(payload.get("expires_in", 7200)) - 60
        log.debug("minted an application token, valid ~%ss", payload.get("expires_in"))
        return token

    def token(self) -> str:
        if self._token is None or time.time() >= self._token_expires_at:
            self._token = self._mint_token()
        return self._token

    # ------------------------------------------------------------- search
    def _get(self, url: str) -> dict[str, Any]:
        if self.calls_made >= self.call_budget:
            raise EbayAPIError(
                f"daily call budget reached ({self.calls_made}/{self.call_budget}). "
                f"Browse allows 5,000/day; this stops well short on purpose."
            )
        request = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token()}",
            "X-EBAY-C-MARKETPLACE-ID": self.credentials.marketplace_id,
            "Accept": "application/json",
        })
        self.calls_made += 1
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400] if exc.fp else ""
            if exc.code == 429:
                raise EbayAPIError(f"rate limited by eBay: {detail}") from exc
            raise EbayAPIError(f"Browse API HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise EbayAPIError(f"could not reach eBay: {exc.reason}") from exc

    def search(self, *, query: str = "", category_ids: str | int | None = None,
               limit: int = MAX_LIMIT, offset: int = 0,
               filters: str | None = None, sort: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": min(int(limit), MAX_LIMIT), "offset": offset}
        if query:
            params["q"] = query
        if category_ids:
            params["category_ids"] = str(category_ids)
        if filters:
            params["filter"] = filters
        if sort:
            params["sort"] = sort
        url = (self.credentials.base_url + SEARCH_PATH + "?"
               + urllib.parse.urlencode(params))
        return self._get(url)

    def iter_items(self, *, query: str = "", category_ids: str | int | None = None,
                   max_items: int = 1000, filters: str | None = None,
                   page_pause: float = 0.3) -> Iterator[ItemSummary]:
        """Page through results, yielding mapped summaries."""
        offset = 0
        seen = 0
        while seen < max_items:
            page_size = min(MAX_LIMIT, max_items - seen)
            payload = self.search(query=query, category_ids=category_ids,
                                  limit=page_size, offset=offset, filters=filters)
            summaries = payload.get("itemSummaries") or []
            if not summaries:
                return
            for raw in summaries:
                item = map_item_summary(raw)
                if item is not None:
                    seen += 1
                    yield item
            total = payload.get("total")
            offset += len(summaries)
            if total is not None and offset >= int(total):
                return
            if len(summaries) < page_size:
                return
            time.sleep(page_pause)
