"""OAuth install flow.

The callback is a public endpoint that writes an API token onto a store, which makes it the
most security-sensitive code in the app after the webhook receiver. These tests are mostly
about what it *refuses* to do.
"""

from __future__ import annotations

import hashlib
import hmac
from unittest.mock import patch

import pytest

from shopify_integration.api import oauth

SECRET = "shpss_EXAMPLE_NOT_A_REAL_SECRET"


def sign(params: dict, secret: str = SECRET) -> str:
	"""Shopify's callback signature: hex over the sorted query string."""
	rest = {k: v for k, v in params.items() if k not in ("hmac", "signature")}
	message = "&".join(f"{k}={rest[k]}" for k in sorted(rest))
	return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------------------
# Shop domain validation -- this value reaches a URL we POST a client secret to
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
	"domain",
	["my-shop.myshopify.com", "example-store.myshopify.com", "a1.myshopify.com"],
)
def test_valid_shop_domains_are_accepted(domain):
	assert oauth.normalise_shop_domain(domain) == domain


@pytest.mark.parametrize(
	"domain",
	[
		"evil.com",
		"my-shop.myshopify.com.evil.com",
		"my-shop.myshopify.com/../x",
		"localhost",
		"127.0.0.1",
		"my shop.myshopify.com",
		"",
		"-leading-dash.myshopify.com",
		"http://my-shop.myshopify.com",
	],
)
def test_hostile_shop_domains_are_rejected(domain):
	"""The domain arrives in a query parameter on a public endpoint and is used to build the
	URL we send the client secret to. Anything looser than an exact match is a forgery hole.

	Asserting None rather than "an exception was raised": frappe.throw needs a site context,
	so a pytest.raises here passes just as happily when the raise came from the missing
	context instead of from the validation it is supposed to be testing."""
	assert oauth.normalise_shop_domain(domain) is None


def test_domain_is_normalised_to_lowercase():
	assert oauth.normalise_shop_domain("My-Shop.MyShopify.com") == "my-shop.myshopify.com"


# --------------------------------------------------------------------------------------
# Callback signature
# --------------------------------------------------------------------------------------


def test_a_correctly_signed_callback_verifies():
	params = {"code": "abc123", "shop": "my-shop.myshopify.com", "state": "nonce", "timestamp": "1"}
	params["hmac"] = sign(params)
	assert oauth.verify_callback_hmac(params, SECRET) is True


def test_a_tampered_parameter_fails():
	params = {"code": "abc123", "shop": "my-shop.myshopify.com", "state": "nonce", "timestamp": "1"}
	params["hmac"] = sign(params)
	params["code"] = "stolen-code"
	assert oauth.verify_callback_hmac(params, SECRET) is False


def test_the_wrong_secret_fails():
	params = {"code": "abc123", "shop": "my-shop.myshopify.com", "state": "n", "timestamp": "1"}
	params["hmac"] = sign(params, "a-different-secret")
	assert oauth.verify_callback_hmac(params, SECRET) is False


def test_a_missing_signature_fails():
	assert oauth.verify_callback_hmac({"code": "abc"}, SECRET) is False


def test_an_unconfigured_secret_fails_closed():
	params = {"code": "abc", "shop": "s.myshopify.com"}
	params["hmac"] = sign(params)
	assert oauth.verify_callback_hmac(params, "") is False


def test_parameter_order_does_not_matter():
	"""Shopify does not promise an order; the signature is over the sorted parameters."""
	a = {"code": "c", "shop": "s.myshopify.com", "state": "n", "timestamp": "1"}
	a["hmac"] = sign(a)
	reordered = {k: a[k] for k in reversed(list(a))}
	assert oauth.verify_callback_hmac(reordered, SECRET) is True


def test_signature_field_itself_is_excluded_from_the_digest():
	params = {"code": "c", "shop": "s.myshopify.com", "state": "n"}
	params["hmac"] = sign(params)
	params["signature"] = "legacy-value-shopify-may-send"
	assert oauth.verify_callback_hmac(params, SECRET) is True


def test_callback_hmac_is_hex_not_base64():
	"""The webhook receiver uses base64 over the body; this uses hex over the query string.
	Using one routine for both silently rejects every install."""
	params = {"code": "c", "shop": "s.myshopify.com"}
	signature = sign(params)
	assert len(signature) == 64
	assert all(c in "0123456789abcdef" for c in signature)


def test_uses_constant_time_comparison():
	import inspect

	assert "compare_digest" in inspect.getsource(oauth.verify_callback_hmac)


# --------------------------------------------------------------------------------------
# Authorize URL
# --------------------------------------------------------------------------------------


def test_authorize_url_requests_an_offline_token():
	"""No grant_options means offline. An online token expires when the merchant logs out,
	which would break every background sync this app performs."""
	with patch.object(oauth, "callback_url", return_value="https://erp.example.com/cb"):
		url = oauth.authorize_url("my-shop.myshopify.com", "cid", "nonce")
	assert "grant_options" not in url
	assert url.startswith("https://my-shop.myshopify.com/admin/oauth/authorize?")


def test_authorize_url_carries_the_scopes_and_state():
	with patch.object(oauth, "callback_url", return_value="https://erp.example.com/cb"):
		url = oauth.authorize_url("my-shop.myshopify.com", "cid", "nonce-123")
	assert "read_orders" in url
	assert "write_inventory" in url
	# Fulfilment worked in live testing only because the scope had been granted by hand in
	# the Shopify admin. Asked for here, it was missing from the OAuth request entirely, so a
	# normal install would have failed at the first shipment.
	assert "write_merchant_managed_fulfillment_orders" in url
	assert "state=nonce-123" in url


def test_scopes_are_least_privilege():
	"""Nothing speculative: every scope backs a feature that ships today."""
	assert "write_orders" not in oauth.REQUIRED_SCOPES
	assert "read_all_orders" not in oauth.REQUIRED_SCOPES
	assert set(oauth.REQUIRED_SCOPES) == {
		"read_products",
		"write_products",
		"read_orders",
		"read_inventory",
		"write_inventory",
		"read_locations",
		"read_customers",
		# Fulfilment is off by default, but the scope is asked for at install: the alternative
		# is a merchant who ticks "Sync Fulfilments" later and has to re-authorise the app.
		"write_merchant_managed_fulfillment_orders",
	}


def test_write_scope_implies_the_matching_read_scope():
	"""Verified against a live shop: with only write_products granted, reading products works.
	Shopify reports just the write scope, so a naive comparison reports read_products missing
	on every correctly installed store."""
	granted = {"write_products", "write_inventory", "read_orders"}
	effective = oauth.effective_scopes(granted)

	assert "read_products" in effective
	assert "read_inventory" in effective
	assert "read_orders" in effective


def test_effective_scopes_does_not_invent_write_from_read():
	"""The implication runs one way only. read_orders must never imply write_orders."""
	effective = oauth.effective_scopes({"read_orders"})
	assert "write_orders" not in effective


def test_a_genuinely_missing_scope_is_still_reported():
	effective = oauth.effective_scopes({"write_products"})
	assert "read_customers" not in effective
