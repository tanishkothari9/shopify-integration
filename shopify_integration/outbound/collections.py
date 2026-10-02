"""Put each product in the Shopify collection whose tax override matches ERPNext's.

Shopify charges tax by collection override; ERPNext decides tax by Item Tax Template, which
on an Indian catalogue often depends on the *price* -- a kurti at 2,450 is 5% and the same
kurti at 2,550 is 18%. Nothing joins those two facts, so a shop with rate bands charges one
tax in its books and another at the checkout.

This joins them. The merchant maps template -> collection on the store, and a product is kept
in exactly the one collection that matches what ERPNext would book for it today.

Three rules decide everything here:

* **An empty map means off.** A store that has not filled the table in is never touched, so
  this cannot change behaviour for anyone who has not asked for it.
* **Only mapped collections are touched.** A product is added to its match and removed from
  the *other mapped* ones. A collection the merchant has not listed is theirs, and is left
  exactly as it is -- seasonal collections, manual curation, none of it is ours to edit.
* **A template mapping to nothing is an answer, not a gap.** The store's default rate
  usually needs no override at all, so "no collection" means remove it from the mapped ones
  and let Shopify's own default apply.

Membership is changed from the product's side, with ``collectionsToJoin`` and
``collectionsToLeave`` on ``productUpdate``. ``collectionAddProductsV2`` and
``collectionRemoveProducts`` do not exist in the pinned API version, and ``collectionUpdate``
takes a product list that *replaces* a collection's membership -- using that to move one
product in would throw every other product out.
"""

from __future__ import annotations

import json

import frappe
from frappe.utils import cstr, flt

from shopify_integration.api.client import ShopifyClient, load_query
from shopify_integration.exceptions import PartialFailure
from shopify_integration.outbound.price import current_price

#: Products per membership mutation. Shopify accepts more, but a smaller call keeps one
#: failure's blast radius to a handful of products.
BATCH = 50


def tax_collection_map(store_doc) -> dict[str, str]:
	"""Item Tax Template -> collection GID, as the merchant configured it."""
	return {
		cstr(row.item_tax_template): cstr(row.collection_gid).strip()
		for row in (store_doc.get("tax_collection_map") or [])
		if cstr(row.item_tax_template) and cstr(row.collection_gid).strip()
	}


def resolve_item_tax_template(store_doc, item_code: str) -> str | None:
	"""The Item Tax Template ERPNext would book for this item, at its price today.

	ERPNext's own resolver, not a reimplementation of it: the item's own tax rows first, then
	up the Item Group tree, honouring valid_from and the min/max net rate bands. Getting a
	different answer here than the Sales Order will get is the one failure mode that would
	make this worse than doing nothing.

	The band is compared against the selling price as the price list holds it. Verified
	against ERPNext v15 with a tax-inclusive list: 2,450 resolves to the 5% template and
	2,550 to the 18% one, which is the gross figure, not a net one.
	"""
	from erpnext.stock.get_item_details import get_item_tax_template

	if not frappe.db.exists("Item", item_code):
		return None

	price = current_price(store_doc, item_code)
	args = {
		"item_code": item_code,
		"company": store_doc.company,
		"posting_date": frappe.utils.nowdate(),
		"transaction_date": frappe.utils.nowdate(),
		"base_net_rate": flt(price) if price is not None else 0,
		"tax_category": "",
	}
	return get_item_tax_template(args, frappe.get_cached_doc("Item", item_code), {})


def target_collection(store_doc, item_code: str) -> tuple[str | None, str | None]:
	"""The collection this product belongs in, and a warning if that cannot be decided.

	For a template the collection is a property of the *product*, and a Shopify product has
	one membership for all its variants. So every variant has to resolve to the same tax
	template. When they do not, Shopify cannot charge both rates whatever we do, and quietly
	picking one would mean charging some customers the wrong tax -- so nothing is changed and
	the clash is reported.
	"""
	mapping = tax_collection_map(store_doc)
	if not mapping:
		return None, None

	children = _variant_codes(item_code)
	if children:
		resolved = {code: resolve_item_tax_template(store_doc, code) for code in children}
		distinct = {template for template in resolved.values() if template}
		if len(distinct) > 1:
			return None, _mixed_variants_warning(item_code, resolved)
		template = next(iter(distinct), None)
	else:
		template = resolve_item_tax_template(store_doc, item_code)

	return (mapping.get(template) if template else None), None


def _variant_codes(item_code: str) -> list[str]:
	if not frappe.db.get_value("Item", item_code, "has_variants"):
		return []
	return frappe.get_all(
		"Item", filters={"variant_of": item_code, "disabled": 0}, pluck="name", order_by="name"
	)


