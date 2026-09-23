"""Reconciliation: find and repair drift (spec §12).

Webhooks get dropped. Workers get killed. Sites go down for an afternoon. An integration
without a repair job quietly diverges and someone has to babysit it; one with a repair job
converges on its own, and the count of repairs it makes is the signal that something upstream
is failing.

Two halves, matching the two ways things drift:

* **Inventory.** Page every mapped variant's Shopify level, compare it against what ERPNext
  computes, and enqueue a corrective push where they differ.
* **Orders.** Ask Shopify for orders created since the last run and check each one produced a
  Sales Order. Missing ones are replayed through the ordinary webhook path, so they are as
  observable as any other event.

Corrections always run ERPNext -> Shopify. Repairing in the other direction would mean writing
ERPNext stock from Shopify, which has no stable fixed point (spec §10.4).
"""

from __future__ import annotations

import json
from datetime import timezone

import frappe
from frappe.utils import add_to_date, cint, get_datetime, get_system_timezone, now_datetime

from shopify_integration.api.client import ShopifyClient, load_query
from shopify_integration.sync.engine import enqueue_sync

#: Inventory items per levels query. Shopify's `nodes(ids:)` accepts up to 250.
LEVELS_PAGE = 200

#: How far back to look for missed orders when a store has never been reconciled.
FIRST_RUN_LOOKBACK_DAYS = 7


def reconcile_all_stores() -> dict:
	"""Scheduled entry point: reconcile every enabled store."""
	summary = {}
	for store in frappe.get_all(
		"Shopify Store", filters={"enabled": 1, "reconcile_enabled": 1}, pluck="name"
	):
		try:
			summary[store] = reconcile_store(store)
		except Exception:
			frappe.log_error(
				title=f"Shopify reconciliation failed for {store}", message=frappe.get_traceback()
			)
			summary[store] = {"error": True}
	return summary


def reconcile_store(store: str) -> dict:
	"""Reconcile one store and record what was found."""
	store_doc = frappe.get_cached_doc("Shopify Store", store)
	started = now_datetime()

	result = {
		"started_on": str(started),
		"inventory": reconcile_inventory(store_doc),
		"orders": reconcile_orders(store_doc),
	}

	frappe.db.set_value(
		"Shopify Store",
		store,
		{"last_reconciled_on": started, "last_reconciliation_summary": json.dumps(result, indent=2)},
		update_modified=False,
	)
	frappe.db.commit()

	corrections = result["inventory"]["corrected"] + result["orders"]["replayed"]
	if corrections:
		# Worth a log line even on success: a rising correction count over successive runs is
		# how an operator learns that webhooks or the queue are failing upstream.
		frappe.logger("shopify_integration").warning(
			f"Reconciliation repaired {corrections} item(s) for {store}. "
			f"Inventory: {result['inventory']}. Orders: {result['orders']}."
		)
	return result


# --------------------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------------------


def reconcile_inventory(store_doc) -> dict:
	"""Compare Shopify's levels against ERPNext's and enqueue corrections."""
	from shopify_integration.outbound.inventory import available_for_location

	if not store_doc.sync_inventory:
		return {"checked": 0, "drifted": 0, "corrected": 0, "skipped": "sync_inventory is off"}

	links = frappe.get_all(
		"Shopify Item Link",
		filters={"store": store_doc.name, "inventory_item_gid": ["is", "set"]},
		fields=["name", "item_code", "inventory_item_gid"],
	)
	if not links:
		return {"checked": 0, "drifted": 0, "corrected": 0}

	client = ShopifyClient.for_store(store_doc.name)
	by_gid = {link.inventory_item_gid: link for link in links}

	checked = drifted = corrected = 0
	details = []

	for start in range(0, len(links), LEVELS_PAGE):
		page = links[start : start + LEVELS_PAGE]
		ids = [link.inventory_item_gid for link in page]
		data = client.execute(load_query("inventory_levels"), {"ids": ids}, cost_hint=5 + len(ids))

		for node in data.get("nodes") or []:
			if not node:
				continue
			link = by_gid.get(node.get("id"))
			if not link:
				continue

			for level in (node.get("inventoryLevels") or {}).get("nodes") or []:
				location_gid = (level.get("location") or {}).get("id")
				if not location_gid:
					continue

				shopify_available = _available_from(level)
				if shopify_available is None:
					continue

				checked += 1
				erpnext_available = available_for_location(store_doc, link.item_code, location_gid)
				if shopify_available == erpnext_available:
					continue

				drifted += 1
				queued = enqueue_sync(
					store_doc.name,
					"inventory",
					dedupe_key=f"inventory:{store_doc.name}:{link.item_code}:{location_gid}",
					ref_doctype="Shopify Item Link",
					ref_docname=link.name,
					payload={"item_code": link.item_code, "location_gid": location_gid},
				)
				if queued:
					corrected += 1
				details.append(
					{
						"item_code": link.item_code,
						"location": location_gid,
						"shopify": shopify_available,
						"erpnext": erpnext_available,
					}
				)

	return {
		"checked": checked,
		"drifted": drifted,
		"corrected": corrected,
		# Bounded on purpose: a store with thousands of drifted items should not write a
		# multi-megabyte summary onto the store document.
		"examples": details[:20],
	}


