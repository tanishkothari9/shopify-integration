"""A manual Shopify collection this product should belong to. The app joins and leaves only the collections it put the product in; anything added by hand in the Shopify admin is left alone."""

from __future__ import annotations

from frappe.model.document import Document


class ShopifyItemCollection(Document):
	pass
