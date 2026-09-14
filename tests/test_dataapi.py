"""The data-API path: the one route that updates automatically without blocking.

Everything here maps a vendor's JSON into the same Listing objects the scraper
produced, so it must be tested at the mapping boundary -- that is where a
provider swap breaks.
"""
from __future__ import annotations

import json

import pytest

from ebayparts.dataapi import (
    DataAPIConfig, DataAPINotConfigured, dig, parse_response_file,
)

RESPONSE = {
    "status": "OK",
    "data": {"products": [
        {"product_id": "111", "title": "2015-2018 FORD F-150 LEFT HEADLIGHT OEM",
         "sold_price": {"value": 212.0, "currency": "USD"}, "shipping_cost": 0,
         "sold_date": "2026-09-10T14:22:01Z",
         "product_url": "https://www.ebay.com/itm/111", "condition": "Used",
         "seller": {"name": "spartan_auto"}},
        {"product_id": "222", "title": "2018 Honda Accord Tail Light OEM",
         "sold_price": {"value": 88.0, "currency": "USD"}, "shipping_cost": 12.5,
         "sold_date": "2026-09-08",
         "product_url": "https://www.ebay.com/itm/222", "condition": "Used",
         "seller": {"name": "atx"}},
    ]},
}

FIELD_MAP = {
    "title": "title", "price": "sold_price.value", "currency": "sold_price.currency",
    "shipping": "shipping_cost", "sold_date": "sold_date", "url": "product_url",
    "item_id": "product_id", "condition": "condition", "seller": "seller.name",
}


def make_config(**overrides) -> DataAPIConfig:
    base = dict(provider="Test", base_url="https://api.example.com", path="/search",
                results_path="data.products", field_map=FIELD_MAP)
    base.update(overrides)
    return DataAPIConfig.from_dict(base)


class TestDig:
    def test_nested(self):
        assert dig(RESPONSE, "data.products.0.title").startswith("2015-2018 FORD")

    def test_list_index(self):
        assert dig({"a": [{"b": 5}]}, "a.0.b") == 5

    def test_missing_returns_none(self):
        assert dig(RESPONSE, "data.nope.0") is None
        assert dig(RESPONSE, "") is None
        assert dig(None, "a.b") is None


class TestConfig:
    def test_requires_base_url(self):
        with pytest.raises(DataAPINotConfigured):
            DataAPIConfig.from_dict({"provider": "x"})

    def test_empty_block_is_not_configured(self):
        with pytest.raises(DataAPINotConfigured):
            DataAPIConfig.from_dict(None)

    def test_missing_key_env_raises_with_help(self, monkeypatch):
        monkeypatch.delenv("EBAY_DATA_API_KEY", raising=False)
        config = make_config(api_key_env="EBAY_DATA_API_KEY")
        with pytest.raises(DataAPINotConfigured) as exc:
            _ = config.api_key
        assert "EBAY_DATA_API_KEY" in str(exc.value)

    def test_key_read_from_env(self, monkeypatch):
        monkeypatch.setenv("EBAY_DATA_API_KEY", "secret")
        assert make_config().api_key == "secret"


class TestMapping:
    @pytest.fixture
    def listings(self, tmp_path):
        path = tmp_path / "resp.json"
        path.write_text(json.dumps(RESPONSE), encoding="utf-8")
        return parse_response_file(str(path), make_config())

    def test_all_rows_mapped(self, listings):
        assert len(listings) == 2

    def test_price_and_currency(self, listings):
        assert listings[0].price == 212.0
        assert listings[0].currency == "USD"

    def test_free_vs_paid_shipping(self, listings):
        assert (listings[0].shipping_cost, listings[0].shipping_status) == (0.0, "free")
        assert (listings[1].shipping_cost, listings[1].shipping_status) == (12.5, "paid")

    def test_iso_dates_parsed(self, listings):
        assert listings[0].sold_date.isoformat() == "2026-09-10"
        assert listings[1].sold_date.isoformat() == "2026-09-08"

    def test_enrichment_runs_on_api_titles(self, listings):
        assert listings[0].make == "Ford" and listings[0].part_category == "Headlight"
        assert listings[1].part_category == "Tail Light"

    def test_total_includes_shipping(self, listings):
        assert listings[1].total_price == 100.5

    def test_bad_results_path_is_explained(self, tmp_path):
        path = tmp_path / "r.json"
        path.write_text(json.dumps(RESPONSE), encoding="utf-8")
        from ebayparts.dataapi import DataAPIError
        with pytest.raises(DataAPIError) as exc:
            parse_response_file(str(path), make_config(results_path="data.wrong"))
        assert "did not point to a list" in str(exc.value)


def test_url_building_puts_key_in_header_not_url(monkeypatch):
    from ebayparts.dataapi import DataAPIClient
    monkeypatch.setenv("EBAY_DATA_API_KEY", "secret")
    config = make_config(query={"q": "{query}"}, auth_header="x-api-key")
    client = DataAPIClient(config)
    url = client._build_url({"query": "headlight"}, page=1)
    assert "secret" not in url
    assert "q=headlight" in url and "page=1" in url
