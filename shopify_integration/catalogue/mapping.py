"""The single mapping writer (spec §8.7).

Three paths produce ERPNext Items from Shopify products -- bulk import, the product webhooks,
and lazy resolution of an unknown SKU on an incoming order -- and all three come through
here. One writer is what keeps them from drifting into three subtly different item shapes.

Everything written here is wrapped in ``inbound_write()``, so nothing it touches can echo
back to Shopify.
"""

from __future__ import annotations

import hashlib
import json

import frappe
from frappe import _
from frappe.utils import cstr, flt

from shopify_integration.catalogue.echo import inbound_write, mark

#: Shopify caps a product at three options. ERPNext items with more attributes cannot
#: round-trip, so we refuse rather than silently dropping the fourth (spec §8.7).
MAX_OPTIONS = 3

#: Shopify's WeightUnit enum -> ERPNext UOM name.
WEIGHT_UOM = {
	"GRAMS": "Gram",
	"KILOGRAMS": "Kg",
	"OUNCES": "Ounce",
	"POUNDS": "Pound",
}

#: Shopify's placeholder for "this product has no real options".
DEFAULT_OPTION_TITLE = "Title"
DEFAULT_OPTION_VALUE = "Default Title"

#: Fields on a document that move by themselves and say nothing about whether its content
#: did. Stripped before two states of an Item are compared.
VOLATILE_KEYS = frozenset(
	{
		"modified",
		"modified_by",
		"creation",
		"owner",
		"name",
		"parent",
		"idx",
		"docstatus",
		"doctype",
		"parentfield",
		"parenttype",
		"__islocal",
		"__unsaved",
		"__last_sync_on",
	}
)


def _content(doc) -> str:
	"""A stable rendering of a document's content, bookkeeping removed.

	Used to answer one question: would saving this change anything? Frappe's `save()` runs
	the full cycle regardless -- it writes `modified` and fires `on_update` whether or not a
	value moved -- and those doc_events are what queue outbound work. So an Item written
	from a webhook that changed nothing still pushed itself back to Shopify, which sent the
	webhook again.
	"""

	def strip(values: dict):
		return {
			key: ([strip(row) for row in value] if isinstance(value, list) else cstr(value))
			for key, value in values.items()
			if key not in VOLATILE_KEYS
		}

	return json.dumps(strip(doc.as_dict()), sort_keys=True, default=str)


def _save_if_changed(item, before: str | None) -> bool:
	"""Save the Item only when something actually moved. Returns whether it saved.

	`before` is the content snapshot taken when the document was loaded, or None for one
	being created. Besides sparing the doc_events, this is what stopped the inbound writer
	colliding with the outbound one: two jobs saving the same unchanged Item a few hundred
	milliseconds apart produced forty "Document has been modified after you have opened it"
	errors in eight minutes, every one of them about a write that had nothing to write.
	"""
	if before is not None and _content(item) == before:
		return False
	mark(item)
	item.save(ignore_permissions=True)
	return True


def product_digest(product: dict) -> str:
	"""A hash of exactly the Shopify fields this app maps onto ERPNext.

	Deliberately not the whole payload, and deliberately not `updated_at`. Shopify stamps
	`updated_at` and fires `products/update` for things that touch nothing on this side --
	a metafield, a tag, an image reorder, the publication state, our own inventory or price
	mutation. Hashing only the mapped fields is what makes an echo of our own write
	recognisable as one.

	Prices are excluded for the same reason: ERPNext owns them outbound and nothing here
	reads them back, so our own price push must not read as a change.
	"""

	def weight(variant: dict, part: str) -> str:
		measurement = ((variant.get("inventoryItem") or {}).get("measurement") or {}).get("weight") or {}
		return cstr(measurement.get(part))

	# Both shapes, because the cost of getting this wrong is silent. `product_by_id` returns
	# variants as a connection and the webhook handler flattens it to a list before mapping;
	# a caller that hashed the unflattened shape would produce a digest that never matches
	# the stored one, and echo suppression would simply stop working without a word.
	variants = product.get("variants") or []
	if isinstance(variants, dict):
		variants = [edge["node"] for edge in (variants.get("edges") or []) if edge.get("node")]

	material = {
		"title": cstr(product.get("title")),
		"description": cstr(product.get("description")),
		"status": cstr(product.get("status")),
		"vendor": cstr(product.get("vendor")),
		"options": [
			{"name": cstr(o.get("name")), "values": sorted(cstr(v) for v in (o.get("values") or []))}
			for o in real_options(product)
		],
		"variants": sorted(
			(
				{
					"id": cstr(v.get("id")),
					# Mapped onto the link rather than the Item, but mapped all the same:
					# leaving it out would let a changed inventory item slip past as "no
					# mapped field differs" and strand the link pointing at the old one.
					"inventory_item": cstr((v.get("inventoryItem") or {}).get("id")),
					"sku": cstr(v.get("sku")),
					"title": cstr(v.get("title")),
					"weight": weight(v, "value"),
					"weight_unit": weight(v, "unit"),
					"options": [
						{"name": cstr(o.get("name")), "value": cstr(o.get("value"))}
						for o in (v.get("selectedOptions") or [])
					],
				}
				for v in variants
			),
			key=lambda variant: variant["id"],
		),
	}
	return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def stored_digest(store: str, product_gid: str) -> str | None:
	"""The digest recorded the last time this product changed anything on this side."""
	if not product_gid:
		return None
	return frappe.db.get_value(
		"Shopify Item Link", {"store": store, "product_gid": product_gid}, "inbound_digest"
	)


