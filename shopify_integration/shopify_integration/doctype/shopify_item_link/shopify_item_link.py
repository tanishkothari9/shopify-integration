"""Shopify Item Link -- the mapping table (spec §5.4).

Replaces `ecommerce_integrations`' `Ecommerce Item`. One row per (store, ERPNext item),
carrying the Shopify identifiers and the per-feature sync watermarks.

Uniqueness is enforced by composite indexes created in install.py, not here: Frappe's
doctype JSON can only express single-column uniqueness, and both of the constraints that
matter -- (store, item_code) and (store, variant_gid) -- span two columns.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class ShopifyItemLink(Document):
	def validate(self):
		if self.is_variant and not self.template_item:
			frappe.throw(_("A variant link must name its template item."))
		if self.variant_gid and not self.product_gid:
			frappe.throw(_("A variant GID requires the product GID it belongs to."))


def get_link(
	store: str, *, item_code: str | None = None, variant_gid: str | None = None, sku: str | None = None
) -> dict | None:
	"""Resolve a link by whichever identifier the caller has.

	Ordered by how trustworthy the identifier is: the GID is Shopify's own stable key, the
	item code is ours, and the SKU is user-entered text that may be duplicated or reused.
	"""
	if variant_gid:
		found = frappe.db.get_value(
			"Shopify Item Link", {"store": store, "variant_gid": variant_gid}, "*", as_dict=True
		)
		if found:
			return found

	if item_code:
		found = frappe.db.get_value(
			"Shopify Item Link", {"store": store, "item_code": item_code}, "*", as_dict=True
		)
		if found:
			return found

	if sku:
		return frappe.db.get_value("Shopify Item Link", {"store": store, "sku": sku}, "*", as_dict=True)
	return None


def linked_stores(item_code: str) -> list[str]:
	"""Stores that know about this item.

	Called from the Item doc_event on every save, so it stays a single indexed read. Most
	saves in a large catalogue touch an item Shopify has never heard of, and those must cost
	almost nothing (spec §10.2).
	"""
	return frappe.get_all("Shopify Item Link", filters={"item_code": item_code}, pluck="store", distinct=True)
