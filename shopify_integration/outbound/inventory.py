"""Real-time inventory: ERPNext -> Shopify (spec §10).

The flagship feature. A sale at the POS counter reaches Shopify in seconds rather than on a
five-to-sixty-minute timer, and it does so without the counter ever waiting on the network:
the doc_event writes a queue row, a worker drains it.

Two things here are easy to get wrong and expensive to get wrong:

* **reserved_qty is part of the formula.** Between a web order arriving and its Delivery Note
  being made, ``actual_qty`` has not yet dropped but the submitted Sales Order has raised
  ``reserved_qty``. Leaving it out pushes the pre-sale quantity back to Shopify and re-exposes
  units that are already sold.
* **The value is read at drain time, never at trigger time.** The queue row records only that
  an item needs pushing. That is what makes duplicate and out-of-order rows harmless.
"""

from __future__ import annotations

import json
import math
from urllib.parse import quote

import frappe
from frappe.utils import cint, cstr, flt

from shopify_integration.api.client import ShopifyClient, load_query
from shopify_integration.exceptions import ShopifyInventoryConflict, ShopifyUserError
from shopify_integration.sync.engine import enqueue_sync

#: Items per inventorySetQuantities call. Shopify caps a bulk-ish mutation well above this;
#: the limit here is about keeping one call's cost, and one failure's blast radius, modest.
BATCH_SIZE = 100

#: Shopify's required `reason`. "correction" is the honest one: ERPNext is the source of
#: truth and we are correcting Shopify to match it.
ADJUSTMENT_REASON = "correction"

#: userError codes that mean "the value changed under you" rather than "your request is bad".
COMPARE_MISMATCH_CODES = frozenset(
	{"COMPARE_QUANTITY_STALE", "STALE_COMPARE_QUANTITY", "INVALID_COMPARE_QUANTITY"}
)


# --------------------------------------------------------------------------------------
# The quantity formula (spec §10.1)
# --------------------------------------------------------------------------------------


def available_quantity(item_code: str, warehouse: str) -> int:
	"""Units Shopify may sell, for one item in one warehouse.

	``actual_qty - reserved_qty``, floored to an integer because Shopify does not accept
	fractional inventory.

	The spec writes this as ``int(actual_qty) - int(reserved_qty)``; flooring the difference
	instead is deliberate. With 10.2 on hand and 2.9 reserved, truncating separately gives
	10 - 2 = 8 while the true figure is 7.3 -- an over-report, which is the direction that
	oversells. Flooring the difference gives 7, and under-reporting by a fraction of a unit
	only ever costs a sale that could not have been fulfilled anyway.

	The result is deliberately not clamped at zero. A negative availability is real
	information, and clamping would make drift detection see a permanent disagreement between
	ERPNext and Shopify that it would try to correct forever.
	"""
	bin_row = frappe.db.get_value(
		"Bin",
		{"item_code": item_code, "warehouse": warehouse},
		["actual_qty", "reserved_qty"],
		as_dict=True,
	)
	if not bin_row:
		return 0

	return math.floor(flt(bin_row.actual_qty) - flt(bin_row.reserved_qty))


def available_for_location(store_doc, item_code: str, location_gid: str) -> int:
	"""Availability across every warehouse this store maps to one Shopify location.

	A group warehouse consolidates its descendants into one location (spec §5.2), so the
	figure Shopify gets is the sum over all the warehouses feeding that location.
	"""
	total = 0
	for warehouse in warehouses_for_location(store_doc, location_gid):
		total += available_quantity(item_code, warehouse)
	return total


def warehouses_for_location(store_doc, location_gid: str) -> list[str]:
	"""Leaf warehouses feeding a Shopify location."""
	leaves: list[str] = []
	for row in store_doc.location_map or []:
		if row.location_gid != location_gid:
			continue
		if frappe.db.get_value("Warehouse", row.warehouse, "is_group"):
			leaves.extend(_descendant_warehouses(row.warehouse))
		else:
			leaves.append(row.warehouse)
	return leaves


def _descendant_warehouses(group: str) -> list[str]:
	"""Leaf warehouses under a group, using the nested set ranges ERPNext maintains."""
	bounds = frappe.db.get_value("Warehouse", group, ["lft", "rgt"], as_dict=True)
	if not bounds:
		return []
	return frappe.get_all(
		"Warehouse",
		filters={"lft": [">=", bounds.lft], "rgt": ["<=", bounds.rgt], "is_group": 0},
		pluck="name",
	)


