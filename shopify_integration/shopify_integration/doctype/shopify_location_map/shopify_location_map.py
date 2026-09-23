"""Shopify Location Map (child of Shopify Store) -- spec §5.2.

Maps an ERPNext warehouse to a Shopify location. Mapping a *group* warehouse consolidates
every descendant leaf warehouse into that one Shopify location; the resolution walk lives on
the parent, in ShopifyStore.location_gid_for_warehouse.
"""

from frappe.model.document import Document


class ShopifyLocationMap(Document):
	pass
