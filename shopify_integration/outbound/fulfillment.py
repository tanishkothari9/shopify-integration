"""Fulfilment: ERPNext -> Shopify.

The only part of the integration that used to run one way. Orders, stock, prices and products
all sync both directions; shipping did not, so a shop packing its orders in ERPNext still had
to go and tick them off in Shopify by hand, and the customer's dispatch email never went out.

Two moments, because ERPNext records them separately and so does Shopify:

* a **Delivery Note** says *what shipped*, and creates the fulfilment
* a **Shipment** says *who is carrying it and under what number*, and attaches the tracking

Either can happen without the other. A shop that never uses Shipment still gets its orders
marked fulfilled; a Shipment raised for a Delivery Note that has not reached Shopify yet
creates the fulfilment and attaches the tracking in one pass.

Shopify has not let anyone fulfil an order directly for years. You fulfil against a
*FulfillmentOrder* -- Shopify's own split of the order by location and status -- and the
quantities available to fulfil on it are what Shopify still considers owed. That is what makes
partial delivery work without any bookkeeping of our own: ask Shopify what is left, ship no
more than that.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint, cstr

from shopify_integration.api.client import ShopifyClient, load_query
from shopify_integration.catalogue.echo import is_echo
from shopify_integration.sync.engine import enqueue_sync

#: Fulfilment orders Shopify will still accept work against.
OPEN_STATUSES = {"OPEN", "IN_PROGRESS", "SCHEDULED"}


def on_delivery_note_submit(doc, method=None) -> None:
	"""doc_event on Delivery Note. Computes a dedupe key and enqueues -- nothing else."""
	_enqueue_for_delivery_note(doc.name, doc)


def on_shipment_submit(doc, method=None) -> None:
	"""doc_event on Shipment: carry its tracking to every Delivery Note it covers.

	A Shipment can cover several Delivery Notes, and they can belong to different Shopify
	orders, so each is enqueued on its own.
	"""
	for row in doc.get("shipment_delivery_note") or []:
		if row.get("delivery_note"):
			_enqueue_for_delivery_note(row.delivery_note)


def shopify_order_for(doc) -> tuple[str | None, str | None]:
	"""``(store, order_gid)`` behind a Delivery Note, from the note or the order under it.

	A note this app created carries both fields itself. One a user made the ordinary way --
	from the Sales Order, in the UI -- carries neither, because `shopify_order_gid` is
	``no_copy`` and ERPNext's mapper would not copy it regardless. Reading only the note was the
	first version of this, and it fulfilled nothing at all: every shipment a merchant actually
	makes takes that second path.
	"""
	store, order_gid = doc.get("shopify_store"), doc.get("shopify_order_gid")
	if store and order_gid:
		return store, order_gid

	for item in doc.get("items") or []:
		sales_order = item.get("against_sales_order")
		if not sales_order:
			continue
		row = frappe.db.get_value(
			"Sales Order", sales_order, ["shopify_store", "shopify_order_gid"], as_dict=True
		)
		if row and row.shopify_store and row.shopify_order_gid:
			return row.shopify_store, row.shopify_order_gid

	return None, None


def _enqueue_for_delivery_note(name: str, doc=None) -> None:
	doc = doc or frappe.get_doc("Delivery Note", name)
	if is_echo(doc):
		return

	store, order_gid = shopify_order_for(doc)
	if not store or not order_gid:
		# Not a Shopify shipment. Most Delivery Notes on a busy site are not.
		return

	store_doc = frappe.get_cached_doc("Shopify Store", store)
	if not store_doc.get("sync_fulfillments"):
		return

	enqueue_sync(
		store,
		"fulfillment",
		dedupe_key=f"fulfillment:{store}:{name}",
		ref_doctype="Delivery Note",
		ref_docname=name,
	)


def push_fulfillments(store: str, rows: list[dict]) -> None:
	"""Queue handler for the ``fulfillment`` operation.

	Idempotent by construction, which is what lets the Delivery Note and the Shipment enqueue
	the same key: a note Shopify has not heard of is fulfilled, and one it has already only has
	its tracking refreshed.
	"""
	store_doc = frappe.get_cached_doc("Shopify Store", store)
	client = ShopifyClient.for_store(store)

	for row in rows:
		name = row.get("ref_docname")
		if not name or not frappe.db.exists("Delivery Note", name):
			continue

		note = frappe.get_doc("Delivery Note", name)
		if note.docstatus != 1:
			# Cancelled between enqueue and drain. Dropping it is correct: cancelling a shipment
			# is not a reason to tell Shopify the goods went out.
			continue

		_, order_gid = shopify_order_for(note)
		if not order_gid:
			continue

		tracking = tracking_for(name)
		existing = note.get("shopify_fulfillment_gid")

		if existing:
			if tracking:
				_update_tracking(client, store_doc, existing, tracking)
			continue

		gid = _create_fulfilment(client, store_doc, name, order_gid, tracking)
		if gid:
			# Stamped onto the note so the link shows on the document, and so a Shipment raised
			# later finds this fulfilment instead of creating a second one.
			frappe.db.set_value(
				"Delivery Note",
				name,
				{
					"shopify_fulfillment_gid": gid,
					"shopify_store": store,
					"shopify_order_gid": order_gid,
				},
				update_modified=False,
			)

	frappe.db.commit()


def tracking_for(delivery_note: str) -> dict | None:
	"""Carrier and tracking number from the Shipment covering this note, if there is one.

	Returns None rather than an empty dict when nothing is known, so callers can tell "no
	tracking yet" from "tracking that happens to be blank" -- sending the latter to Shopify
	would wipe tracking a merchant had entered there by hand.
	"""
	if not frappe.db.table_exists("Shipment Delivery Note"):
		# ERPNext ships Shipment, but a site can have it disabled or an older version may not
		# carry it at all. Its absence is not an error; there is simply no tracking.
		return None

	parents = frappe.get_all(
		"Shipment Delivery Note",
		filters={"delivery_note": delivery_note, "parenttype": "Shipment"},
		pluck="parent",
	)
	for parent in parents:
		shipment = frappe.db.get_value(
			"Shipment", parent, ["docstatus", "carrier", "awb_number", "tracking_url"], as_dict=True
		)
		if not shipment or shipment.docstatus != 1:
			continue

		info = {}
		if cstr(shipment.awb_number).strip():
			info["number"] = cstr(shipment.awb_number).strip()
		if cstr(shipment.carrier).strip():
			info["company"] = cstr(shipment.carrier).strip()
		if cstr(shipment.tracking_url).strip():
			info["url"] = cstr(shipment.tracking_url).strip()
		if info:
			return info

	return None


def _shipped_quantities(delivery_note: str, store: str) -> dict[str, int]:
	"""Shopify variant GID -> quantity this Delivery Note shipped.

	Keyed on the variant rather than the SKU because a SKU is only unique by convention, and a
	shop that reuses one across variants would otherwise have its quantities merged.
	"""
	shipped: dict[str, int] = {}
	for item in frappe.get_all(
		"Delivery Note Item",
		filters={"parent": delivery_note, "parenttype": "Delivery Note"},
		fields=["item_code", "qty"],
	):
		variant = frappe.db.get_value(
			"Shopify Item Link", {"store": store, "item_code": item.item_code}, "variant_gid"
		)
		if not variant:
			continue
		shipped[variant] = shipped.get(variant, 0) + cint(item.qty)
	return shipped


def _line_items_to_fulfil(client: ShopifyClient, order_gid: str, shipped: dict[str, int]) -> list[dict]:
	"""Match what the note shipped against what Shopify still considers owed.

	Never asks Shopify to fulfil more than it says remains: a Delivery Note raised for three
	units against an order Shopify has already seen two of would otherwise be refused outright,
	losing the one unit that was genuinely new.
	"""
	data = client.execute(load_query("fulfillment_orders"), {"id": order_gid}, cost_hint=15)
	order = data.get("order") or {}

	remaining = dict(shipped)
	grouped = []

	for node in (order.get("fulfillmentOrders") or {}).get("nodes") or []:
		if cstr(node.get("status")).upper() not in OPEN_STATUSES:
			continue

		lines = []
		for line in (node.get("lineItems") or {}).get("nodes") or []:
			variant_gid = _variant_of(line)
			want = remaining.get(variant_gid, 0)
			if want <= 0:
				continue

			take = min(want, cint(line.get("remainingQuantity")))
			if take <= 0:
				continue

			lines.append({"id": line["id"], "quantity": take})
			remaining[variant_gid] = want - take

		if lines:
			grouped.append({"fulfillmentOrderId": node["id"], "fulfillmentOrderLineItems": lines})

	return grouped


def _variant_of(line: dict) -> str | None:
	"""The variant GID behind a fulfilment order line.

	Shopify exposes the variant on the order's line item, not on the fulfilment order's, which
	is why this reaches through rather than reading it directly.
	"""
	item = line.get("lineItem") or {}
	return item.get("variant", {}).get("id") if item.get("variant") else item.get("id")


def _create_fulfilment(
	client: ShopifyClient, store_doc, delivery_note: str, order_gid: str, tracking: dict | None
) -> str | None:
	shipped = _shipped_quantities(delivery_note, store_doc.name)
	if not shipped:
		# Nothing on the note maps to a Shopify variant -- a shipping charge booked as an item,
		# or a note that has drifted from its order. Silently fulfilling nothing is right.
		return None

	grouped = _line_items_to_fulfil(client, order_gid, shipped)
	if not grouped:
		# Shopify already considers these lines fulfilled, usually because the merchant ticked
		# them off there first. Not an error, and not something to retry.
		return None

	payload = {
		"lineItemsByFulfillmentOrder": grouped,
		"notifyCustomer": bool(store_doc.get("notify_customer_on_fulfillment")),
	}
	if tracking:
		payload["trackingInfo"] = tracking

	data = client.execute(
		load_query("fulfillment_create"), {"fulfillment": payload}, cost_hint=10 + len(grouped)
	)
	fulfilment = (data.get("fulfillmentCreate") or {}).get("fulfillment") or {}
	return fulfilment.get("id")


def _update_tracking(client: ShopifyClient, store_doc, fulfilment_gid: str, tracking: dict) -> None:
	client.execute(
		load_query("fulfillment_tracking_update"),
		{
			"fulfillmentId": fulfilment_gid,
			"trackingInfoInput": tracking,
			"notifyCustomer": bool(store_doc.get("notify_customer_on_fulfillment")),
		},
		cost_hint=10,
	)