def record_digest(store: str, product_gid: str, digest: str) -> None:
	"""Stamp the digest on every link for this product.

	Every link, because a product with twelve variants has twelve of them and the check
	reads whichever one it finds first. Written with `update_modified=False`: this is the
	app's own bookkeeping and must not make the link look edited.
	"""
	if not (product_gid and digest):
		return
	frappe.db.sql(
		"""UPDATE `tabShopify Item Link` SET inbound_digest = %s
		   WHERE store = %s AND product_gid = %s""",
		(digest, store, product_gid),
	)


def write_product_mapping(store: str, product: dict) -> dict:
	"""Create or update the ERPNext Items and Shopify Item Links for one product.

	Returns a summary naming what was written, which the bulk importer logs and the webhook
	handlers record on the event log.
	"""
	with inbound_write():
		options = real_options(product)
		if len(options) > MAX_OPTIONS:
			frappe.throw(
				_("Shopify product {0} has {1} options; at most {2} can be mapped.").format(
					product.get("id"), len(options), MAX_OPTIONS
				)
			)

		variants = product.get("variants") or []
		if not variants:
			frappe.throw(_("Shopify product {0} has no variants to map.").format(product.get("id")))

		if not options:
			result = _write_simple_product(store, product, variants[0])
		else:
			result = _write_variant_product(store, product, options, variants)

		# After the links exist, because that is what the digest is stamped on. Recorded on
		# every path, not just the webhook: a bulk import or a reconcile has seen the same
		# product, so the next webhook carrying it unchanged has nothing to do either.
		record_digest(store, cstr(product.get("id")), product_digest(product))
		return result


def real_options(product: dict) -> list[dict]:
	"""Options that actually vary, ignoring Shopify's single-variant placeholder.

	Every Shopify product has at least one variant. A product with no real options still
	reports one option called "Title" with the single value "Default Title"; mapping that to
	an ERPNext variant template would produce a meaningless attribute on every such item.
	"""
	found = []
	for option in product.get("options") or []:
		name = cstr(option.get("name")).strip()
		values = [v for v in (option.get("values") or []) if cstr(v).strip()]
		if name == DEFAULT_OPTION_TITLE and values == [DEFAULT_OPTION_VALUE]:
			continue
		if name:
			found.append({"name": name, "values": values})
	return found


def _write_simple_product(store: str, product: dict, variant: dict) -> dict:
	item_code = resolve_item_code(store, product, variant)
	item = _upsert_item(item_code, product, variant, item_group=item_group_for(product), store=store)
	link = upsert_link(store, item_code=item.name, product=product, variant=variant, is_variant=False)
	return {"item": item.name, "links": [link], "template": None}


def _write_variant_product(store: str, product: dict, options: list[dict], variants: list[dict]) -> dict:
	"""Map a multi-option product to an ERPNext variant template plus its variant Items."""
	attributes = [ensure_item_attribute(option) for option in options]

	template_code = _template_already_linked(store, product)
	if not template_code:
		template_code = template_item_code(product)
		_refuse_to_twin(store, product, template_code)

	template = _upsert_template(template_code, product, attributes, item_group_for(product), store)

	links = []
	warnings = []
	for variant in variants:
		variant_code = resolve_item_code(store, product, variant)
		item, warning = _upsert_variant_item(variant_code, template, product, variant, store)
		if warning:
			warnings.append(warning)
		links.append(
			upsert_link(
				store,
				item_code=item.name,
				product=product,
				variant=variant,
				is_variant=True,
				template_item=template.name,
			)
		)
	result = {"item": template.name, "links": links, "template": template.name}
	if warnings:
		result["warnings"] = warnings
	return result


# --------------------------------------------------------------------------------------
# Item writers
# --------------------------------------------------------------------------------------


