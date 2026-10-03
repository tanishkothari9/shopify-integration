"""Price sync: ERPNext -> Shopify (spec §9.1, §19 phase 5).

Prices live on variants, and Shopify's ``productUpdate`` cannot change them -- its own
documentation points at ``productVariantsBulkUpdate`` for this. That mutation takes one
product and a list of its variants, so a drain group is grouped by product before sending,
rather than batched as one flat list.

Only price changes on the store's own selling price list are pushed. An ERPNext site
typically carries several price lists -- cost, wholesale, a promotional one -- and pushing
every change would send Shopify a wholesale price the moment someone edited it.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr, flt, nowdate

from shopify_integration.api.client import ShopifyClient, load_query
from shopify_integration.catalogue.echo import is_echo
from shopify_integration.sync.engine import enqueue_sync
from shopify_integration.utils.money import from_document, quantize


def on_item_change(doc, method=None):
	"""doc_event on Item. Queues a price push when the Item itself carries the price.

	`current_price` falls back to the Item's Standard Selling Rate, because that is where
	ERPNext puts a rate typed on the Item form unless the store's own price list happens to be
	the one in Selling Settings. If only Item Price changes queued a push, that rate would be
	readable and never sent: the product sat in Shopify at 0.00 while ERPNext showed 1,200.
	"""
	if is_echo(doc):
		return

	item_code = doc.name
	for store in frappe.get_all(
		# `is_template` excluded: a template's row carries the product but no variant, and a
		# price belongs to a variant. Including it queued a price row on every template save
		# that `push_prices` could only skip.
		"Shopify Item Link",
		filters={"item_code": item_code, "is_template": 0},
		pluck="store",
		distinct=True,
	):
		store_doc = frappe.get_cached_doc("Shopify Store", store)
		if not store_doc.sync_prices:
			continue

		enqueue_sync(
			store,
			"price",
			dedupe_key=f"price:{store}:{item_code}",
			ref_doctype="Item",
			ref_docname=item_code,
		)


def enqueue_price(store: str, item_code: str) -> str | None:
	"""Queue a price push for one item on one store. Honours the store's own switch."""
	store_doc = frappe.get_cached_doc("Shopify Store", store)
	if not store_doc.sync_prices:
		return None

	return enqueue_sync(
		store,
		"price",
		dedupe_key=f"price:{store}:{item_code}",
		ref_doctype="Item",
		ref_docname=item_code,
	)


def on_price_change(doc, method=None):
	"""doc_event on Item Price. Computes a dedupe key and enqueues -- nothing else."""
	if is_echo(doc):
		return
	if not doc.get("selling"):
		# A buying price says nothing about what the item sells for on Shopify.
		return

	item_code = doc.get("item_code")
	if not item_code:
		return

	for store in frappe.get_all(
		# `is_template` excluded: a template's row carries the product but no variant, and a
		# price belongs to a variant. Including it queued a price row on every template save
		# that `push_prices` could only skip.
		"Shopify Item Link",
		filters={"item_code": item_code, "is_template": 0},
		pluck="store",
		distinct=True,
	):
		store_doc = frappe.get_cached_doc("Shopify Store", store)
		if not store_doc.sync_prices:
			continue
		if not _is_stores_price_list(store_doc, doc.get("price_list")):
			continue

		enqueue_sync(
			store,
			"price",
			dedupe_key=f"price:{store}:{item_code}",
			ref_doctype="Item",
			ref_docname=item_code,
		)


def _is_stores_price_list(store_doc, price_list: str | None) -> bool:
	"""Whether this price list is the one the store sells from.

	Falls back to the dedicated list the order builder creates when no list is configured, so
	the two halves of the app agree on which prices are Shopify's.
	"""
	if not price_list:
		return False
	if store_doc.selling_price_list:
		return price_list == store_doc.selling_price_list
	return price_list == f"Shopify - {store_doc.name}"[:140]


