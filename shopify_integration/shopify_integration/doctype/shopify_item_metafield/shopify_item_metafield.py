"""One metafield to publish on the Shopify product. Namespace and key together name it; the type must match the definition on the store or Shopify refuses the write."""

from __future__ import annotations

from frappe.model.document import Document


class ShopifyItemMetafield(Document):
	pass