def _apply_default_hsn(item, store: str) -> None:
	"""Give a newly created Item the store's fallback HSN/SAC code. India only.

	With `india_compliance` installed, an HSN code is mandatory on every sales item, and
	Shopify has no equivalent field to supply one. Without a fallback the very first product
	of a catalogue import fails to save and nothing imports at all.

	Applied only when the item is being created. An item that already carries a code keeps it:
	the HSN is the merchant's own, maintained in ERPNext against the goods they actually sell,
	and an import must never overwrite it with a blanket default.

	A no-op on any site without india_compliance, where the field does not exist.
	"""
	if not item.meta.has_field("gst_hsn_code") or item.get("gst_hsn_code"):
		return

	default = frappe.db.get_value("Shopify Store", store, "default_hsn_code")
	if default:
		item.gst_hsn_code = default


def _upsert_item(item_code: str, product: dict, variant: dict, item_group: str, store: str):
	existing = frappe.db.exists("Item", item_code)
	item = frappe.get_doc("Item", item_code) if existing else frappe.new_doc("Item")
	before = _content(item) if existing else None

	if not existing:
		item.item_code = item_code
		item.item_group = item_group
		item.stock_uom = default_stock_uom()
		_apply_default_hsn(item, store)

	item.item_name = _item_name(product, variant)
	item.description = product.get("description") or item.item_name
	item.disabled = 1 if product.get("status") == "ARCHIVED" else 0
	_apply_weight(item, variant)
	_apply_supplier(item, product, store)

	_save_if_changed(item, before)
	return item


def _refuse_to_twin(store: str, product: dict, template_code: str) -> None:
	"""Stop a product we already sell being imported as a second ERPNext range.

	Reached only when nothing recognised the product -- no link, and no ERPNext items behind
	its SKUs -- so the template is about to be named after the Shopify handle and created.
	That is correct for a range this shop genuinely does not have. It is catastrophic for one
	it does: on 3 October it gave SHOPIFY TEST A3 a twin with no variants, pulled the XL link
	onto the twin, and the range stopped accepting sizes.

	So before creating anything, ask the plainest question there is: does this shop already
	have an item with one of these SKUs? If it does, something upstream is wrong -- the
	resolution above should have found it -- and guessing is worse than stopping. The webhook
	is marked failed with a message naming the collision, and the merchant's catalogue is
	left exactly as it was.
	"""
	skus = [cstr(v.get("sku")).strip() for v in (product.get("variants") or [])]
	skus = [sku for sku in skus if sku]
	if not skus:
		return

	clashing = frappe.get_all("Item", filters={"name": ["in", skus]}, pluck="name")
	if not clashing:
		return

	frappe.throw(
		_(
			"Shopify product {0} carries SKUs this shop already sells ({1}), but nothing links "
			"it to an ERPNext range. Importing it would create a second template called {2} "
			"and move those items onto it. Link the product to its range, or clear the SKUs on "
			"Shopify, and retry this webhook."
		).format(
			product.get("id"), ", ".join(sorted(clashing)[:5]), template_code
		)
	)


def _upsert_template(template_code: str, product: dict, attributes: list[str], item_group: str, store: str):
	existing = frappe.db.exists("Item", template_code)
	item = frappe.get_doc("Item", template_code) if existing else frappe.new_doc("Item")
	before = _content(item) if existing else None

	if not existing:
		item.item_code = template_code
		item.item_group = item_group
		item.stock_uom = default_stock_uom()
		item.has_variants = 1
		_apply_default_hsn(item, store)

	item.item_name = product.get("title") or template_code
	item.description = product.get("description") or item.item_name
	item.disabled = 1 if product.get("status") == "ARCHIVED" else 0
	_apply_supplier(item, product, store)

	present = {row.attribute for row in (item.attributes or [])}
	for attribute in attributes:
		if attribute not in present:
			item.append("attributes", {"attribute": attribute})

	_save_if_changed(item, before)
	return item


def _upsert_variant_item(item_code: str, template, product: dict, variant: dict, store: str):
	existing = frappe.db.exists("Item", item_code)
	item = frappe.get_doc("Item", item_code) if existing else frappe.new_doc("Item")
	before = _content(item) if existing else None

	if not existing:
		item.item_code = item_code
		item.variant_of = template.name
		item.item_group = template.item_group
		item.stock_uom = template.stock_uom
		_apply_default_hsn(item, store)

	item.item_name = _item_name(product, variant)
	item.description = product.get("description") or item.item_name
	item.disabled = 1 if product.get("status") == "ARCHIVED" else 0
	_apply_weight(item, variant)

	warning = _apply_variant_attributes(item, variant, existing)

	_save_if_changed(item, before)
	return item, warning


