"""Pure mapping logic -- the parts that need no database (spec §8.7)."""

import pytest

from shopify_integration.catalogue.mapping import (
	MAX_OPTIONS,
	gid_suffix,
	real_options,
	template_item_code,
)


def test_default_title_option_is_not_a_real_option():
	"""Every Shopify product has a variant; one with no real options still reports a
	placeholder 'Title'/'Default Title'. Mapping that would put a meaningless attribute on
	every simple item in the catalogue."""
	product = {"options": [{"name": "Title", "position": 1, "values": ["Default Title"]}]}
	assert real_options(product) == []


def test_genuine_options_are_kept():
	product = {
		"options": [
			{"name": "Size", "values": ["S", "M"]},
			{"name": "Colour", "values": ["Red"]},
		]
	}
	assert [o["name"] for o in real_options(product)] == ["Size", "Colour"]


def test_an_option_literally_named_title_with_real_values_is_kept():
	"""Only the exact placeholder is ignored, not every option called Title."""
	product = {"options": [{"name": "Title", "values": ["Deluxe", "Standard"]}]}
	assert [o["name"] for o in real_options(product)] == ["Title"]


def test_blank_option_names_are_dropped():
	product = {"options": [{"name": "  ", "values": ["x"]}, {"name": "Size", "values": ["S"]}]}
	assert [o["name"] for o in real_options(product)] == ["Size"]


def test_missing_options_key_is_empty():
	assert real_options({}) == []


def test_shopify_caps_options_at_three():
	"""Documents the limit the mapping writer enforces: ERPNext items with more attributes
	cannot round-trip to Shopify."""
	assert MAX_OPTIONS == 3


@pytest.mark.parametrize(
	("gid", "expected"),
	[
		("gid://shopify/ProductVariant/12345", "12345"),
		("gid://shopify/Product/1", "1"),
		("gid://shopify/Product/1/", "1"),
		(None, "UNKNOWN"),
		("", "UNKNOWN"),
	],
)
def test_gid_suffix(gid, expected):
	assert gid_suffix(gid) == expected


def test_template_code_prefers_the_handle():
	assert template_item_code({"handle": "cotton-tee", "id": "gid://shopify/Product/7"}) == "cotton-tee"


def test_template_code_falls_back_to_the_id():
	"""Never derive a code from the title -- merchants rename products constantly."""
	assert template_item_code({"id": "gid://shopify/Product/7"}) == "SHOPIFY-TPL-7"


def test_item_codes_are_capped_to_the_frappe_column_width():
	long_handle = "x" * 300
	assert len(template_item_code({"handle": long_handle})) == 140