def _available_from(level: dict) -> int | None:
	for quantity in level.get("quantities") or []:
		if quantity.get("name") == "available":
			return cint(quantity.get("quantity"))
	return None


# --------------------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------------------


def _as_utc(moment) -> str:
	"""A site-local timestamp expressed as UTC, which is what Shopify's search parses.

	`last_reconciled_on` is written from `now_datetime()`, which is naive local time. Stamping
	a `Z` on it and handing it to Shopify claims it is already UTC, so every run asks for
	orders newer than local-time-read-as-UTC.

	East of UTC that window starts *later* than it should, and the gap is never revisited: an
	IST site reconciling at 03:00 asks next time for everything after 08:30 IST, and an order
	created in between is invisible to every run there will ever be. Reconciliation is the net
	that catches orders whose webhook was dropped, so one lost there is lost for good.
	"""
	from zoneinfo import ZoneInfo

	local = get_datetime(moment)
	if local.tzinfo is None:
		local = local.replace(tzinfo=ZoneInfo(get_system_timezone()))
	return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def reconcile_orders(store_doc) -> dict:
	"""Replay orders Shopify has that ERPNext does not (spec §12).

	The safety net for dropped webhooks and downtime. Replaying through the normal handler,
	rather than building the order here, means a replayed order is logged, retryable and
	debuggable exactly like one that arrived by webhook.
	"""
	if not store_doc.sync_orders:
		return {"checked": 0, "missing": 0, "replayed": 0, "skipped": "sync_orders is off"}

	since = store_doc.last_reconciled_on or add_to_date(now_datetime(), days=-FIRST_RUN_LOOKBACK_DAYS)
	search = f"created_at:>='{_as_utc(since)}'"

	client = ShopifyClient.for_store(store_doc.name)
	checked = missing = replayed = 0
	details = []

	for node in client.paginate(load_query("orders_since"), {"search": search}, "orders", cost_hint=20):
		checked += 1
		order_gid = node.get("id")
		if node.get("cancelledAt"):
			# A cancelled order that never reached us has nothing to book.
			continue

		exists = frappe.db.exists(
			"Sales Order",
			{"shopify_store": store_doc.name, "shopify_order_gid": order_gid, "docstatus": ["<", 2]},
		)
		if exists:
			continue

		missing += 1
		if _replay_order(store_doc.name, node):
			replayed += 1
			details.append({"order": node.get("name"), "gid": order_gid})

	return {"checked": checked, "missing": missing, "replayed": replayed, "examples": details[:20]}


def _replay_order(store: str, node: dict) -> bool:
	"""Feed a missed order back through the ordinary webhook path.

	The event log's unique webhook id doubles as the dedupe: a second reconciliation run that
	finds the same order still missing will not queue it twice while the first attempt is in
	flight.
	"""
	order_gid = node.get("id")
	webhook_id = f"reconcile::{order_gid}"

	if frappe.db.exists("Shopify Event Log", {"webhook_id": webhook_id}):
		return False

	log = frappe.new_doc("Shopify Event Log")
	log.store = store
	log.webhook_id = webhook_id
	log.topic = "orders/create"
	log.payload = json.dumps({"admin_graphql_api_id": order_gid, "id": _numeric(order_gid)})
	log.status = "Queued"
	log.insert(ignore_permissions=True)
	frappe.db.commit()

	frappe.enqueue(
		"shopify_integration.inbound.order.on_order_create",
		queue="long",
		job_id=f"shopify_replay::{log.name}",
		deduplicate=True,
		enqueue_after_commit=True,
		event_log=log.name,
	)
	return True


def _numeric(gid: str | None) -> str:
	return str(gid or "").rstrip("/").split("/")[-1]