def _attribute_pairs(variant: dict) -> list[tuple[str, str]]:
	"""The (attribute, value) pairs Shopify states for this variant, cleaned."""
	pairs = []
	for selected in variant.get("selectedOptions") or []:
		attribute = cstr(selected.get("name")).strip()
		value = cstr(selected.get("value")).strip()
		if attribute and value:
			pairs.append((attribute, value))
	return pairs


def _same_attributes(item, wanted: list[tuple[str, str]]) -> bool:
	"""Whether the item already carries exactly these attributes.

	Compared case- and space-insensitively on both sides, because "Golden" and "golden " are
	the same option to a merchant and rewriting the row for that difference is what ERPNext
	refuses.
	"""

	def normalise(pairs):
		return sorted((a.strip().casefold(), v.strip().casefold()) for a, v in pairs)

	current = [(row.attribute, row.attribute_value) for row in item.get("attributes") or []]
	return normalise(current) == normalise(wanted)


def _has_stock_history(item_code: str) -> bool:
	return bool(frappe.db.exists("Stock Ledger Entry", {"item_code": item_code, "is_cancelled": 0}))


def _apply_variant_attributes(item, variant: dict, existing) -> str | None:
	"""Set the variant's attributes, unless ERPNext would refuse the change.

	``item.set("attributes", [])`` followed by re-appending the same rows counts as a change,
	and ERPNext rejects any change to a variant's attributes once the item has stock
	transactions: "Cannot change Attributes after stock transaction." Since Shopify fires
	products/update on inventory movements among much else, that turned every update of a
	stocked variant into a failure -- and took the rest of the update, the name, description,
	archived flag and weight, down with it.

	So: rewrite the rows only when they would actually differ, and when they differ on an item
	that already has stock, keep the other fields and report what could not be applied rather
	than losing the whole update.

	Returns a warning to record, or None.
	"""
	wanted = _attribute_pairs(variant)

	if existing and not wanted:
		# Shopify saying nothing about the options is not Shopify saying there are none. A
		# partial payload would otherwise wipe the rows off an existing variant.
		return None

	if existing and _same_attributes(item, wanted):
		return None

	if existing and wanted and _has_stock_history(item.name):
		current = ", ".join(f"{row.attribute}={row.attribute_value}" for row in item.get("attributes") or [])
		incoming = ", ".join(f"{attribute}={value}" for attribute, value in wanted)
		warning = (
			f"Variant options changed in Shopify ({current or 'none'} -> {incoming}), but {item.name} "
			"has stock transactions and ERPNext does not allow a variant's attributes to change "
			"after that. Everything else on the item was updated. To follow Shopify, make a new "
			"Item and transfer the stock to it."
		)
		frappe.logger("shopify_integration").warning(warning)
		return warning

	item.set("attributes", [])
	for attribute, value in wanted:
		ensure_attribute_value(attribute, value)
		item.append("attributes", {"attribute": attribute, "attribute_value": value})
	return None


def _item_name(product: dict, variant: dict) -> str:
	title = cstr(product.get("title")).strip()
	variant_title = cstr(variant.get("title")).strip()
	if variant_title and variant_title != DEFAULT_OPTION_VALUE:
		return f"{title} - {variant_title}"[:140]
	return title[:140]


def _apply_weight(item, variant: dict) -> None:
	measurement = ((variant.get("inventoryItem") or {}).get("measurement") or {}).get("weight") or {}
	value = measurement.get("value")
	unit = measurement.get("unit")
	if value in (None, "") or unit not in WEIGHT_UOM:
		return
	# flt, not parse_money. Weight is a measurement, not money: Shopify sends it as a JSON
	# number, and the money parser refuses floats on purpose. Routing it through there made
	# every real product fail on import while the tests passed, because the test fixture used
	# a string where Shopify sends a number.
	item.weight_per_unit = flt(value)
	item.weight_uom = ensure_uom(WEIGHT_UOM[unit])