def _mixed_variants_warning(item_code: str, resolved: dict[str, str | None]) -> str:
	bands = ", ".join(f"{code} -> {template or 'no template'}" for code, template in sorted(resolved.items()))
	return (
		f"Variants of {item_code} fall in different tax bands, so Shopify cannot charge both: "
		f"{bands}. Its collection has been left alone. Split the product, or align the prices "
		"so every variant lands in the same band."
	)


def enqueue_for_item(store: str, item_code: str, *, sweep: bool = False) -> str | None:
	"""Queue a collection check for one item. Cheap, and safe inside a user's save.

	`sweep` marks a row that may wait: a group-wide re-check queues thousands of these at
	once, and they must not sit in front of the stock figure for something a customer is
	buying right now.
	"""
	from shopify_integration.sync.engine import SWEEP_PRIORITY, enqueue_sync

	store_doc = frappe.get_cached_doc("Shopify Store", store)
	if not tax_collection_map(store_doc):
		return None

	return enqueue_sync(
		store,
		"collection",
		dedupe_key=f"collection:{store}:{item_code}",
		ref_doctype="Item",
		ref_docname=item_code,
		payload={"item_code": item_code},
		priority=SWEEP_PRIORITY if sweep else None,
	)


def on_item_change(doc, method=None):
	"""Item saved: its group or its own tax rows may have moved it to another band."""
	_enqueue_for_stores(doc.name)


def on_price_change(doc, method=None):
	"""Item Price saved: the price is what decides the band."""
	_enqueue_for_stores(doc.get("item_code"), price_list=doc.get("price_list"))


def on_item_group_change(doc, method=None):
	"""An Item Group's tax rules changed, so items under it may have moved band.

	Everything expensive happens in a background job. This runs inside somebody's save, and
	an Item Group near the root of a real catalogue covers an enormous number of items --
	SAREE has 39,384 and the root 77,667. Walking them here at two or three queries each was
	something like a hundred thousand queries in one request, which does not slow the save
	down so much as end it.

	So the save does three cheap things and stops: is anybody using this feature, did the
	taxes actually change, and if so hand the group to a worker.
	"""
	if not _any_store_maps_collections():
		return

	if not _taxes_changed(doc):
		# Item Groups are saved for all sorts of reasons -- a rename, a parent move, a
		# description. Only a change to the tax rules can move an item between bands.
		return

	frappe.enqueue(
		"shopify_integration.outbound.collections.recheck_item_group",
		queue="long",
		job_id=f"shopify_tax_collections::{doc.name}",
		deduplicate=True,
		enqueue_after_commit=True,
		item_group=doc.name,
	)


def _any_store_maps_collections() -> bool:
	"""Whether this feature is switched on for a store that is actually running.

	One query, and on a site that does not use this it is the only one the hook costs. A
	disabled store does not count: its map may be half-built or left over from a shop that
	has moved on, and nothing is pushed to it either way.
	"""
	return bool(
		frappe.db.sql(
			"""
			SELECT 1
			FROM `tabShopify Tax Collection` row
			JOIN `tabShopify Store` store
			  ON store.name = row.parent
			 AND row.parenttype = 'Shopify Store'
			 AND IFNULL(store.enabled, 0) = 1
			WHERE IFNULL(row.collection_gid, '') != ''
			LIMIT 1
			"""
		)
	)


def _taxes_changed(doc) -> bool:
	"""Whether the save actually altered the group's tax rules.

	Compared in memory against the version before the save, so it costs nothing. A new group
	with rules counts; one saved without a previous version to compare cannot be ruled out,
	so it is treated as changed.
	"""
	before = doc.get_doc_before_save()
	if before is None:
		return bool(doc.get("taxes"))

	def rules(source):
		return [
			(
				cstr(row.item_tax_template),
				flt(row.minimum_net_rate),
				flt(row.maximum_net_rate),
				cstr(row.get("valid_from")),
				cstr(row.get("tax_category")),
			)
			for row in (source.get("taxes") or [])
		]

	return rules(doc) != rules(before)


def recheck_item_group(item_group: str) -> dict:
	"""Re-check every *published* item under a group, in batches. Runs in a worker.

	Only published items, found by joining to the links rather than by listing the group's
	items and asking about each: on a catalogue of 77,667 items under the root, perhaps a
	few hundred are on Shopify, and the other seventy-seven thousand must not cost a query
	apiece to discard.
	"""
	if not _any_store_maps_collections():
		return {"skipped": "no store maps collections"}

	groups = [item_group, *_descendant_groups(item_group)]
	published = frappe.db.sql(
		"""
		SELECT DISTINCT link.store, COALESCE(NULLIF(link.template_item, ''), link.item_code) AS item_code
		FROM `tabShopify Item Link` link
		JOIN `tabItem` item ON item.name = link.item_code
		WHERE item.item_group IN %(groups)s
		  AND IFNULL(item.disabled, 0) = 0
		  AND IFNULL(link.product_gid, '') != ''
		""",
		{"groups": tuple(groups)},
		as_dict=True,
	)

	queued = 0
	for row in published:
		if enqueue_for_item(row.store, row.item_code, sweep=True):
			queued += 1

	if queued:
		frappe.db.commit()
		frappe.logger("shopify_integration").info(
			f"Item Group {item_group}: queued {queued} product(s) for a tax collection re-check"
		)
	return {"checked": len(published), "queued": queued}


