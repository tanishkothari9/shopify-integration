"""Shopify Store -- the root of the data model (spec §5.1).

A normal doctype, not a Single. Multi-store is non-negotiable #1, and everything else in
the app is keyed by store: credentials, throttle state, queue rows, event logs.
"""

from __future__ import annotations

import json
import re
import time

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cstr, get_url

#: Series field -> the doctype whose naming series it draws from.
SERIES_FIELDS = {
	"sales_order_series": "Sales Order",
	"sales_invoice_series": "Sales Invoice",
	"delivery_note_series": "Delivery Note",
	"credit_note_series": "Sales Invoice",
}

API_VERSION_PATTERN = re.compile(r"^\d{4}-\d{2}$")


class ShopifyStore(Document):
	def validate(self):
		self.shop_domain = normalise_shop_domain(self.shop_domain)
		self.validate_api_version()

		from shopify_integration.api.webhooks import callback_url

		self.webhook_callback_url = callback_url(get_url())

	def validate_api_version(self):
		if not API_VERSION_PATTERN.match(self.api_version or ""):
			frappe.throw(
				_("API Version must look like 2026-01 (Shopify releases quarterly), not {0}").format(
					self.api_version
				)
			)

	def on_update(self):
		"""Register or remove webhook subscriptions when the enabled flag flips.

		Enqueued rather than called inline: a save must not block on Shopify, and must not
		fail because Shopify happened to be slow. The user gets immediate feedback from the
		Test Connection button instead.
		"""
		if not self.has_value_changed("enabled"):
			return

		method = "shopify_integration.shopify_integration.doctype.shopify_store.shopify_store." + (
			"register_webhooks" if self.enabled else "unregister_webhooks"
		)
		frappe.enqueue(
			method,
			queue="short",
			job_id=f"shopify_webhook_registration::{self.name}",
			deduplicate=True,
			enqueue_after_commit=True,
			store=self.name,
		)

	def on_trash(self):
		if self.enabled:
			frappe.throw(_("Disable the store before deleting it, so its webhooks are removed."))

	def location_gid_for_warehouse(self, warehouse: str) -> str | None:
		"""Resolve a warehouse to this store's Shopify location.

		Walks up the warehouse tree so that mapping a group warehouse consolidates every
		descendant leaf into one Shopify location (spec §5.2).
		"""
		direct = {row.warehouse: row.location_gid for row in self.location_map}
		if warehouse in direct:
			return direct[warehouse]

		for ancestor in warehouse_ancestors(warehouse):
			if ancestor in direct:
				return direct[ancestor]
		return None


def normalise_shop_domain(domain: str | None) -> str:
	"""Reduce whatever the user pasted to a bare ``my-shop.myshopify.com``, and insist on it.

	The receiver matches incoming ``X-Shopify-Shop-Domain`` headers against this value, so
	a stored ``https://my-shop.myshopify.com/`` would silently never match and every webhook
	would be rejected as unauthorised.

	It must also *be* a Shopify host. This field is what `ShopifyClient` builds its request URL
	from, and every request carries the store's Admin API token. Trimming the value without
	checking it -- which is what this did -- let a store be saved pointing at
	`attacker.example.com`, `127.0.0.1:8000` or `169.254.169.254`, and the next sync or Test
	Connection would hand the token straight to it.

	`api.oauth` already held the pattern for exactly this. The bug was that this function
	existed alongside it and did not use it, so the guard covered the OAuth path and missed
	the field that actually builds the URLs.
	"""
	if not domain:
		return ""

	from shopify_integration.api.oauth import normalise_shop_domain as strict

	# Trim what a person actually pastes -- they copy the URL out of the browser, scheme and
	# trailing path and all -- and only then insist it is a Shopify host. Validating before
	# trimming rejects `https://my-shop.myshopify.com/`, which is the commonest way anyone
	# enters it.
	trimmed = re.sub(r"^https?://", "", cstr(domain).strip().lower())
	trimmed = trimmed.rstrip("/").split("/")[0]

	cleaned = strict(trimmed)
	if not cleaned:
		frappe.throw(
			_(
				"{0} is not a Shopify shop domain. It must look like "
				"<strong>your-store.myshopify.com</strong>."
			).format(frappe.utils.escape_html(cstr(domain))),
			title=_("Invalid Shop Domain"),
		)
	return cleaned