def _apply_supplier(item, product: dict, store: str) -> None:
	"""Record Shopify's Vendor as a Supplier on the item -- if the merchant asked for that.

	Off unless a store opts in, because Shopify's Vendor is not a supplier field. Left alone
	it holds the shop's own name, so on a live store this created a Supplier called "Smart
	Choice" and attached it to every product the shop sells to itself. A Supplier is a real
	accounting record with a ledger behind it; inventing one from a display field is not
	something to do by default.

	And never for the shop's own name even when the setting is on. A product published from
	ERPNext comes back through products/create carrying whatever Vendor we sent or Shopify
	defaulted to, which is exactly the value that must not become a Supplier.
	"""
	vendor = cstr(product.get("vendor")).strip()
	if not vendor:
		return

	if not frappe.db.get_value("Shopify Store", store, "create_suppliers_from_vendor"):
		return

	if _is_the_shop_itself(vendor, store):
		frappe.logger("shopify_integration").info(
			f"Not making a Supplier from vendor {vendor!r}: that is this shop, not someone it buys from."
		)
		return

	supplier = ensure_supplier(vendor)
	if not any(row.supplier == supplier for row in (item.supplier_items or [])):
		item.append("supplier_items", {"supplier": supplier})


def _is_the_shop_itself(vendor: str, store: str) -> bool:
	"""Whether this vendor name is just the shop's own, however it is spelled."""
	settings = (
		frappe.db.get_value("Shopify Store", store, ["store_name", "shop_domain"], as_dict=True)
		or frappe._dict()
	)

	subdomain = cstr(settings.get("shop_domain")).split(".")[0]
	ours = {
		cstr(value).strip().casefold()
		for value in (store, settings.get("store_name"), settings.get("shop_domain"), subdomain)
		if cstr(value).strip()
	}
	return vendor.strip().casefold() in ours


# --------------------------------------------------------------------------------------
# Naming and supporting records
# --------------------------------------------------------------------------------------


def resolve_item_code(store: str, product: dict, variant: dict) -> str:
	"""Pick the ERPNext item code for a Shopify variant.

	SKU first, because that is the identifier a warehouse and a purchase order both use. If
	there is no SKU, fall back to the variant's numeric id -- ugly but stable, and far better
	than deriving from a title that the merchant will rename next week.
	"""
	from shopify_integration.shopify_integration.doctype.shopify_item_link.shopify_item_link import (
		get_link,
	)

	existing = get_link(store, variant_gid=variant.get("id"))
	if existing:
		return existing["item_code"]

	sku = cstr(variant.get("sku")).strip()
	if sku:
		return sku[:140]
	return f"SHOPIFY-{gid_suffix(variant.get('id'))}"


def _template_already_linked(store: str, product: dict) -> str | None:
	"""The ERPNext template this Shopify product is already mapped to, if any.

	Publishing a template from ERPNext makes Shopify fire `products/create` straight back at
	us. Without this the inbound writer does not recognise the product as one we just made:
	it names a template after the Shopify *handle*, so `KURTA-SET` gained a twin called
	`chikankari-kurta`, the variant links were rewired to the twin, and the run then died on
	the unique (store, item_code) constraint.

	The damage was quiet and total. `_stores_for` finds a published template by
	`template_item`, so with the links pointing at the handle no new size could ever attach --
	the range looked published and silently stopped accepting variants.
	"""
	gid = cstr(product.get("id"))
	if gid:
		by_gid = frappe.db.get_value(
			"Shopify Item Link",
			{"store": store, "product_gid": gid, "template_item": ("is", "set")},
			"template_item",
		)
		if by_gid:
			return by_gid

	# The links may not name this product at all. A product the app created a moment ago is
	# announced back by products/create before anything is linked to it, and a duplicate
	# created by mistake never gets links of its own. Either way its SKUs are item codes this
	# store already sells, and that is enough to know the range is ours: reuse the template
	# they belong to rather than inventing a second one named after the Shopify handle and
	# rewiring the existing variants onto it, which is what turned one bad product into two
	# bogus ERPNext templates.
	skus = [cstr(variant.get("sku")).strip() for variant in product.get("variants") or []]
	skus = [sku for sku in skus if sku]
	if not skus:
		return None

	templates = frappe.get_all(
		"Shopify Item Link",
		filters={"store": store, "item_code": ["in", skus], "template_item": ("is", "set")},
		pluck="template_item",
		distinct=True,
	)
	if len(templates) == 1:
		frappe.logger("shopify_integration").info(
			f"Recognised {gid or 'an incoming product'} as {templates[0]} by its SKUs; not "
			"creating another template for it."
		)
		return templates[0]
	if len(templates) > 1:
		frappe.logger("shopify_integration").warning(
			f"{gid or 'An incoming product'} carries SKUs belonging to several ERPNext "
			f"templates ({', '.join(sorted(templates))}). Leaving it for someone to sort out "
			"rather than guessing which range it is."
		)
		return None

	# Last, and the only route that does not depend on a link existing yet.
	#
	# Publishing a template takes several calls -- create the product, create its variants,
	# re-read them -- and the links are written at the end of all of it. Shopify fires
	# products/create the instant the product exists, so the echo routinely arrives while
	# that job is still running and there is not one link to find. Both routes above then
	# come back empty, the template is named after the Shopify handle instead, and a second
	# ERPNext template is created for a range that already has one. On 3 October that turned
	# SHOPIFY TEST A3 into a twin with no variants and dragged the XL link onto it.
	#
	# The SKUs are the answer, and they are in the payload. They are this app's own item
	# codes, so if ERPNext already has them as variants of one parent, that parent is the
	# template -- whatever the links do or do not say yet.
	parents = frappe.get_all(
		"Item",
		filters={"name": ["in", skus], "variant_of": ("is", "set")},
		pluck="variant_of",
		distinct=True,
	)
	parents = sorted(set(parents))
	if len(parents) == 1:
		frappe.logger("shopify_integration").info(
			f"Recognised {gid or 'an incoming product'} as {parents[0]} from the ERPNext items "
			"behind its SKUs; its links have not been written yet."
		)
		return parents[0]
	if len(parents) > 1:
		frappe.logger("shopify_integration").warning(
			f"{gid or 'An incoming product'} carries SKUs from several ERPNext templates "
			f"({', '.join(parents)}); leaving it rather than guessing."
		)
	return None


