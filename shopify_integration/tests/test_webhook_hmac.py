"""Webhook HMAC verification (spec §8.1, §15).

This is the app's only publicly reachable endpoint, so these are the tests that stand
between a stranger on the internet and the creation of ERPNext documents.
"""

import base64
import hashlib
import hmac

from shopify_integration.inbound.webhook import verify_hmac

SECRET = "shpss_EXAMPLE_NOT_A_REAL_SECRET"
BODY = b'{"id":123456789,"name":"#1001","total_price":"19.99"}'


def sign(body: bytes, secret: str = SECRET) -> str:
	return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def test_accepts_a_correctly_signed_body():
	assert verify_hmac(BODY, SECRET, sign(BODY)) is True


def test_rejects_a_tampered_body():
	"""The signature is over the raw bytes, so any edit invalidates it."""
	signature = sign(BODY)
	tampered = BODY.replace(b'"19.99"', b'"0.01"')
	assert verify_hmac(tampered, SECRET, signature) is False


def test_rejects_the_wrong_store_secret():
	"""Multi-store: store A's signature must not verify against store B's secret."""
	assert verify_hmac(BODY, "a-different-secret", sign(BODY)) is False


def test_rejects_a_missing_signature():
	assert verify_hmac(BODY, SECRET, "") is False


def test_rejects_when_no_secret_is_configured():
	"""An unconfigured store must fail closed, never open."""
	assert verify_hmac(BODY, "", sign(BODY)) is False


def test_rejects_garbage_signature():
	assert verify_hmac(BODY, SECRET, "not-base64-at-all") is False


def test_empty_body_still_verifies_against_its_own_signature():
	assert verify_hmac(b"", SECRET, sign(b"")) is True


def test_signature_is_over_raw_bytes_not_reserialised_json():
	"""Shopify signs the exact bytes it sent. Parsing and re-dumping the JSON changes
	whitespace and key order, and the signature would no longer match -- which is why the
	receiver reads the raw body before any parsing."""
	reserialised = b'{"id": 123456789, "name": "#1001", "total_price": "19.99"}'
	assert verify_hmac(reserialised, SECRET, sign(BODY)) is False


def test_uses_constant_time_comparison():
	"""A naive == leaks the expected digest one byte at a time via response timing."""
	import inspect

	from shopify_integration.inbound import webhook

	source = inspect.getsource(webhook.verify_hmac)
	assert "compare_digest" in source
