"""Product webhook handlers (spec §8.7).

Each handler takes the name of a Shopify Event Log, reads the stored payload, does its work,
and records the outcome on that log. Handlers must be idempotent: Shopify delivers at least
once, and a Retry replays the same payload deliberately.
"""

from __future__ import annotations

import frappe
from frappe import _

from shopify_integration.api.client import ShopifyClient, load_query
from shopify_integration.catalogue.echo import inbound_write
from shopify_integration.catalogue.mapping import write_product_mapping
from shopify_integration.inbound.webhook import payload_of


def on_product_create(event_log: str):
	return _upsert_from_webhook(event_log)


def on_product_update(event_log: str):
	return _upsert_from_webhook(event_log)


def on_product_delete(event_log: str):
	"""Unlink, never delete the ERPNext Item (spec §8.2).

	The Item may carry stock, ledger history and open orders. Shopify removing a listing says
	nothing about any of that, so the mapping goes and the Item stays.
	"""
	log = frappe.get_doc("Shopify Event Log", event_log)
	try:
		payload = payload_of(event_log)
		product_gid = _product_gid(payload)

		links = frappe.get_all(
			"Shopify Item Link",
			filters={"store": log.store, "product_gid": product_gid},
			pluck="name",
		)
		for name in links:
			frappe.delete_doc("Shopify Item Link", name, ignore_permissions=True, force=True)
		frappe.db.commit()

		log.mark_success(ref_doctype="Shopify Item Link", ref_docname=f"{len(links)} unlinked")
		return {"unlinked": len(links)}
	except Exception:
		log.mark_error(frappe.get_traceback())
		raise


def _upsert_from_webhook(event_log: str):
	"""Refetch the product over GraphQL, then map it.

	The webhook body is the REST-shaped payload and is not the shape the mapping writer
	expects. Refetching costs one cheap call and means all three import paths feed the writer
	identical data -- which is the only reason one writer can serve all of them.
	"""
	log = frappe.get_doc("Shopify Event Log", event_log)
	try:
		payload = payload_of(event_log)
		product_gid = _product_gid(payload)
		if not product_gid:
			log.mark_error("Webhook payload carried no product id")
			return {"skipped": "no product id"}

		client = ShopifyClient.for_store(log.store)
		data = client.execute(load_query("product_by_id"), {"id": product_gid}, cost_hint=10)
		product = data.get("product")

		if not product:
			# Created and deleted before we got here. Not an error.
			log.mark_success(ref_doctype=None, ref_docname=None)
			return {"skipped": "product no longer exists"}

		variants = product.get("variants") or {}
		if (variants.get("pageInfo") or {}).get("hasNextPage"):
			# The query selects pageInfo and nothing was checking it, so a product past 100
			# variants quietly imported its first hundred and dropped the rest -- and the
			# missing ones then look like items that were never on Shopify. Refusing is what
			# the order path does with more than 250 line items, for the same reason.
			frappe.throw(
				_(
					"Shopify product {0} has more than 100 variants, which this version does "
					"not page through. Raise it with the maintainers rather than importing a "
					"partial product."
				).format(product.get("title") or product_gid)
			)

		product["variants"] = [edge["node"] for edge in (variants.get("edges") or [])]

		with inbound_write():
			result = write_product_mapping(log.store, product)
		frappe.db.commit()

		log.mark_success(ref_doctype="Item", ref_docname=result["item"])
		return result
	except Exception:
		log.mark_error(frappe.get_traceback())
		raise


def _product_gid(payload: dict) -> str | None:
	"""Get a product GID from a webhook body, which may use either id form.

	Webhook payloads carry the legacy numeric id; some also carry admin_graphql_api_id. Take
	the GID when present, otherwise build it.
	"""
	gid = payload.get("admin_graphql_api_id")
	if gid:
		return gid
	numeric = payload.get("id")
	return f"gid://shopify/Product/{numeric}" if numeric else None