def _descendant_groups(group: str) -> list[str]:
	lft, rgt = frappe.db.get_value("Item Group", group, ["lft", "rgt"]) or (None, None)
	if lft is None:
		return []
	return frappe.get_all("Item Group", filters={"lft": [">", lft], "rgt": ["<", rgt]}, pluck="name")


def _enqueue_for_stores(item_code: str | None, price_list: str | None = None) -> None:
	"""Queue a check on every store that sells this item and has a map."""
	if not item_code:
		return

	for store in frappe.get_all(
		"Shopify Item Link", filters={"item_code": item_code}, pluck="store", distinct=True
	):
		store_doc = frappe.get_cached_doc("Shopify Store", store)
		if price_list and cstr(price_list) != cstr(store_doc.selling_price_list or f"Shopify - {store}"):
			continue
		enqueue_for_item(store, item_code)

	# A variant's price decides its template, but the collection belongs to the template's
	# product, so the template is what has to be re-checked.
	template = frappe.db.get_value("Item", item_code, "variant_of")
	if template:
		for store in frappe.get_all(
			"Shopify Item Link",
			filters={"template_item": template},
			pluck="store",
			distinct=True,
		):
			enqueue_for_item(store, template)


def push_collections(store: str, rows: list[dict]) -> None:
	"""Queue handler for the ``collection`` operation."""
	store_doc = frappe.get_cached_doc("Shopify Store", store)
	if not tax_collection_map(store_doc):
		return

	client = ShopifyClient.for_store(store)
	failures: dict[str, Exception] = {}

	for row in rows:
		try:
			item_code = _item_from(row)
			if item_code:
				sync_item_collections(client, store_doc, item_code)
		except Exception as exc:
			# One row's fault is one row's fault. Letting it escape failed every other row in
			# the claimed group -- including the ones after it, which were never attempted at
			# all -- and nothing reconciles product, price or media, so those were simply lost
			# until somebody found them in the dashboard.
			failures[row["name"]] = exc

	if failures:
		raise PartialFailure(failures)


def _item_from(row: dict) -> str | None:
	"""The item a queue row is about.

	The payload comes back as the JSON string `frappe.as_json` wrote, not a dict -- reading
	it as one raised `'str' object has no attribute 'get'` on every single collection row, so
	the operation had never once succeeded.
	"""
	payload = row.get("payload")
	if payload:
		try:
			data = json.loads(payload) if isinstance(payload, str) else payload
			if isinstance(data, dict) and data.get("item_code"):
				return cstr(data["item_code"])
		except (ValueError, TypeError):
			pass
	return cstr(row.get("ref_docname")) or None


def sync_item_collections(client: ShopifyClient, store_doc, item_code: str) -> dict:
	"""Bring one product's membership of the mapped collections in line with its tax."""
	from shopify_integration.outbound.product import product_link_for

	link = product_link_for(store_doc.name, item_code)
	if not link or not link.product_gid:
		return {"skipped": "not published"}

	wanted, warning = target_collection(store_doc, item_code)
	if warning:
		frappe.logger("shopify_integration").warning(warning)
		return {"warning": warning}

	mapped = set(tax_collection_map(store_doc).values())
	current = _current_collections(client, link.product_gid) & mapped

	to_add = sorted({wanted} - current) if wanted else []
	to_remove = sorted(current - ({wanted} if wanted else set()))

	if to_add or to_remove:
		payload = {"id": link.product_gid}
		if to_add:
			payload["collectionsToJoin"] = to_add
		if to_remove:
			payload["collectionsToLeave"] = to_remove
		client.execute(load_query("product_collections_update"), {"product": payload}, cost_hint=10)

	return {"product": link.product_gid, "added": to_add, "removed": to_remove}


def _current_collections(client: ShopifyClient, product_gid: str) -> set[str]:
	data = client.execute(load_query("product_collections"), {"id": product_gid}, cost_hint=5)
	nodes = ((data.get("product") or {}).get("collections") or {}).get("nodes") or []
	return {cstr(node.get("id")) for node in nodes if node.get("id")}