def push_prices(store: str, rows: list[dict]) -> None:
	"""Queue handler for the ``price`` operation.

	Reads the current price at drain time, never from the queue payload, so a row that waited
	behind a backlog pushes what the item costs now rather than what it cost when the row was
	written.
	"""
	store_doc = frappe.get_cached_doc("Shopify Store", store)
	client = ShopifyClient.for_store(store)

	by_product: dict[str, list[dict]] = {}
	links_by_product: dict[str, list[str]] = {}

	for row in rows:
		item_code = row.get("ref_docname") or _item_from_key(row.get("dedupe_key"))
		if not item_code:
			continue

		link = frappe.db.get_value(
			"Shopify Item Link",
			{"store": store, "item_code": item_code},
			["name", "product_gid", "variant_gid"],
			as_dict=True,
		)
		if not link or not link.variant_gid or not link.product_gid:
			# Unmapped, usually because a products/delete webhook unlinked it while this row was
			# still pending. Skipped rather than raised: raising here fails every *other* row in
			# the claimed batch too, and the queue's whole promise is that a poison row never
			# blocks the ones behind it.
			frappe.logger("shopify_integration").info(
				f"Skipping {item_code}: no Shopify variant mapping for store {store}"
			)
			continue

		price = current_price(store_doc, item_code)
		if price is None:
			# No price on the store's list. Sending 0 would zero the product on Shopify, which
			# is far worse than leaving the existing price alone.
			continue

		# compareAtPrice is deliberately not sent. It is Shopify's promotional "was" price and
		# merchants set it there; overwriting it from an ERPNext list price would silently
		# clear a running promotion.
		entry = {"id": link.variant_gid, "price": str(quantize(price))}

		by_product.setdefault(link.product_gid, []).append(entry)
		links_by_product.setdefault(link.product_gid, []).append(link.name)

	for product_gid, variants in by_product.items():
		client.execute(
			load_query("product_variants_bulk_update"),
			{"productId": product_gid, "variants": variants},
			cost_hint=10 + len(variants),
		)
		_stamp_synced(links_by_product[product_gid])


def _item_from_key(dedupe_key: str | None) -> str | None:
	"""``price:{store}:{item_code}`` -> item_code."""
	parts = cstr(dedupe_key).split(":")
	return parts[2] if len(parts) >= 3 and parts[0] == "price" else None


def current_price(store_doc, item_code: str):
	"""The item's selling price for this store, or None if it genuinely has none.

	The store's own price list wins. Failing that, the Item's **Standard Selling Rate**, which
	is what ERPNext writes when someone types a rate on the Item form.

	That fallback is not a nicety. ERPNext files the rate typed on the Item form under the
	price list in Selling Settings, which is rarely the one a Shopify store is pointed at. So a
	merchant sets a saree at 1,200, publishes it, and it goes live at 0.00 -- the price is
	there, just on another list, and nothing says so.
	"""
	price_list = store_doc.selling_price_list or f"Shopify - {store_doc.name}"[:140]
	value = _retail_rate(item_code, price_list)
	if value is not None:
		return from_document(value)

	standard = frappe.db.get_value("Item", item_code, "standard_rate")
	if standard and flt(standard) > 0:
		return from_document(flt(standard))

	return None


def _retail_rate(item_code: str, price_list: str):
	"""The one rate a walk-up customer would be charged today, or None.

	ERPNext allows many Item Price rows for the same item and price list, differing by
	validity dates, UOM, minimum quantity or customer. `get_value` returns whichever the
	database hands back first, so a lapsed festival rate, a wholesale break or one
	customer's negotiated price could be published to the storefront as the retail price.

	The conditions mirror ERPNext's own `get_item_price`: no party-specific row, no batch,
	in date, in the stock UOM, and no quantity break (`packing_unit`, which is ERPNext's
	name for it). Ordered newest-valid-first, so of two equally valid rows the later one
	wins -- which is what ERPNext itself does.
	"""
	rows = frappe.db.sql(
		"""
		SELECT price_list_rate
		FROM `tabItem Price`
		WHERE item_code = %(item_code)s
		  AND price_list = %(price_list)s
		  AND selling = 1
		  AND IFNULL(customer, '') = ''
		  AND IFNULL(uom, '') IN ('', IFNULL((SELECT stock_uom FROM `tabItem` WHERE name = %(item_code)s), ''))
		  AND IFNULL(packing_unit, 0) IN (0, 1)
		  AND IFNULL(batch_no, '') = ''
		  AND IFNULL(valid_from, '2000-01-01') <= %(today)s
		  AND IFNULL(valid_upto, '2500-12-31') >= %(today)s
		ORDER BY IFNULL(valid_from, '2000-01-01') DESC, IFNULL(uom, '') DESC, modified DESC
		LIMIT 1
		""",
		{"item_code": item_code, "price_list": price_list, "today": nowdate()},
	)
	if not rows or rows[0][0] in (None, ""):
		return None
	return flt(rows[0][0])


def _stamp_synced(link_names: list[str]) -> None:
	now = frappe.utils.now_datetime()
	for name in link_names:
		frappe.db.set_value("Shopify Item Link", name, "price_synced_on", now, update_modified=False)
