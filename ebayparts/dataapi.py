"""Pull sold listings from a JSON data API instead of scraping eBay.

This is the route that actually updates on a schedule and does not get blocked,
because a data vendor absorbs the scraping and hands you clean JSON. You call a
normal REST endpoint with an API key; nothing here touches eBay's website, so
there is no captcha, no 403, no browser.

It is deliberately PROVIDER-AGNOSTIC. Every vendor (SoldComps, OpenWeb Ninja on
RapidAPI, an Apify actor's dataset, or eBay's own Marketplace Insights if you
are granted it) returns the same shape of data under different field names. You
describe your chosen provider once in the `data_api` block of settings.yml -- a
base URL, how the key is sent, and a field map -- and this module maps whatever
comes back into the same Listing objects the rest of the tool already uses.

The API key is NEVER read from the config file. It comes from an environment
variable named in `api_key_env`, so a key is never committed to git.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .enrich import enrich_title
from .normalize import (
    clean_url,
    extract_item_id,
    normalize_condition,
    parse_money,
    parse_shipping,
    parse_sold_date,
)
from .parse import Listing

log = logging.getLogger(__name__)


class DataAPIError(RuntimeError):
    pass


class DataAPINotConfigured(DataAPIError):
    pass


# --------------------------------------------------------------- json helpers

def dig(data: Any, path: str | None) -> Any:
    """Resolve a dotted path like 'price.value' or 'a.b.0.c' into nested JSON."""
    if not path:
        return None
    current = data
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    price, _ = parse_money(str(value))
    return price


# --------------------------------------------------------------- config

@dataclass
class DataAPIConfig:
    provider: str
    base_url: str
    path: str = ""
    method: str = "GET"
    api_key_env: str = "EBAY_DATA_API_KEY"
    auth_header: str = "x-rapidapi-key"
    auth_query_param: str = ""          # some providers want the key in the query
    headers: dict[str, str] | None = None
    query: dict[str, str] | None = None
    results_path: str = "results"       # dotted path to the list of items
    page_param: str = "page"            # query param used for paging
    start_page: int = 1
    max_pages: int = 5
    page_size_param: str = ""
    page_size: int = 0
    field_map: dict[str, str] | None = None
    sold_only_note: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "DataAPIConfig":
        if not raw or not raw.get("base_url"):
            raise DataAPINotConfigured(
                "No data_api block in settings.yml. See the commented example "
                "there, pick a provider, and set the base_url + field_map."
            )
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in raw.items() if k in known})

    @property
    def api_key(self) -> str:
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise DataAPINotConfigured(
                f"No API key. Set the {self.api_key_env} environment variable to "
                f"your {self.provider} key.\n"
                f"  Windows (PowerShell):  setx {self.api_key_env} \"your-key-here\"\n"
                f"  then open a NEW terminal so it takes effect."
            )
        return key


# --------------------------------------------------------------- client

class DataAPIClient:
    def __init__(self, config: DataAPIConfig, *, timeout: int = 45):
        self.config = config
        self.timeout = timeout

    def _build_url(self, query_values: dict[str, str], page: int) -> str:
        cfg = self.config
        params: dict[str, str] = {}
        for key, template in (cfg.query or {}).items():
            params[key] = template.format(page=page, **query_values)
        if cfg.page_param:
            params[cfg.page_param] = str(page)
        if cfg.page_size_param and cfg.page_size:
            params[cfg.page_size_param] = str(cfg.page_size)
        if cfg.auth_query_param:
            params[cfg.auth_query_param] = cfg.api_key
        base = cfg.base_url.rstrip("/") + "/" + cfg.path.lstrip("/") if cfg.path else cfg.base_url
        return f"{base}?{urllib.parse.urlencode(params)}"

    def _request(self, url: str) -> Any:
        cfg = self.config
        headers = dict(cfg.headers or {})
        if cfg.auth_header and not cfg.auth_query_param:
            headers[cfg.auth_header] = cfg.api_key
        headers.setdefault("Accept", "application/json")
        request = urllib.request.Request(url, headers=headers, method=cfg.method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300] if exc.fp else ""
            raise DataAPIError(f"HTTP {exc.code} from {cfg.provider}: {body}") from exc
        except urllib.error.URLError as exc:
            raise DataAPIError(f"could not reach {cfg.provider}: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise DataAPIError(f"{cfg.provider} did not return JSON: {exc}") from exc

    def _map_item(self, raw: dict[str, Any], source_url: str) -> Listing | None:
        fm = self.config.field_map or {}
        title = dig(raw, fm.get("title"))
        if not title:
            return None
        title = str(title).strip()

        price = _as_float(dig(raw, fm.get("price")))
        currency = dig(raw, fm.get("currency"))
        shipping_raw = dig(raw, fm.get("shipping"))
        if isinstance(shipping_raw, (int, float)):
            shipping_cost, shipping_status = float(shipping_raw), (
                "free" if shipping_raw == 0 else "paid")
        else:
            shipping_cost, shipping_status = parse_shipping(str(shipping_raw or ""))

        sold_raw = dig(raw, fm.get("sold_date"))
        sold_date = None
        if sold_raw:
            sold_date = parse_sold_date(str(sold_raw))
            if sold_date is None:
                for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ"):
                    try:
                        sold_date = dt.datetime.strptime(str(sold_raw)[:19], fmt).date()
                        break
                    except ValueError:
                        continue

        url = clean_url(dig(raw, fm.get("url")))
        item_id = dig(raw, fm.get("item_id")) or extract_item_id(url) or ""
        qty = dig(raw, fm.get("quantity_sold")) or 1
        try:
            qty = max(1, int(qty))
        except (ValueError, TypeError):
            qty = 1

        total = round(price + (shipping_cost or 0.0), 2) if price is not None else None
        enriched = enrich_title(title)

        return Listing(
            item_id=str(item_id) or (url or title)[:40],
            title=title,
            url=url,
            seller=dig(raw, fm.get("seller")),
            sold_date=sold_date,
            price=price,
            currency=(str(currency) if currency else None),
            shipping_cost=shipping_cost,
            shipping_status=shipping_status,
            total_price=total,
            quantity_sold=qty,
            condition=normalize_condition(dig(raw, fm.get("condition"))),
            location=dig(raw, fm.get("location")),
            image_url=dig(raw, fm.get("image_url")),
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

    def fetch(self, query: str, *, max_pages: int | None = None,
              since: dt.date | None = None) -> list[Listing]:
        """Pull sold listings for one query (a keyword or category term)."""
        cfg = self.config
        limit = max_pages or cfg.max_pages
        listings: list[Listing] = []
        seen: set[str] = set()

        for offset in range(limit):
            page = cfg.start_page + offset
            url = self._build_url({"query": query}, page)
            log.info("api: %s page %s", query, page)
            payload = self._request(url)
            items = dig(payload, cfg.results_path)
            if not isinstance(items, list) or not items:
                break

            new_this_page = 0
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                listing = self._map_item(raw, source_url=url)
                if not listing or listing.item_id in seen:
                    continue
                if since and listing.sold_date and listing.sold_date < since:
                    continue
                seen.add(listing.item_id)
                listings.append(listing)
                new_this_page += 1

            if new_this_page == 0:
                break
            time.sleep(0.5)  # gentle to the vendor's rate limit

        return listings


def parse_response_file(path: str, config: DataAPIConfig) -> list[Listing]:
    """Map a saved JSON response (for testing a field_map without a key)."""
    payload = json.loads(open(path, encoding="utf-8").read())
    client = DataAPIClient(config)
    items = dig(payload, config.results_path)
    if not isinstance(items, list):
        raise DataAPIError(
            f"results_path '{config.results_path}' did not point to a list. "
            f"Top-level keys are: {list(payload) if isinstance(payload, dict) else type(payload)}"
        )
    out = []
    for raw in items:
        if isinstance(raw, dict):
            listing = client._map_item(raw, source_url=path)
            if listing:
                out.append(listing)
    return out