# --------------------------------------------------------------------------------------
# Triggers (spec §10.2)
# --------------------------------------------------------------------------------------


def on_stock_movement(doc, method=None):
	"""Stock Ledger Entry on_submit: every physical movement.

	Covers POS, Delivery Notes, Stock Entries, Purchase Receipts and Reconciliations in one
	hook, because ERPNext creates SLEs via ``sle.submit()`` so doc events do fire. (Do not be
	tempted to hook Bin instead -- it is written with ``db_update()``, which fires nothing.)
	"""
	enqueue_for_item(doc.item_code, doc.warehouse, "Stock Ledger Entry", doc.name)


def on_reservation_change(doc, method=None):
	"""Sales Order on_submit / on_cancel: reserved_qty moved.

	The SLE hook alone would miss this entirely. Between a web order and its delivery note
	nothing physical has moved, yet the sellable quantity has dropped.
	"""
	for item in doc.get("items") or []:
		warehouse = item.get("warehouse") or doc.get("set_warehouse")
		if warehouse:
			enqueue_for_item(item.item_code, warehouse, doc.doctype, doc.name)


def enqueue_for_item(item_code: str, warehouse: str, ref_doctype: str, ref_docname: str) -> int:
	"""Queue an inventory push for every store that sells this item from this warehouse.

	Runs inside the user's save transaction, including at the POS counter, so the early exit
	matters: in an 87,000-item catalogue most stock movements touch something Shopify has
	never heard of, and those must cost one indexed read and nothing more.
	"""
	if not item_code or not warehouse:
		return 0

	stores = frappe.get_all(
		"Shopify Item Link", filters={"item_code": item_code}, pluck="store", distinct=True
	)
	if not stores:
		return 0

	queued = 0
	for store in stores:
		if not frappe.db.get_value("Shopify Store", store, "sync_inventory"):
			continue

		store_doc = frappe.get_cached_doc("Shopify Store", store)
		location_gid = store_doc.location_gid_for_warehouse(warehouse)
		if not location_gid:
			# This warehouse does not feed any Shopify location for this store.
			continue

		enqueue_sync(
			store,
			"inventory",
			dedupe_key=f"inventory:{store}:{item_code}:{location_gid}",
			ref_doctype=ref_doctype,
			ref_docname=ref_docname,
			payload={"item_code": item_code, "location_gid": location_gid},
		)
		queued += 1
	return queued


# --------------------------------------------------------------------------------------
# The push (spec §10.3)
# --------------------------------------------------------------------------------------


def push_inventory(store: str, rows: list[dict]) -> None:
	"""Queue handler for the ``inventory`` operation. Batches a whole drain group.

	Batching many items into one mutation is the entire point of moving off REST, where the
	same work cost two calls per item.
	"""
	store_doc = frappe.get_cached_doc("Shopify Store", store)
	client = ShopifyClient.for_store(store)

	targets = _resolve_targets(store_doc, rows)
	if not targets:
		return

	for start in range(0, len(targets), BATCH_SIZE):
		batch = targets[start : start + BATCH_SIZE]
		_push_batch(client, store_doc, batch, allow_retry=True)


def _resolve_targets(store_doc, rows: list[dict]) -> list[dict]:
	"""Turn queue rows into what the mutation needs, reading live state.

	The queue payload is a hint and is never trusted for the quantity: the whole design rests
	on reading the current figure here, at drain time.
	"""
	targets = []
	seen = set()

	for row in rows:
		item_code, location_gid = _row_target(row)
		if not item_code or not location_gid:
			continue
		if (item_code, location_gid) in seen:
			continue
		seen.add((item_code, location_gid))

		link = frappe.db.get_value(
			"Shopify Item Link",
			{"store": store_doc.name, "item_code": item_code},
			["name", "inventory_item_gid"],
			as_dict=True,
		)
		if not link or not link.inventory_item_gid:
			# No cached inventory item GID means the catalogue import has not run for this
			# item. Skipping is right: there is nothing on Shopify to update yet.
			continue

		targets.append(
			{
				"link": link.name,
				"item_code": item_code,
				"location_gid": location_gid,
				"inventory_item_gid": link.inventory_item_gid,
				"quantity": available_for_location(store_doc, item_code, location_gid),
			}
		)
	return targets


