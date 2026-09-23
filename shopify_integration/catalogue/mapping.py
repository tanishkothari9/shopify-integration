"""The single mapping writer (spec §8.7).

Three paths produce ERPNext Items from Shopify products -- bulk import, the product webhooks,
and lazy resolution of an unknown SKU on an incoming order -- and all three come through
here. One writer is what keeps them from drifting into three subtly different item shapes.

Everything written here is wrapped in ``inbound_write()``, so nothing it touches can echo
back to Shopify.
"""

from __future__ import annotations

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
			return _write_simple_product(store, product, variants[0])
		return _write_variant_product(store, product, options, variants)


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

	template_code = _template_already_linked(store, product) or template_item_code(product)
	template = _upsert_template(template_code, product, attributes, item_group_for(product), store)

	links = []
	for variant in variants:
		variant_code = resolve_item_code(store, product, variant)
		item = _upsert_variant_item(variant_code, template, product, variant, store)
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
	return {"item": template.name, "links": links, "template": template.name}


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

	if not existing:
		item.item_code = item_code
		item.item_group = item_group
		item.stock_uom = default_stock_uom()
		_apply_default_hsn(item, store)

	item.item_name = _item_name(product, variant)
	item.description = product.get("description") or item.item_name
	item.disabled = 1 if product.get("status") == "ARCHIVED" else 0
	_apply_weight(item, variant)
	_apply_supplier(item, product)

	mark(item)
	item.save(ignore_permissions=True)
	return item


def _upsert_template(template_code: str, product: dict, attributes: list[str], item_group: str, store: str):
	existing = frappe.db.exists("Item", template_code)
	item = frappe.get_doc("Item", template_code) if existing else frappe.new_doc("Item")

	if not existing:
		item.item_code = template_code
		item.item_group = item_group
		item.stock_uom = default_stock_uom()
		item.has_variants = 1
		_apply_default_hsn(item, store)

	item.item_name = product.get("title") or template_code
	item.description = product.get("description") or item.item_name
	item.disabled = 1 if product.get("status") == "ARCHIVED" else 0
	_apply_supplier(item, product)

	present = {row.attribute for row in (item.attributes or [])}
	for attribute in attributes:
		if attribute not in present:
			item.append("attributes", {"attribute": attribute})

	mark(item)
	item.save(ignore_permissions=True)
	return item


def _upsert_variant_item(item_code: str, template, product: dict, variant: dict, store: str):
	existing = frappe.db.exists("Item", item_code)
	item = frappe.get_doc("Item", item_code) if existing else frappe.new_doc("Item")

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

	item.set("attributes", [])
	for selected in variant.get("selectedOptions") or []:
		attribute = cstr(selected.get("name")).strip()
		value = cstr(selected.get("value")).strip()
		if not attribute or not value:
			continue
		ensure_attribute_value(attribute, value)
		item.append("attributes", {"attribute": attribute, "attribute_value": value})

	mark(item)
	item.save(ignore_permissions=True)
	return item


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


def _apply_supplier(item, product: dict) -> None:
	vendor = cstr(product.get("vendor")).strip()
	if not vendor:
		return
	supplier = ensure_supplier(vendor)
	if not any(row.supplier == supplier for row in (item.supplier_items or [])):
		item.append("supplier_items", {"supplier": supplier})


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
	if not gid:
		return None
	return frappe.db.get_value(
		"Shopify Item Link",
		{"store": store, "product_gid": gid, "template_item": ("is", "set")},
		"template_item",
	)


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
	if frappe.db.exists("Supplier", name):
		return name
	supplier = frappe.new_doc("Supplier")
	supplier.supplier_name = name
	mark(supplier)
	supplier.insert(ignore_permissions=True)
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

	_append_values(attribute, option["values"])
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

	_append_values(attribute, [value])
	_assert_abbreviations_unique(attribute)
	mark(attribute)
	attribute.save(ignore_permissions=True)


def _append_values(attribute, values: list[str]) -> None:
	"""Append values that are not already present, each with a non-colliding abbreviation.

	ERPNext requires abbreviations to be unique within an attribute, and it ships a "Size"
	attribute whose "Small" value already occupies the abbreviation "S". A Shopify shop using
	standard sizes therefore collides on the very first import unless abbreviations are
	allocated against what is already there.
	"""
	rows = attribute.item_attribute_values or []
	present = {row.attribute_value for row in rows}
	taken = {cstr(row.abbr).upper() for row in rows}

	for value in values:
		value = cstr(value).strip()
		if not value or value in present:
			continue
		abbr = _unique_abbr(value, taken)
		taken.add(abbr)
		present.add(value)
		attribute.append("item_attribute_values", {"attribute_value": value, "abbr": abbr})


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

	existing = _find()
	if existing:
		return _write(existing)

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
		return raced

	# Released here, not in an `else:` -- a `return` inside the `try` would skip the `else`
	# entirely and leak a savepoint on every successful insert.
	frappe.db.release_savepoint(save_point)
	return name
