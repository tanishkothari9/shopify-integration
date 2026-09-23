"""Outbound product sync: ERPNext Item -> Shopify (spec §9.1).

Two jobs. It updates products that are already linked, and it creates Shopify products for
items ticked **Publish to Shopify** on a store set to **Publish New Items**. Both switches
have to be on: an ERPNext catalogue is mostly raw materials, packaging and internal parts,
and a single careless default would put all of it in a live shop.

Creation handles both shapes. A plain item becomes a product with one variant. A template
item -- `has_variants`, with an attribute table -- becomes a product whose options are built
from the attributes its variants actually use, and one Shopify variant per ERPNext variant.

The options come from the variants that exist, never from the Item Attribute's full value
list: an attribute may hold every size a business has ever stocked, and a shop offering two
of them should show two.

The hook here is the other half of echo suppression: it is what would loop forever if the
inbound writer did not mark its saves.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr

from shopify_integration.api.client import ShopifyClient, load_query
from shopify_integration.catalogue.echo import is_echo
from shopify_integration.shopify_integration.doctype.shopify_item_link.shopify_item_link import (
	linked_stores,
)
from shopify_integration.sync.engine import enqueue_sync

#: Variants per productVariantsBulkCreate call. Shopify caps a product at 100 variants, and
#: smaller batches keep one rejected value from costing the whole product.
VARIANT_BATCH = 25


def on_item_change(doc, method=None):
	"""doc_event on Item. Computes a dedupe key and enqueues -- nothing else.

	Runs inside the user's save transaction, so it must stay cheap and must never touch the
	network (spec §7.1).
	"""
	if is_echo(doc):
		# Written by an inbound handler. Pushing it back is the infinite loop.
		return

	if doc.get("has_variants") and not doc.get("publish_to_shopify"):
		# A template is not sellable and has no Shopify variant of its own, so there is nothing
		# to update. It does reach the queue when it is asked to be published, because that is
		# the document carrying the options the whole product is built from.
		return

	for store in _stores_for(doc):
		enqueue_sync(
			store,
			"product",
			dedupe_key=f"product:{store}:{doc.name}",
			ref_doctype="Item",
			ref_docname=doc.name,
		)

	_republish_template_if_waiting(doc)


def _republish_template_if_waiting(doc) -> None:
	"""A variant whose template is meant to be published, but is not yet, re-queues the template.

	A template on its own has nothing to sell, so `_create_product` declines it and the queue
	row is marked Done. That is correct -- but it means the publish decision is spent. Saving
	the template first and adding sizes afterwards is exactly how anyone works in the UI, and
	a worker draining between the two steps consumed the only attempt.

	The variants cannot rescue it themselves: they carry no `publish_to_shopify` of their own,
	and `_stores_for` finds a store through the template only once the template *has* a link.
	Before the first publish there is none, so nothing fires and the product never appears.

	So a variant of an unpublished, publish-marked template queues the template again. The
	dedupe key is the template's, so six sizes arriving together coalesce into one push.
	"""
	template = doc.get("variant_of")
	if not template or doc.get("has_variants"):
		return
	if not frappe.db.get_value("Item", template, "publish_to_shopify"):
		return

	for store in frappe.get_all(
		"Shopify Store",
		filters={"enabled": 1, "sync_items": 1, "publish_new_items": 1},
		pluck="name",
	):
		already = frappe.db.exists(
			"Shopify Item Link", {"store": store, "template_item": template, "product_gid": ("is", "set")}
		)
		if already:
			continue
		enqueue_sync(
			store,
			"product",
			dedupe_key=f"product:{store}:{template}",
			ref_doctype="Item",
			ref_docname=template,
		)


def _stores_for(doc) -> list[str]:
	"""Stores that should hear about this item: the ones it is on, plus the ones it is for.

	An item already linked goes to those stores. An item ticked Publish to Shopify and not yet
	on a store goes to every enabled store that accepts new items -- which is how a product
	created in ERPNext reaches a shop at all.
	"""
	stores = [
		store
		for store in linked_stores(doc.name)
		if frappe.db.get_value("Shopify Store", store, "sync_items")
	]

	# A new variant of a published range inherits the decision. Nobody ticks a box per size:
	# the template already said this product belongs in the shop, and a size added to it is
	# part of that product. Without this a new variant sits unlinked for ever -- the template
	# is already published, so nothing takes the create path again.
	template = doc.get("variant_of")
	if template:
		for store in frappe.get_all(
			"Shopify Item Link",
			filters={"template_item": template, "product_gid": ["is", "set"]},
			pluck="store",
			distinct=True,
		):
			if store not in stores and frappe.db.get_value("Shopify Store", store, "sync_items"):
				stores.append(store)

	if not doc.get("publish_to_shopify"):
		return stores

	for store in frappe.get_all(
		"Shopify Store",
		filters={"enabled": 1, "sync_items": 1, "publish_new_items": 1},
		pluck="name",
	):
		if store not in stores:
			stores.append(store)

	return stores


def push_products(store: str, rows: list[dict]) -> None:
	"""Queue handler for the ``product`` operation.

	Reads current state at drain time rather than trusting the queue payload, so a row that
	waited through a backlog pushes what the item looks like now.
	"""
	client = ShopifyClient.for_store(store)

	for row in rows:
		item_code = row.get("ref_docname")
		if not item_code or not frappe.db.exists("Item", item_code):
			continue

		link = frappe.db.get_value(
			"Shopify Item Link",
			{"store": store, "item_code": item_code},
			["product_gid", "is_variant", "template_item"],
			as_dict=True,
		)
		if not link or not link.product_gid:
			if _attach_to_published_template(client, store, item_code):
				continue

			if _should_publish(store, item_code):
				_create_product(client, store, item_code)
				continue

			# Unmapped and not meant for this shop. Usually a products/delete webhook unlinked
			# it while this row was still pending. Skipped rather than raised: raising here
			# fails every *other* row in the claimed batch too, and the queue's whole promise
			# is that a poison row never blocks the ones behind it.
			frappe.logger("shopify_integration").info(
				f"Skipping {item_code}: no Shopify product mapping for store {store}"
			)
			continue

		item = frappe.get_doc("Item", item_code)

		# Only what ERPNext actually owns. Shopify holds the storefront copy: the title a
		# customer reads and the description someone wrote for the product page. Sending
		# ERPNext's item_name and description on every save destroyed both -- a merchant
		# adjusting an item's *weight* replaced "Banarasi Silk Saree, Festive Edition" with
		# the plain item name, and wiped the marketing HTML under it.
		#
		# Whether the item is sellable is ERPNext's to say, so status still goes.
		payload = {
			"id": link.product_gid,
			"status": "ARCHIVED" if item.disabled else "ACTIVE",
		}

		if frappe.db.get_value("Shopify Store", store, "sync_item_titles"):
			payload["title"] = cstr(item.item_name)[:255]
			payload["descriptionHtml"] = cstr(item.description or "")

		client.execute(load_query("product_update"), {"product": payload}, cost_hint=10)


# --------------------------------------------------------------------------------------
# Creating a Shopify product from an ERPNext item
# --------------------------------------------------------------------------------------


def _should_publish(store: str, item_code: str) -> bool:
	"""Both switches on, and the item actually sellable."""
	if not frappe.db.get_value("Item", item_code, "publish_to_shopify"):
		return False
	if not frappe.db.get_value("Shopify Store", store, "publish_new_items"):
		return False
	return True


def _create_product(client: ShopifyClient, store: str, item_code: str) -> str | None:
	"""Create the Shopify product for an ERPNext item, and link the two.

	Two calls, because Shopify splits them: ``productCreate`` makes the product and its one
	default variant, and only ``productVariantsBulkUpdate`` can give that variant a SKU, a
	price and inventory tracking. Without the second call the product exists but cannot be
	sold or counted.
	"""
	item = frappe.get_doc("Item", item_code)
	store_doc = frappe.get_cached_doc("Shopify Store", store)

	children = _variants_of(item_code) if item.get("has_variants") else []
	if item.get("has_variants") and not children:
		frappe.logger("shopify_integration").info(
			f"Not publishing {item_code}: a template with no variants has nothing to sell."
		)
		return None

	options = _options_from(children) if children else []

	payload = {
		"title": cstr(item.item_name or item_code)[:255],
		"descriptionHtml": cstr(item.description or ""),
		"productType": cstr(item.item_group or "")[:255],
		"vendor": cstr(item.get("brand") or "")[:255] or None,
		"status": "ARCHIVED" if item.disabled else "ACTIVE",
	}
	if options:
		payload["productOptions"] = options

	data = client.execute(load_query("product_create"), {"product": payload}, cost_hint=15)
	product = (data.get("productCreate") or {}).get("product") or {}
	if not product.get("id"):
		return None

	if children:
		_create_variants(client, product["id"], children, store_doc, first=True)
	else:
		variants = (product.get("variants") or {}).get("nodes") or []
		if variants:
			_fill_variant(client, product["id"], variants[0]["id"], item, store_doc)

	# Re-read: SKUs, prices and inventory items only exist after the second call, and a link
	# without the inventory item id cannot push stock.
	product = _reread(client, product["id"]) or product

	if children:
		_link_variants(store, item_code, product, children)
	else:
		_link(store, item_code, product)

	frappe.logger("shopify_integration").info(
		f"Published {item_code} to {store} as {product['id']}"
		+ (f" with {len(children)} variants" if children else "")
	)
	return product["id"]


def _fill_variant(client: ShopifyClient, product_gid: str, variant_gid: str, item, store_doc) -> None:
	variant: dict = {
		"id": variant_gid,
		"inventoryItem": {"sku": cstr(item.item_code)[:255], "tracked": bool(item.is_stock_item)},
	}

	price = _selling_price(item.item_code, store_doc)
	if price is not None:
		variant["price"] = str(price)

	client.execute(
		load_query("product_variants_bulk_update"),
		{"productId": product_gid, "variants": [variant]},
		cost_hint=15,
	)


def _selling_price(item_code: str, store_doc):
	"""The item's price on the store's own list, or None to let Shopify keep its default.

	None rather than zero: a product listed at nothing is worse than one a merchant has yet
	to price.
	"""
	from shopify_integration.outbound.price import current_price

	return current_price(store_doc, item_code)


def _reread(client: ShopifyClient, product_gid: str) -> dict | None:
	data = client.execute(load_query("product_by_id"), {"id": product_gid}, cost_hint=10)
	product = data.get("product")
	if not product:
		return None

	product["variants"] = [edge["node"] for edge in ((product.get("variants") or {}).get("edges") or [])]
	return product


def _link(store: str, item_code: str, product: dict) -> None:
	from shopify_integration.catalogue.mapping import upsert_link

	variants = product.get("variants") or []
	variant = variants[0] if variants else {}
	upsert_link(store, item_code=item_code, product=product, variant=variant, is_variant=False)


# --------------------------------------------------------------------------------------
# Templates and their variants
# --------------------------------------------------------------------------------------


def _variants_of(template: str) -> list[dict]:
	"""Every sellable variant of a template, each with the attribute values that define it.

	Ordered by item code so the Shopify variants land in a stable order, and so re-running
	against the same template produces the same product twice.
	"""
	children = []
	for code in frappe.get_all(
		"Item",
		filters={"variant_of": template, "disabled": 0},
		order_by="item_code",
		pluck="name",
	):
		values = frappe.get_all(
			"Item Variant Attribute",
			filters={"parent": code, "parenttype": "Item"},
			fields=["attribute", "attribute_value"],
			order_by="idx",
		)
		if not values:
			# A variant with no attribute values cannot be placed on any option, and Shopify
			# would reject the whole batch for it. Skipping one is better than losing all.
			frappe.logger("shopify_integration").info(
				f"Skipping variant {code}: it declares no attribute values."
			)
			continue
		children.append({"item_code": code, "values": values})
	return children


def _options_from(children: list[dict]) -> list[dict]:
	"""Shopify product options, built from the values the variants actually use.

	Not from the Item Attribute's own list: an attribute may hold every size a business has
	ever stocked, and a shop selling two of them should offer two.
	"""
	seen: dict[str, list[str]] = {}
	for child in children:
		for row in child["values"]:
			name = cstr(row.attribute).strip()
			value = cstr(row.attribute_value).strip()
			if not name or not value:
				continue
			values = seen.setdefault(name, [])
			if value not in values:
				values.append(value)

	return [
		{"name": name, "position": index + 1, "values": [{"name": v} for v in values]}
		for index, (name, values) in enumerate(seen.items())
	]


def _create_variants(
	client: ShopifyClient, product_gid: str, children: list[dict], store_doc, first: bool
) -> None:
	"""Create one Shopify variant per ERPNext variant, in batches Shopify will accept."""
	payload = []
	for child in children:
		variant: dict = {
			"optionValues": [
				{"optionName": cstr(row.attribute).strip(), "name": cstr(row.attribute_value).strip()}
				for row in child["values"]
				if cstr(row.attribute).strip() and cstr(row.attribute_value).strip()
			],
			"inventoryItem": {
				"sku": cstr(child["item_code"])[:255],
				"tracked": bool(frappe.db.get_value("Item", child["item_code"], "is_stock_item")),
			},
		}
		price = _selling_price(child["item_code"], store_doc)
		if price is not None:
			variant["price"] = str(price)
		payload.append(variant)

	for start in range(0, len(payload), VARIANT_BATCH):
		batch = payload[start : start + VARIANT_BATCH]
		client.execute(
			load_query("product_variants_bulk_create"),
			{
				"productId": product_gid,
				"variants": batch,
				# Only the first call may remove the placeholder variant productCreate leaves
				# behind; Shopify refuses the strategy once real variants exist.
				"strategy": "REMOVE_STANDALONE_VARIANT" if (first and start == 0) else "DEFAULT",
			},
			cost_hint=15 + len(batch),
		)


def _link_variants(store: str, template: str, product: dict, children: list[dict]) -> None:
	"""Link each ERPNext variant to its Shopify variant.

	The template gets no link of its own -- it has no Shopify variant to point at. Each child
	records `template_item` instead, which is how the product is found again later.

	Matched on SKU, which is the item code we just sent. Position is not safe to match on:
	Shopify orders variants by its own option positions, not by the order they were created.
	"""
	from shopify_integration.catalogue.mapping import upsert_link

	by_sku = {cstr(v.get("sku")): v for v in (product.get("variants") or []) if v.get("sku")}

	for child in children:
		variant = by_sku.get(child["item_code"])
		if not variant:
			frappe.logger("shopify_integration").info(
				f"Shopify returned no variant for {child['item_code']}; it will be linked on the "
				"next product sync."
			)
			continue
		upsert_link(
			store,
			item_code=child["item_code"],
			product=product,
			variant=variant,
			is_variant=True,
			template_item=template,
		)


def _attach_to_published_template(client: ShopifyClient, store: str, item_code: str) -> bool:
	"""Add a newly created ERPNext variant to a product that is already on Shopify.

	A merchant adding a size to an existing range expects it to appear in the shop. Without
	this the variant sits unlinked for ever: the template is already published, so nothing
	takes the create path again, and nothing else would ever notice the new child.

	Returns True when it handled the item, so the caller stops.
	"""
	template = frappe.db.get_value("Item", item_code, "variant_of")
	if not template:
		return False

	# Found through a sibling, not through the template. A template carries no link of its own --
	# it has no Shopify variant to point at -- so each variant records `template_item` instead.
	# Looking for a link on the template finds nothing, ever, and a newly added size would sit
	# unlinked for good.
	parent = frappe.db.get_value(
		"Shopify Item Link",
		{"store": store, "template_item": template, "product_gid": ["is", "set"]},
		"product_gid",
	)
	if not parent:
		return False

	child = next((c for c in _variants_of(template) if c["item_code"] == item_code), None)
	if not child:
		return False

	store_doc = frappe.get_cached_doc("Shopify Store", store)
	_create_variants(client, parent, [child], store_doc, first=False)

	product = _reread(client, parent)
	if product:
		_link_variants(store, template, product, [child])

	frappe.logger("shopify_integration").info(f"Added {item_code} to the Shopify product for {template}")
	return True