def template_item_code(product: dict) -> str:
	handle = cstr(product.get("handle")).strip()
	if handle:
		return handle[:140]
	return f"SHOPIFY-TPL-{gid_suffix(product.get('id'))}"


def gid_suffix(gid: str | None) -> str:
	"""``gid://shopify/ProductVariant/12345`` -> ``12345``."""
	return cstr(gid).rstrip("/").split("/")[-1] or "UNKNOWN"


def item_group_for(product: dict) -> str:
	product_type = cstr(product.get("productType")).strip()
	if not product_type:
		return default_item_group()
	return ensure_item_group(product_type)


def root_item_group() -> str:
	"""The Item Group tree root, looked up rather than assumed.

	ERPNext ships "All Item Groups" but lets a site rename it, and a site that has not run the
	setup wizard has no tree at all. Hardcoding the name fails on both, so find the actual
	root -- the group with no parent -- and create one only if there is genuinely no tree.
	"""
	# ifnull() rather than a filter on ("", None): in SQL, `IN ('', NULL)` never matches a NULL,
	# so a site whose tree root has a NULL parent -- which depends on how the site was created --
	# looked rootless here, and this function went on to create a second "All Item Groups" and
	# fail on the primary key. A root with an empty-string parent hid the bug entirely.
	root = frappe.db.sql(
		"""SELECT name FROM `tabItem Group`
		WHERE ifnull(parent_item_group, '') = '' AND is_group = 1
		ORDER BY lft LIMIT 1""",
	)
	if root:
		return root[0][0]

	group = frappe.new_doc("Item Group")
	group.item_group_name = "All Item Groups"
	group.is_group = 1
	mark(group)
	group.insert(ignore_permissions=True)
	return group.name


def default_item_group() -> str:
	"""Where products with no Shopify product type land."""
	for candidate in ("Products", "All Item Groups"):
		if frappe.db.exists("Item Group", candidate) and not frappe.db.get_value(
			"Item Group", candidate, "is_group"
		):
			return candidate

	leaf = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
	return leaf or ensure_item_group("Products")


def ensure_item_group(name: str) -> str:
	if frappe.db.exists("Item Group", name):
		return name
	group = frappe.new_doc("Item Group")
	group.item_group_name = name
	group.parent_item_group = root_item_group()
	group.is_group = 0
	mark(group)
	group.insert(ignore_permissions=True)
	return group.name


def default_stock_uom() -> str:
	"""The site's configured stock UOM, not a hardcoded "Nos".

	Stock Settings is where a site declares this, and a site that has not run the setup wizard
	may have no UOM records at all -- so fall back to creating the one we need.
	"""
	configured = frappe.db.get_single_value("Stock Settings", "stock_uom")
	if configured and frappe.db.exists("UOM", configured):
		return configured
	return ensure_uom("Nos")


def ensure_supplier(name: str) -> str:
	"""The Supplier of this name, creating it if nobody has yet.

	Two products/update webhooks arriving together both found no Supplier and both inserted,
	and the loser died on the unique name. Whoever got there first is the right answer for
	both, so the collision is caught rather than raised: the caller wanted a name, and the
	name now exists.
	"""
	if frappe.db.exists("Supplier", name):
		return name

	save_point = "shopify_ensure_supplier"
	frappe.db.savepoint(save_point)
	supplier = frappe.new_doc("Supplier")
	supplier.supplier_name = name
	mark(supplier)
	try:
		supplier.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		# Rolled back to the savepoint rather than caught bare: a failed insert leaves the
		# transaction dirty, and everything else this webhook is doing still has to commit.
		frappe.db.rollback(save_point=save_point)
		return name

	# Released after the try, not in an `else`: a `return` inside the `try` would skip an
	# `else` and leak the savepoint.
	frappe.db.release_savepoint(save_point)
	return supplier.name


