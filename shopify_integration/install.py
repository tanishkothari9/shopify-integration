"""Install and migrate hooks.

Composite indexes live here rather than in the doctype JSON because Frappe's schema
definition only expresses single-column indexes, and the queue's performance depends on
multi-column ones.
"""

from __future__ import annotations

import frappe

#: (doctype, columns, index name). Spec §5.5, §5.4.
COMPOSITE_INDEXES = [
	# The drain query: every claim pass filters on exactly these three columns, in this order.
	("Shopify Sync Queue", ["store", "state", "next_attempt_at"], "sync_queue_drain"),
	# Supports the dashboard's per-store failure counts without scanning the table.
	("Shopify Sync Queue", ["store", "operation", "state"], "sync_queue_store_op"),
	# Event log lookups when replaying a store's history for support.
	("Shopify Event Log", ["store", "topic", "status"], "event_log_store_topic"),
	# Resolving an order line's SKU to a mapping, per store.
	("Shopify Item Link", ["store", "sku"], "item_link_store_sku"),
	# The inventory delta query: "links whose watermark predates the bin's last change".
	("Shopify Item Link", ["store", "inventory_synced_on"], "item_link_store_synced"),
	# Finding every link for a product, which product webhooks and unlinking both need.
	("Shopify Item Link", ["store", "product_gid"], "item_link_store_product"),
	# Watching a store's in-flight bulk operations.
	("Shopify Bulk Operation", ["store", "type", "status"], "bulk_op_store_type_status"),
]

#: (doctype, columns, constraint name). These are correctness constraints, not performance
#: ones: without them a re-run of the catalogue import could create a second mapping for the
#: same variant, and inventory would then be pushed twice with different answers.
UNIQUE_CONSTRAINTS = [
	("Shopify Item Link", ["store", "item_code"], "item_link_store_item"),
	("Shopify Item Link", ["store", "variant_gid"], "item_link_store_variant"),
	# One Sales Order per Shopify order, per store (spec §8.3). This constraint is what makes
	# duplicate webhook delivery a no-op rather than a second order for the same sale.
	("Sales Order", ["shopify_store", "shopify_order_gid"], "so_shopify_order"),
	# One credit note per Shopify refund, per store (spec §8.6). Without this a redelivered
	# refunds/create webhook would credit the customer twice.
	("Sales Invoice", ["shopify_store", "shopify_refund_gid"], "si_shopify_refund"),
]


#: Custom fields that link ERPNext documents back to Shopify (spec §8.3, §17).
#:
#: Custom fields rather than a separate link doctype: a user looking at a Sales Order needs to
#: see which Shopify order it came from.
CUSTOM_FIELDS = {
	"Sales Order": [
		{
			"fieldname": "shopify_section",
			"fieldtype": "Section Break",
			"label": "Shopify",
			"insert_after": "order_type",
			"collapsible": 1,
		},
		{
			"fieldname": "shopify_store",
			"fieldtype": "Link",
			"options": "Shopify Store",
			"label": "Shopify Store",
			"insert_after": "shopify_section",
			"read_only": 1,
		},
		{
			"fieldname": "shopify_order_gid",
			"fieldtype": "Data",
			"label": "Shopify Order GID",
			"insert_after": "shopify_store",
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "shopify_order_number",
			"fieldtype": "Data",
			"label": "Shopify Order Number",
			"insert_after": "shopify_order_gid",
			"read_only": 1,
			"no_copy": 1,
		},
	],
	"Sales Invoice": [
		{
			"fieldname": "shopify_section",
			"fieldtype": "Section Break",
			"label": "Shopify",
			"insert_after": "is_return",
			"collapsible": 1,
		},
		{
			"fieldname": "shopify_store",
			"fieldtype": "Link",
			"options": "Shopify Store",
			"label": "Shopify Store",
			"insert_after": "shopify_section",
			"read_only": 1,
		},
		{
			"fieldname": "shopify_order_gid",
			"fieldtype": "Data",
			"label": "Shopify Order GID",
			"insert_after": "shopify_store",
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "shopify_refund_gid",
			"fieldtype": "Data",
			"label": "Shopify Refund GID",
			"insert_after": "shopify_order_gid",
			"read_only": 1,
			"no_copy": 1,
		},
	],
	"Delivery Note": [
		{
			"fieldname": "shopify_section",
			"fieldtype": "Section Break",
			"label": "Shopify",
			"insert_after": "is_return",
			"collapsible": 1,
		},
		{
			"fieldname": "shopify_store",
			"fieldtype": "Link",
			"options": "Shopify Store",
			"label": "Shopify Store",
			"insert_after": "shopify_section",
			"read_only": 1,
		},
		{
			"fieldname": "shopify_order_gid",
			"fieldtype": "Data",
			"label": "Shopify Order GID",
			"insert_after": "shopify_store",
			"read_only": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "shopify_fulfillment_gid",
			"fieldtype": "Data",
			"label": "Shopify Fulfillment GID",
			"insert_after": "shopify_order_gid",
			"read_only": 1,
			"no_copy": 1,
		},
	],
	"Item": [
		{
			"fieldname": "publish_to_shopify",
			"fieldtype": "Check",
			"label": "Publish to Shopify",
			"insert_after": "item_group",
			"default": "0",
			"description": (
				"Create this item as a product on every Shopify store set to publish new items. "
				"Off by default and per item on purpose: most of an ERPNext catalogue is raw "
				"materials, packaging and internal parts that must never appear in a shop."
			),
		},
	],
	"Customer": [
		{
			"fieldname": "shopify_customer_gid",
			"fieldtype": "Data",
			"label": "Shopify Customer GID",
			"insert_after": "customer_group",
			"read_only": 1,
			"no_copy": 1,
		},
	],
}


def ensure_custom_fields():
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	# Stamped with the module so they are identifiable as ours in the Custom Field list.
	tagged = {
		doctype: [dict(field, module="Shopify Integration") for field in fields]
		for doctype, fields in CUSTOM_FIELDS.items()
	}
	create_custom_fields(tagged, ignore_validate=True)


def after_install():
	"""Runs once, on a fresh `install-app`.

	Needed as well as after_migrate because Frappe marks every entry in patches.txt as
	already-applied when an app is first installed. Without this hook a brand new site would
	silently never get the composite indexes, and the drain query would table-scan.
	"""
	ensure_custom_fields()
	ensure_composite_indexes()


def after_migrate():
	ensure_custom_fields()
	ensure_composite_indexes()


def ensure_composite_indexes():
	"""Create the composite indexes and unique constraints. Idempotent."""
	for doctype, columns, index_name in COMPOSITE_INDEXES:
		if not frappe.db.table_exists(doctype):
			continue
		try:
			frappe.db.add_index(doctype, columns, index_name)
		except Exception:
			# add_index is create-if-missing on MariaDB, but a concurrent migrate on another
			# bench worker can still race us. An index that already exists is success.
			frappe.log_error(
				title=f"Could not create index {index_name} on {doctype}",
				message=frappe.get_traceback(),
			)

	for doctype, columns, constraint_name in UNIQUE_CONSTRAINTS:
		if not frappe.db.table_exists(doctype):
			continue
		try:
			frappe.db.add_unique(doctype, columns, constraint_name)
		except Exception:
			# Unlike an index, this one can fail for a reason worth seeing: duplicate rows
			# already in the table. Log loudly rather than leaving the constraint silently
			# absent, because its absence is a correctness problem, not a slow query.
			frappe.log_error(
				title=f"Could not create unique constraint {constraint_name} on {doctype}",
				message=frappe.get_traceback(),
			)