def warehouse_ancestors(warehouse: str) -> list[str]:
	"""Warehouse names from the immediate parent upward."""
	ancestors = []
	current = frappe.db.get_value("Warehouse", warehouse, "parent_warehouse")
	seen = set()
	while current and current not in seen:
		seen.add(current)
		ancestors.append(current)
		current = frappe.db.get_value("Warehouse", current, "parent_warehouse")
	return ancestors


@frappe.whitelist()
def get_series() -> dict[str, str]:
	"""Naming series options for each series field, read from the target doctype.

	Whitelisted, so any logged-in user could reach it. It only reads doctype metadata, but
	there is no reason for someone who cannot configure a store to learn how the site names
	its Sales Orders.
	"""
	frappe.has_permission("Shopify Store", "write", throw=True)
	options = {}
	for fieldname, doctype in SERIES_FIELDS.items():
		field = frappe.get_meta(doctype).get_field("naming_series")
		options[fieldname] = field.options if field else ""
	return options


@frappe.whitelist()
def test_connection(store: str) -> dict:
	"""Prove the token, shop domain and pinned API version agree. Called from the form.

	Shopify's failures are translated into something a user can act on. Letting the raw
	exception escape gives them a "Server Error" dialog containing a Python class name, which
	is precisely the kind of unactionable failure §14 exists to prevent -- and this is the
	first button anyone presses, so it is the worst possible place for it.
	"""
	frappe.has_permission("Shopify Store", "write", doc=store, throw=True)

	from shopify_integration.api.client import ShopifyClient, load_query
	from shopify_integration.exceptions import (
		ShopifyConfigurationError,
		ShopifyError,
		ShopifyGraphQLError,
		ShopifyThrottled,
		ShopifyTransportError,
	)

	doc = frappe.get_cached_doc("Shopify Store", store)

	try:
		client = ShopifyClient.for_store(store)
		data = client.execute(load_query("shop"), cost_hint=1)
	except ShopifyConfigurationError as exc:
		return {"ok": False, "problem": str(exc), "hint": _("Check the store's credentials.")}
	except ShopifyThrottled:
		return {
			"ok": False,
			"problem": _("Shopify is rate-limiting this store right now."),
			"hint": _("Nothing is wrong with the configuration. Try again in a minute."),
		}
	except ShopifyTransportError as exc:
		return {
			"ok": False,
			"problem": _("Could not reach Shopify: {0}").format(exc),
			"hint": _("Check the shop domain and this server's outbound network access."),
		}
	except ShopifyGraphQLError as exc:
		message = str(exc)
		if "404" in message:
			hint = _(
				"Shopify does not recognise the shop domain '{0}'. It should look like "
				"my-shop.myshopify.com, not your custom domain."
			).format(doc.shop_domain)
		elif "401" in message or "ACCESS_DENIED" in message.upper():
			hint = _(
				"Shopify rejected the Admin API access token, or it lacks the scopes this app "
				"needs. Regenerate it on the custom app in your Shopify admin."
			)
		else:
			hint = _("Check the shop domain, the access token and the pinned API version.")
		return {"ok": False, "problem": message, "hint": hint}
	except ShopifyError as exc:
		return {"ok": False, "problem": str(exc), "hint": None}

	shop = data.get("shop") or {}
	return {
		"ok": True,
		"name": shop.get("name"),
		"domain": shop.get("myshopifyDomain"),
		"currency": shop.get("currencyCode"),
		"timezone": shop.get("ianaTimezone"),
	}


@frappe.whitelist()
def import_catalogue(store: str) -> dict:
	"""Start a bulk catalogue import for this store (spec §11)."""
	frappe.has_permission("Shopify Store", "write", doc=store, throw=True)

	from shopify_integration.api import bulk

	operation = bulk.start_import(store)
	return {"operation": operation}