def ensure_uom(name: str) -> str:
	if frappe.db.exists("UOM", name):
		return name
	uom = frappe.new_doc("UOM")
	uom.uom_name = name
	mark(uom)
	uom.insert(ignore_permissions=True)
	return uom.name


def ensure_item_attribute(option: dict) -> str:
	"""Create or extend the Item Attribute for a Shopify option, with all its values."""
	name = option["name"]
	if frappe.db.exists("Item Attribute", name):
		attribute = frappe.get_doc("Item Attribute", name)
	else:
		attribute = frappe.new_doc("Item Attribute")
		attribute.attribute_name = name

	added = _append_values(attribute, option["values"])
	if not added and not attribute.is_new():
		# Nothing to add, so nothing to save. Saving anyway rewrote `modified` on a document
		# that every variant product in the catalogue shares -- in a clothing shop, Colour
		# and Size are the two most shared documents there are. Two product webhooks a
		# second apart then collided on an attribute neither of them was changing, and the
		# loser died with "Document has been modified after you have opened it".
		#
		# It also meant an echo of this app's own publish rewrote the merchant's attribute
		# list, which is theirs and not ours to touch.
		return attribute.name

	_assert_abbreviations_unique(attribute)
	mark(attribute)
	attribute.save(ignore_permissions=True)
	return attribute.name


def ensure_attribute_value(attribute_name: str, value: str) -> None:
	"""Add a value to an existing attribute if Shopify has introduced a new one.

	Merchants add options after the initial import; without this the variant save fails with
	an unhelpful "not a valid attribute value".
	"""
	if not frappe.db.exists("Item Attribute", attribute_name):
		ensure_item_attribute({"name": attribute_name, "values": [value]})
		return

	attribute = frappe.get_doc("Item Attribute", attribute_name)
	if any(row.attribute_value == value for row in (attribute.item_attribute_values or [])):
		return

	if not _append_values(attribute, [value]):
		return

	_assert_abbreviations_unique(attribute)
	mark(attribute)
	attribute.save(ignore_permissions=True)


def _append_values(attribute, values: list[str]) -> int:
	"""Append values that are not already present, each with a non-colliding abbreviation.

	Returns how many were actually added, so a caller can tell a real change from a no-op
	and leave a shared document alone when there is nothing to write.

	ERPNext requires abbreviations to be unique within an attribute, and it ships a "Size"
	attribute whose "Small" value already occupies the abbreviation "S". A Shopify shop using
	standard sizes therefore collides on the very first import unless abbreviations are
	allocated against what is already there.
	"""
	rows = attribute.item_attribute_values or []
	present = {row.attribute_value for row in rows}
	taken = {cstr(row.abbr).upper() for row in rows}

	added = 0
	for value in values:
		value = cstr(value).strip()
		if not value or value in present:
			continue
		abbr = _unique_abbr(value, taken)
		taken.add(abbr)
		present.add(value)
		attribute.append("item_attribute_values", {"attribute_value": value, "abbr": abbr})
		added += 1
	return added


def _assert_abbreviations_unique(attribute) -> None:
	"""Fail with an actionable message if the attribute already has duplicate abbreviations.

	ERPNext requires them unique and refuses the save, but its own error names only the
	abbreviation -- not which attribute, nor which values collide, nor that the problem is
	pre-existing data rather than the import. Since we never add a colliding abbreviation,
	reaching this means the attribute was already broken, and the user needs to know exactly
	what to fix before any import touching it can succeed.
	"""
	seen: dict[str, str] = {}
	for row in attribute.item_attribute_values or []:
		key = cstr(row.abbr).lower()
		if not key:
			continue
		if key in seen:
			frappe.throw(
				_(
					"Item Attribute {0} already has two values sharing the abbreviation "
					"{1}: {2} and {3}. Give one of them a different abbreviation, then retry "
					"the import."
				).format(attribute.name, cstr(row.abbr), seen[key], row.attribute_value)
			)
		seen[key] = row.attribute_value


def _unique_abbr(value: str, taken: set[str]) -> str:
	"""An abbreviation for ``value`` that is not already used in this attribute."""
	base = "".join(ch for ch in cstr(value) if ch.isalnum()).upper()[:8] or "V"
	if base not in taken:
		return base

	for suffix in range(2, 1000):
		candidate = f"{base[: 8 - len(str(suffix))]}{suffix}"
		if candidate not in taken:
			return candidate
	raise ValueError(f"Could not allocate a unique abbreviation for {value!r}")


