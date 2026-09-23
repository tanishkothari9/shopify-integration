"""Shopify Tax Mapping (child of Shopify Store) -- spec §5.3.

Maps a Shopify tax line title to an ERPNext account head. An unmapped tax title must fail
loudly when an order is built rather than being silently dropped (spec §13.2).
"""

from frappe.model.document import Document


class ShopifyTaxMapping(Document):
	pass