@frappe.whitelist()
def store_status(store: str) -> dict:
	"""Everything the dashboard shows for one store (spec §14).

	The design rule behind this: "it didn't sync" must always have an answer. Pending and
	failed counts say whether work is stuck, the last success says when it last worked, API
	headroom says whether Shopify is throttling us, and the reconciliation figures say whether
	drift is being found -- a rising count is the signal that something upstream is failing.
	"""
	frappe.has_permission("Shopify Store", "read", doc=store, throw=True)

	counts = {}
	for state in ("Pending", "Running", "Done", "Failed"):
		counts[state.lower()] = frappe.db.count("Shopify Sync Queue", {"store": store, "state": state})

	last_success = frappe.db.get_value(
		"Shopify Sync Queue",
		{"store": store, "state": "Done"},
		"modified",
		order_by="modified desc",
	)

	events = {}
	for status in ("Queued", "Success", "Error", "Skipped"):
		events[status.lower()] = frappe.db.count("Shopify Event Log", {"store": store, "status": status})

	doc = frappe.get_cached_doc("Shopify Store", store)
	summary = None
	if doc.last_reconciliation_summary:
		try:
			summary = json.loads(doc.last_reconciliation_summary)
		except ValueError:
			summary = None

	return {
		"queue": counts,
		"events": events,
		"last_successful_sync": str(last_success) if last_success else None,
		"last_reconciled_on": str(doc.last_reconciled_on) if doc.last_reconciled_on else None,
		"reconciliation": summary,
		"api_headroom": _api_headroom(store),
	}


def _api_headroom(store: str) -> dict | None:
	"""Shopify's remaining cost budget for this store, as the throttle last saw it."""
	try:
		from shopify_integration.api.throttle import AdaptiveThrottle

		throttle = AdaptiveThrottle(store)
		state = throttle.read_state()
		if not state:
			return None
		# time.time(), not a Frappe helper: the throttle stores its snapshot against the
		# wall clock so that worker processes can compare against a shared reference.
		return {
			"available": round(state.projected_available(time.time()), 1),
			"maximum": state.maximum_available,
			"restore_rate": state.restore_rate,
		}
	except Exception:
		# The dashboard must render even when Redis is unreachable.
		return None


@frappe.whitelist()
def run_reconciliation(store: str) -> dict:
	"""Run the drift check now, from the store form."""
	frappe.has_permission("Shopify Store", "write", doc=store, throw=True)

	from shopify_integration.sync.reconcile import reconcile_store

	return reconcile_store(store)


def register_webhooks(store: str) -> dict:
	from shopify_integration.api import webhooks
	from shopify_integration.api.client import ShopifyClient

	result = webhooks.register(ShopifyClient.for_store(store), get_url())
	frappe.logger("shopify_integration").info(f"Registered webhooks for {store}: {result}")

	if result.get("failed"):
		topics = ", ".join(entry["topic"] for entry in result["failed"])
		reason = result["failed"][0]["reason"]
		# Logged as an error, not a warning: an app silently missing its order webhooks looks
		# healthy right up until the first sale fails to arrive.
		frappe.log_error(
			title=f"Shopify refused {len(result['failed'])} webhook topic(s) for {store}",
			message=(
				f"Topics: {topics}\n\nFirst reason: {reason}\n\n"
				"Order, refund and customer topics require Protected Customer Data approval. "
				"Request it on the app in the Shopify Dev Dashboard, then re-enable the store."
			),
		)
	return result


def unregister_webhooks(store: str) -> dict:
	from shopify_integration.api import webhooks
	from shopify_integration.api.client import ShopifyClient, StoreCredentials

	# The store is disabled by now, so credentials_for_store() would refuse it. Read the
	# document directly: removing the subscriptions is exactly what disabling must do.
	doc = frappe.get_doc("Shopify Store", store)
	token = doc.get_password("admin_api_token", raise_exception=False)
	if not token:
		return {"removed": []}

	client = ShopifyClient(
		StoreCredentials(
			store=store,
			shop_domain=doc.shop_domain,
			access_token=token,
			api_version=doc.api_version,
		)
	)
	result = webhooks.unregister(client)
	frappe.logger("shopify_integration").info(f"Removed webhooks for {store}: {result}")
	return result