# --------------------------------------------------------------------------------------
# Link writer
# --------------------------------------------------------------------------------------


def upsert_link(
	store: str,
	*,
	item_code: str,
	product: dict,
	variant: dict,
	is_variant: bool,
	template_item: str | None = None,
) -> str:
	"""Create or refresh the Shopify Item Link for one variant.

	Idempotent by (store, variant_gid): re-importing a catalogue updates links rather than
	duplicating them, which is what makes the whole import safe to re-run.
	"""

	def _find() -> str | None:
		return frappe.db.get_value(
			"Shopify Item Link", {"store": store, "variant_gid": variant.get("id")}, "name"
		) or frappe.db.get_value("Shopify Item Link", {"store": store, "item_code": item_code}, "name")

	def _find_committed() -> str | None:
		"""The row that beat us, read past our own snapshot.

		`_find` cannot see it. Our transaction opened before the winner committed, and under
		MariaDB's REPEATABLE READ an ordinary SELECT keeps serving that snapshot -- so the
		unique index rejects the insert while the row it collided with stays invisible. The
		retry then finds nothing and re-raises, which is exactly the error this branch exists
		to prevent.

		A locking read is not subject to the snapshot: it returns the latest committed row.
		"""
		rows = frappe.db.sql(
			"""SELECT name FROM `tabShopify Item Link`
			   WHERE store = %s AND (variant_gid = %s OR item_code = %s)
			   LIMIT 1 FOR UPDATE""",
			(store, variant.get("id"), item_code),
		)
		return rows[0][0] if rows else None

	def _write(existing: str | None) -> str:
		link = (
			frappe.get_doc("Shopify Item Link", existing) if existing else frappe.new_doc("Shopify Item Link")
		)
		link.store = store
		link.item_code = item_code
		link.product_gid = product.get("id")
		link.variant_gid = variant.get("id")
		link.inventory_item_gid = (variant.get("inventoryItem") or {}).get("id")
		link.sku = cstr(variant.get("sku")).strip() or None
		link.is_variant = 1 if is_variant else 0
		link.template_item = template_item

		mark(link)
		link.save(ignore_permissions=True)
		return link.name

	def _written(name: str) -> str:
		"""Whatever ERPNext already knows about this item, now that there is somewhere to
		send it.

		Here rather than at the call sites because the call sites race each other: the
		webhook for a product this app is in the middle of publishing arrives through this
		same function, and whichever of the two writes the link first has to be the one that
		queues the stock. Outside the savepoint above, so a failure to enqueue is never
		mistaken for the insert losing a race.
		"""
		from shopify_integration.outbound.product import push_initial_state

		push_initial_state(store, item_code)
		return name

	existing = _find()
	if existing:
		return _written(_write(existing))

	# Nothing found, so this is an insert -- and inserts race. Publishing a product from
	# ERPNext makes Shopify fire `products/create` straight back, and that webhook lands while
	# the outbound push is still writing its own links. Both sides look, both find nothing,
	# and the slower one dies on the unique (store, item_code) index.
	#
	# A savepoint keeps the failed insert from poisoning the surrounding transaction, and the
	# row the loser wanted now exists, so look again and update it instead.
	save_point = "shopify_item_link_upsert"
	frappe.db.savepoint(save_point)
	try:
		name = _write(None)
	except frappe.UniqueValidationError:
		frappe.db.rollback(save_point=save_point)
		raced = _find_committed()
		if not raced:
			raise

		# Do not `get_doc` it: the same snapshot that hid the row from `_find` hides it from
		# the document loader too, and the retry dies on DoesNotExistError instead. A direct
		# UPDATE is not snapshot-bound, and the winner wrote this link for the same variant,
		# so the only field worth settling is the one the two sides can disagree on.
		frappe.db.set_value(
			"Shopify Item Link",
			raced,
			{
				"product_gid": product.get("id"),
				"variant_gid": variant.get("id"),
				"inventory_item_gid": (variant.get("inventoryItem") or {}).get("id"),
				"is_variant": 1 if is_variant else 0,
				"template_item": template_item,
			},
			update_modified=False,
		)
		# The winner wrote this link, and went through this same function to do it, so it is
		# the one that queued the stock. This covers the timings where the loser can see the
		# row too; the enqueue deduplicates either way.
		return _written(raced)

	# Released here, not in an `else:` -- a `return` inside the `try` would skip the `else`
	# entirely and leak a savepoint on every successful insert.
	frappe.db.release_savepoint(save_point)
	return _written(name)
