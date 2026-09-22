"""eBay's official Browse API client: credentials, mapping, budget."""
from __future__ import annotations

import datetime as dt

import pytest

from ebayparts.ebayapi import (
    EbayAPIError, EbayBrowseClient, EbayCredentials, EbayCredentialsMissing,
    map_item_summary,
)

RAW = {
    "itemId": "v1|126742318899|0",
    "title": "2015-2018 FORD F-150 LEFT HEADLIGHT HALOGEN OEM",
    "price": {"value": "212.00", "currency": "USD"},
    "condition": "Used",
    "seller": {"username": "spartan_auto", "feedbackPercentage": "99.8"},
    "itemEndDate": "2026-10-22T00:00:00.000Z",
    "itemWebUrl": "https://www.ebay.com/itm/126742318899",
    "image": {"imageUrl": "https://i.ebayimg.com/x.jpg"},
    "shippingOptions": [{"shippingCost": {"value": "0.00", "currency": "USD"}}],
    "itemLocation": {"country": "US"},
    "categories": [{"categoryName": "Headlight Assemblies"}],
}


class TestCredentials:
    def test_missing_env_explains_how_to_set_it(self, monkeypatch):
        monkeypatch.delenv("EBAY_APP_ID", raising=False)
        monkeypatch.delenv("EBAY_CERT_ID", raising=False)
        with pytest.raises(EbayCredentialsMissing) as exc:
            EbayCredentials.from_env()
        message = str(exc.value)
        assert "EBAY_APP_ID" in message and "EBAY_CERT_ID" in message
        assert "setx" in message

    def test_basic_auth_is_base64_of_the_pair(self, monkeypatch):
        monkeypatch.setenv("EBAY_APP_ID", "app")
        monkeypatch.setenv("EBAY_CERT_ID", "cert")
        import base64
        creds = EbayCredentials.from_env()
        assert base64.b64decode(creds.basic_auth).decode() == "app:cert"

    def test_sandbox_and_production_differ(self, monkeypatch):
        monkeypatch.delenv("EBAY_API_BASE", raising=False)
        monkeypatch.setenv("EBAY_APP_ID", "a")
        monkeypatch.setenv("EBAY_CERT_ID", "b")
        assert "sandbox" in EbayCredentials.from_env(sandbox=True).base_url
        assert "sandbox" not in EbayCredentials.from_env(sandbox=False).base_url


class TestMapping:
    def test_maps_every_field(self):
        item = map_item_summary(RAW)
        assert item.item_id == "v1|126742318899|0"
        assert item.price == 212.0 and item.currency == "USD"
        assert item.seller == "spartan_auto"
        assert item.seller_feedback_pct == 99.8
        assert item.shipping_cost == 0.0
        assert item.item_end_date.date() == dt.date(2026, 10, 22)
        assert item.location == "US"
        assert item.categories == "Headlight Assemblies"

    def test_rows_without_a_title_are_dropped(self):
        assert map_item_summary({"itemId": "1"}) is None

    def test_rows_without_an_id_are_dropped(self):
        assert map_item_summary({"title": "x"}) is None

    def test_missing_optional_blocks_do_not_crash(self):
        item = map_item_summary({"itemId": "1", "title": "A part"})
        assert item.price is None and item.shipping_cost is None
        assert item.item_end_date is None


class TestCallBudget:
    def test_budget_stops_before_ebays_limit(self, monkeypatch):
        monkeypatch.setenv("EBAY_APP_ID", "a")
        monkeypatch.setenv("EBAY_CERT_ID", "b")
        client = EbayBrowseClient(EbayCredentials.from_env(), call_budget=2)
        client._token = "tok"
        client._token_expires_at = 9e18
        monkeypatch.setattr(client, "_get", lambda url: {"itemSummaries": []})
        # _get is stubbed, so drive the budget check directly
        real_get = EbayBrowseClient._get.__get__(client)
        client.calls_made = 2
        with pytest.raises(EbayAPIError) as exc:
            real_get("https://api.ebay.com/x")
        assert "budget" in str(exc.value)
