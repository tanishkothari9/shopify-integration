"""Webhook receiver (spec §8.1, §15).

This is the app's only publicly reachable endpoint, so it is deliberately the dumbest code
in the repo. It does four things and nothing else: verify, deduplicate, log, enqueue.

Three constraints shape every line of it:

1. Shopify treats a response slower than ~5s as a failure, so no document work happens
   inline -- handlers run on a worker.
2. A 5xx makes Shopify retry for ~48 hours, so an unrecognised topic returns 200 and is
   marked Skipped rather than raising.
3. The endpoint is ``allow_guest=True``, so nothing may touch the database on behalf of the
   caller until the HMAC has verified.

Ordering note: the spec's §8.1 sketch verifies HMAC at step 2 and resolves the store at
step 3, but its own closing note corrects this -- the secret is per-store, so the store must
be resolved from the shop-domain header first. Resolution is a single indexed read of a
doctype we control, with no side effects, which is why it is safe to do pre-verification.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import frappe

#: Shopify's own cap is 20 MiB on some resources; anything near it is not a real webhook.
#: Capping before hashing keeps a hostile caller from making us digest arbitrary megabytes.
MAX_BODY_BYTES = 5 * 1024 * 1024

HEADER_HMAC = "X-Shopify-Hmac-Sha256"
HEADER_TOPIC = "X-Shopify-Topic"
HEADER_DOMAIN = "X-Shopify-Shop-Domain"
HEADER_WEBHOOK_ID = "X-Shopify-Webhook-Id"

#: Slash-form topic -> dotted path of the handler that processes it.
#: Each phase adds its handlers here. A topic with no entry is logged Skipped, which is why
#: subscribing to every topic up front is safe.
TOPIC_HANDLERS: dict[str, str] = {
	"orders/create": "shopify_integration.inbound.order.on_order_create",
	"orders/paid": "shopify_integration.inbound.order.on_order_paid",
	"orders/fulfilled": "shopify_integration.inbound.order.on_order_fulfilled",
	"orders/partially_fulfilled": "shopify_integration.inbound.order.on_order_fulfilled",
	"orders/cancelled": "shopify_integration.inbound.order.on_order_cancelled",
	"refunds/create": "shopify_integration.inbound.refund.on_refund_create",
	"products/create": "shopify_integration.inbound.product.on_product_create",
	"products/update": "shopify_integration.inbound.product.on_product_update",
	"products/delete": "shopify_integration.inbound.product.on_product_delete",
	# Drift detection only. This never writes ERPNext stock -- see inbound/inventory.py.
	"inventory_levels/update": "shopify_integration.inbound.inventory.on_inventory_level_update",
	"customers/create": "shopify_integration.inbound.customer.on_customer_create",
	"customers/update": "shopify_integration.inbound.customer.on_customer_update",
}


def verify_hmac(raw_body: bytes, secret: str, provided_hmac: str) -> bool:
	"""Constant-time HMAC-SHA256 (base64) check over the raw request body.

	Pure and dependency-free so it can be tested directly.

	``compare_digest`` rather than ``==``: a naive comparison returns as soon as two bytes
	differ, and that timing difference leaks the expected digest one byte at a time to a
	caller willing to make enough requests.
	"""
	if not secret or not provided_hmac:
		return False
	digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
	expected = base64.b64encode(digest).decode()
	return hmac.compare_digest(expected, provided_hmac)


def resolve_store(shop_domain: str) -> str | None:
	"""Find the enabled Shopify Store for a shop domain, or None.

	Multi-store from day one: the shop domain is what tells us *which* store's secret to
	verify against, so it is read before anything else.
	"""
	if not shop_domain:
		return None
	return frappe.db.get_value(
		"Shopify Store", {"shop_domain": shop_domain.strip().lower(), "enabled": 1}, "name"
	)


@frappe.whitelist(allow_guest=True)
def webhook():
	"""Receive, verify, deduplicate, log and enqueue one Shopify webhook."""
	request = frappe.local.request
	raw_body = request.get_data()

	if len(raw_body) > MAX_BODY_BYTES:
		return _respond(413, "Payload too large")

	headers = request.headers
	shop_domain = headers.get(HEADER_DOMAIN, "")
	topic = headers.get(HEADER_TOPIC, "")
	webhook_id = headers.get(HEADER_WEBHOOK_ID, "")
	provided_hmac = headers.get(HEADER_HMAC, "")

	store = resolve_store(shop_domain)
	if not store:
		# Unknown or disabled shop. 401 rather than 404: saying "no such store" would let an
		# unauthenticated caller enumerate which shops this site is connected to.
		return _respond(401, "Unauthorised")

	secret = frappe.get_cached_doc("Shopify Store", store).get_password("api_secret", raise_exception=False)
	if not verify_hmac(raw_body, secret or "", provided_hmac):
		frappe.logger("shopify_integration").warning(
			f"Rejected webhook with invalid HMAC for store {store} (topic {topic or 'unknown'})"
		)
		return _respond(401, "Unauthorised")

	# --- verified beyond this point; the payload is now trusted enough to store ---

	if not webhook_id:
		# Without an id there is no dedupe key, and Shopify's at-least-once delivery would
		# eventually double-post a document. Refusing is safer than guessing.
		return _respond(400, "Missing webhook id")

	if frappe.db.exists("Shopify Event Log", {"webhook_id": webhook_id}):
		# Redelivery of something already accepted. 200 without reprocessing: Shopify retries
		# for ~48h and duplicates are normal traffic, not an error.
		return _respond(200, "Duplicate")

	handler = TOPIC_HANDLERS.get(topic)
	try:
		event = _log_event(
			store=store,
			webhook_id=webhook_id,
			topic=topic,
			raw_body=raw_body,
			status="Queued" if handler else "Skipped",
		)
	except frappe.DuplicateEntryError:
		# Shopify delivers at least once and sometimes twice at the same instant. Both copies
		# pass the `exists` check above, and the loser's insert hits the unique index on
		# webhook_id. Left uncaught that is a 500, and a 5xx makes Shopify retry the same
		# webhook for about 48 hours. It is the same duplicate the check above already answers
		# with a 200.
		return _respond(200, "Duplicate")

	if not handler:
		return _respond(200, "Skipped")

	frappe.enqueue(
		run_handler,
		queue="default",
		job_id=f"shopify_webhook::{event}",
		deduplicate=True,
		enqueue_after_commit=True,
		event_log=event,
		handler=handler,
	)
	return _respond(200, "Queued")


def run_handler(event_log: str, handler: str):
	"""Run a webhook handler as Administrator.

	This endpoint is ``allow_guest=True``, so the request belongs to Guest -- and Frappe copies
	``frappe.session.user`` onto the background job, so the handler would run as Guest too.

	That fails, and not in a way ``ignore_permissions`` can rescue: building a Sales Order takes
	ERPNext through ``get_item_details``, which calls ``check_permission()`` on the Item itself,
	below the level any flag on the parent document reaches.

	Administrator is the right identity because the work is not being done on the caller's
	behalf. The caller is Shopify, which has no ERPNext user; the app is acting on its own
	configuration. Nothing here is derived from the session -- the handler reads only the stored
	event log -- so widening the identity grants the payload no new reach.
	"""
	frappe.set_user("Administrator")
	return frappe.get_attr(handler)(event_log=event_log)


def _log_event(*, store: str, webhook_id: str, topic: str, raw_body: bytes, status: str) -> str:
	"""Persist the raw payload. Returns the Shopify Event Log name.

	The body is stored exactly as received so a Retry replays precisely what Shopify sent,
	and so support can read what actually arrived rather than our interpretation of it.
	"""
	try:
		payload = raw_body.decode("utf-8")
	except UnicodeDecodeError:
		payload = raw_body.decode("utf-8", errors="replace")

	doc = frappe.new_doc("Shopify Event Log")
	doc.store = store
	doc.webhook_id = webhook_id
	doc.topic = topic or "unknown"
	doc.payload = payload
	doc.status = status
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _respond(status_code: int, message: str) -> dict:
	frappe.local.response["http_status_code"] = status_code
	return {"message": message}


def payload_of(event_log_name: str) -> dict:
	"""Parse a stored payload back into a dict, for handlers and for Retry."""
	raw = frappe.db.get_value("Shopify Event Log", event_log_name, "payload")
	if not raw:
		return {}
	try:
		return json.loads(raw)
	except ValueError:
		frappe.throw(f"Shopify Event Log {event_log_name} does not contain valid JSON")
		return {}