def _row_target(row: dict) -> tuple[str | None, str | None]:
	"""item_code and location from a queue row, preferring the dedupe key.

	The key is authoritative because it is what the unique index enforces; the payload is a
	convenience that a coalesced row may have written earlier.
	"""
	key = row.get("dedupe_key") or ""
	parts = key.split(":")
	if len(parts) >= 4 and parts[0] == "inventory":
		# inventory:{store}:{item_code}:{location_gid} -- the location itself contains colons,
		# so rejoin everything past the item code.
		return parts[2], ":".join(parts[3:])

	payload = row.get("payload")
	if payload:
		try:
			data = json.loads(payload) if isinstance(payload, str) else payload
			return data.get("item_code"), data.get("location_gid")
		except (ValueError, AttributeError):
			pass
	return None, None


def _push_batch(client: ShopifyClient, store_doc, batch: list[dict], allow_retry: bool) -> None:
	"""One inventorySetQuantities call for a batch, with compare-and-set."""
	current = _current_levels(client, batch)

	quantities = []
	for target in batch:
		entry = {
			"inventoryItemId": target["inventory_item_gid"],
			"locationId": target["location_gid"],
			"quantity": cint(target["quantity"]),
		}
		compare = current.get((target["inventory_item_gid"], target["location_gid"]))
		if compare is not None:
			entry["compareQuantity"] = cint(compare)
		quantities.append(entry)

	try:
		client.execute(
			load_query("inventory_set_quantities"),
			{
				"input": {
					"name": "available",
					"reason": ADJUSTMENT_REASON,
					"referenceDocumentUri": reference_uri(store_doc.name),
					"quantities": quantities,
				}
			},
			cost_hint=10 + len(quantities),
		)
	except ShopifyUserError as exc:
		if not _is_compare_mismatch(exc):
			raise

		if allow_retry:
			# The level moved between our read and our write -- another worker, or a human in
			# the Shopify admin. Re-read and try once more, in process, because that usually
			# settles it and costs nothing.
			_push_batch(client, store_doc, batch, allow_retry=False)
			return

		# Still losing the race. Hand it back to the queue as *retryable* rather than failing
		# it outright: backing off and re-reading is the only correct answer to optimistic
		# concurrency losing, and this is the one userErrors refusal that is worth retrying.
		raise ShopifyInventoryConflict(str(exc), user_errors=getattr(exc, "user_errors", None))

	stamp_synced([target["link"] for target in batch])


def _current_levels(client: ShopifyClient, batch: list[dict]) -> dict[tuple[str, str], int]:
	"""Shopify's current 'available' per (inventory item, location), for compareQuantity."""
	ids = sorted({target["inventory_item_gid"] for target in batch})
	if not ids:
		return {}

	data = client.execute(load_query("inventory_levels"), {"ids": ids}, cost_hint=5 + len(ids))

	levels: dict[tuple[str, str], int] = {}
	for node in data.get("nodes") or []:
		if not node:
			continue
		item_gid = node.get("id")
		for level in (node.get("inventoryLevels") or {}).get("nodes") or []:
			location_gid = (level.get("location") or {}).get("id")
			for quantity in level.get("quantities") or []:
				if quantity.get("name") == "available":
					levels[(item_gid, location_gid)] = cint(quantity.get("quantity"))
	return levels


def _is_compare_mismatch(exc: ShopifyUserError) -> bool:
	codes = {
		str((err or {}).get("code", "")).upper() for err in (exc.user_errors or []) if isinstance(err, dict)
	}
	if codes & COMPARE_MISMATCH_CODES:
		return True
	# Shopify's code names for this have moved between versions; the message is a reliable
	# secondary signal and a false positive costs only one extra call.
	return "compare" in str(exc).lower()


def reference_uri(store: str) -> str:
	"""Identifies ERPNext as the source of the change, visible in Shopify's adjustment log.

	Percent-encoded, because Shopify validates this as a real URI and rejects the whole
	mutation otherwise. Store names routinely contain spaces -- "Acme Apparel EU" -- and an
	unencoded space made every inventory push fail with a message about a reference document,
	which gives no hint that the store's own name is the problem.
	"""
	site = quote(cstr(frappe.local.site), safe="")
	return f"erpnext://{site}/shopify-integration/{quote(cstr(store), safe='')}"


def stamp_synced(link_names: list[str]) -> None:
	"""Record the watermark used by the reconciliation job's delta query."""
	if not link_names:
		return
	now = frappe.utils.now_datetime()
	for name in link_names:
		frappe.db.set_value("Shopify Item Link", name, "inventory_synced_on", now, update_modified=False)
