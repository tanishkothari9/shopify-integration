"""Drift detection from `inventory_levels/update` (spec §10.4).

ERPNext is the master for stock. This handler exists only to notice when Shopify disagrees
with it, and never to write ERPNext stock from Shopify.

That restraint is the whole point. Writing ERPNext stock from a Shopify level creates a
feedback loop with no stable fixed point: each system keeps correcting the other, and a
transient disagreement becomes a permanent oscillation. So when the two disagree, ERPNext
wins and Shopify is corrected -- always in that direction.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint, cstr

from shopify_integration.inbound.webhook import payload_of
from shopify_integration.outbound.inventory import available_for_location
from shopify_integration.sync.engine import enqueue_sync


def on_inventory_level_update(event_log: str):
	"""Compare Shopify's level with ERPNext's, and enqueue a correction if they differ."""
	log = frappe.get_doc("Shopify Event Log", event_log)
	try:
		payload = payload_of(event_log)
		result = detect_drift(log.store, payload)
		frappe.db.commit()
		log.mark_success(ref_doctype="Shopify Sync Queue", ref_docname=result.get("queued"))
		return result
	except Exception:
		log.mark_error(frappe.get_traceback())
		raise


def detect_drift(store: str, payload: dict) -> dict:
	"""Return what the comparison found, enqueuing a corrective push on a real mismatch."""
	inventory_item_gid = _gid(payload.get("inventory_item_id"), "InventoryItem")
	location_gid = _gid(payload.get("location_id"), "Location")
	if not inventory_item_gid or not location_gid:
		return {"skipped": "payload carried no inventory item or location"}

	store_doc = frappe.get_cached_doc("Shopify Store", store)
	if not store_doc.sync_inventory:
		return {"skipped": "sync_inventory is off"}

	link = frappe.db.get_value(
		"Shopify Item Link",
		{"store": store, "inventory_item_gid": inventory_item_gid},
		["name", "item_code"],
		as_dict=True,
	)
	if not link:
		return {"skipped": "no mapping for this inventory item"}

	shopify_available = cint(payload.get("available"))
	erpnext_available = available_for_location(store_doc, link.item_code, location_gid)

	if shopify_available == erpnext_available:
		return {"drift": False, "item_code": link.item_code}

	dedupe_key = f"inventory:{store}:{link.item_code}:{location_gid}"
	if _push_already_pending(dedupe_key):
		# A correction is already queued; this notification is almost certainly the echo of
		# our own last write arriving back. Counting it as drift would inflate the drift
		# metric that is supposed to tell operators when something is actually wrong.
		return {"drift": False, "reason": "a push is already pending", "item_code": link.item_code}

	frappe.logger("shopify_integration").info(
		f"Inventory drift for {link.item_code} at {location_gid}: "
		f"Shopify {shopify_available}, ERPNext {erpnext_available}. Correcting Shopify."
	)

	queued = enqueue_sync(
		store,
		"inventory",
		dedupe_key=dedupe_key,
		ref_doctype="Shopify Item Link",
		ref_docname=link.name,
		payload={"item_code": link.item_code, "location_gid": location_gid},
	)
	return {
		"drift": True,
		"item_code": link.item_code,
		"shopify": shopify_available,
		"erpnext": erpnext_available,
		"queued": queued,
	}


def _push_already_pending(dedupe_key: str) -> bool:
	return bool(frappe.db.exists("Shopify Sync Queue", {"pending_dedupe_key": dedupe_key}))


def _gid(value, resource: str) -> str | None:
	"""Webhook payloads carry legacy numeric ids; the rest of the app speaks GIDs."""
	if not value:
		return None
	text = cstr(value)
	if text.startswith("gid://"):
		return text
	return f"gid://shopify/{resource}/{text}"
