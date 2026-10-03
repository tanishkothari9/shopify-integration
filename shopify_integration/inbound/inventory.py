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
from frappe.utils import add_to_date, cint, cstr, now_datetime

from shopify_integration.inbound.webhook import payload_of
from shopify_integration.outbound.inventory import available_for_location
from shopify_integration.sync.engine import enqueue_sync

#: How long a drift correction waits, so an order landing at the same moment can finish
#: importing first. Long enough to outlast a slow order import, short enough that a genuine
#: disagreement is still fixed promptly.
DRIFT_SETTLE_SECONDS = 45


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

	# Held back, because this notification and the order that caused it arrive together and
	# are processed in parallel. Shopify decrements at checkout and tells us immediately; the
	# matching Sales Order, which is what reserves the unit in ERPNext, takes a second or two
	# longer to import. In that gap ERPNext still believes the unit is free, so a correction
	# drained right now would push the sold item back on sale -- the exact oversell this is
	# meant to prevent. A correction is a safety net rather than something anyone is waiting
	# for, so a short delay costs nothing and the push recomputes from ERPNext when it runs.
	if queued:
		frappe.db.set_value(
			"Shopify Sync Queue",
			queued,
			"next_attempt_at",
			add_to_date(now_datetime(), seconds=DRIFT_SETTLE_SECONDS, as_datetime=True),
			update_modified=False,
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
