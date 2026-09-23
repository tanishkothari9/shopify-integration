"""Inventory logic that needs no database (spec §10)."""

import json

import pytest

from shopify_integration.exceptions import ShopifyUserError
from shopify_integration.outbound.inventory import _is_compare_mismatch, _row_target


def test_dedupe_key_is_parsed_back_into_item_and_location():
	row = {"dedupe_key": "inventory:My Store:TEE-01:gid://shopify/Location/123"}
	assert _row_target(row) == ("TEE-01", "gid://shopify/Location/123")


def test_location_gid_containing_colons_survives_the_split():
	"""A GID is full of colons and slashes; a naive split loses most of it."""
	row = {"dedupe_key": "inventory:Store:SKU-1:gid://shopify/Location/987654321"}
	item, location = _row_target(row)
	assert item == "SKU-1"
	assert location == "gid://shopify/Location/987654321"


def test_store_names_with_spaces_are_handled():
	row = {"dedupe_key": "inventory:Acme Retail EU:ITEM-9:gid://shopify/Location/1"}
	assert _row_target(row) == ("ITEM-9", "gid://shopify/Location/1")


def test_payload_is_the_fallback_when_there_is_no_usable_key():
	row = {
		"dedupe_key": "something-else",
		"payload": json.dumps({"item_code": "TEE-02", "location_gid": "gid://shopify/Location/9"}),
	}
	assert _row_target(row) == ("TEE-02", "gid://shopify/Location/9")


def test_unparseable_row_yields_nothing_rather_than_raising():
	assert _row_target({"dedupe_key": "", "payload": "{not json"}) == (None, None)
	assert _row_target({}) == (None, None)


@pytest.mark.parametrize(
	"code", ["COMPARE_QUANTITY_STALE", "STALE_COMPARE_QUANTITY", "INVALID_COMPARE_QUANTITY"]
)
def test_compare_mismatch_codes_are_recognised(code):
	exc = ShopifyUserError("rejected", user_errors=[{"code": code, "message": "stale"}])
	assert _is_compare_mismatch(exc) is True


def test_compare_mismatch_detected_from_the_message_too():
	"""Shopify's code names for this have moved between API versions, so the message is a
	secondary signal. A false positive costs one extra call; a false negative loses a write."""
	exc = ShopifyUserError("inventorySetQuantities rejected: compareQuantity did not match", user_errors=[])
	assert _is_compare_mismatch(exc) is True


def test_an_ordinary_user_error_is_not_a_compare_mismatch():
	"""A genuine validation failure must stay permanent, not be retried forever."""
	exc = ShopifyUserError(
		"rejected", user_errors=[{"code": "INVALID_LOCATION", "message": "Location not found"}]
	)
	assert _is_compare_mismatch(exc) is False


def test_reference_uri_is_a_valid_uri_for_a_store_name_with_spaces():
	"""Shopify validates referenceDocumentUri and rejects the entire mutation if it is not a
	valid URI. Store names routinely contain spaces, and an unencoded one failed every
	inventory push with an error that never mentioned the store name."""
	from unittest.mock import patch
	from urllib.parse import urlparse

	from shopify_integration.outbound.inventory import reference_uri

	with patch("frappe.local") as local:
		local.site = "shopify.localhost"
		uri = reference_uri("Acme Apparel EU")

	assert " " not in uri
	assert "%20" in uri
	parsed = urlparse(uri)
	assert parsed.scheme == "erpnext"
	assert parsed.netloc == "shopify.localhost"


def test_reference_uri_encodes_other_awkward_characters():
	from unittest.mock import patch

	from shopify_integration.outbound.inventory import reference_uri

	with patch("frappe.local") as local:
		local.site = "shopify.localhost"
		uri = reference_uri("Shop / EU & UK")

	assert " " not in uri
	# The point is that a slash inside the store name becomes %2F rather than an extra path
	# segment, so the URI still names one store.
	assert uri.endswith("Shop%20%2F%20EU%20%26%20UK")
