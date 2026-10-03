"""Give every already-published template its own product link.

Until now a template had no link of its own: its Shopify product could only be found
backwards, through whichever of its variants still carried the right `template_item`. One
corrupted child was enough to lose the whole range, and the app then made a *second* Shopify
product for it.

Existing sites have the links; what they lack is the template's row. This writes one per
(store, template) from the variants' own rows, which all name the same product.

Templates whose variants disagree about the product are skipped and named in the log. Two
products for one range is exactly the damage this change exists to prevent, and picking one
of them here -- without seeing either shop -- would be a guess.
"""

from __future__ import annotations

import frappe


def execute():
	if not frappe.db.has_column("Shopify Item Link", "is_template"):
		return

	groups = frappe.db.sql(
		"""
		SELECT store, template_item,
		       COUNT(DISTINCT product_gid) AS products,
		       MIN(product_gid) AS product_gid
		FROM `tabShopify Item Link`
		WHERE IFNULL(template_item, '') != ''
		  AND IFNULL(product_gid, '') != ''
		  AND IFNULL(is_template, 0) = 0
		GROUP BY store, template_item
		""",
		as_dict=True,
	)

	written = skipped = 0
	for row in groups:
		if row.products > 1:
			frappe.logger("shopify_integration").warning(
				f"{row.template_item} on {row.store} has variants pointing at {row.products} "
				"different Shopify products; not writing a template link for it until someone "
				"decides which product the range is."
			)
			skipped += 1
			continue

		if frappe.db.exists("Shopify Item Link", {"store": row.store, "item_code": row.template_item}):
			continue

		link = frappe.new_doc("Shopify Item Link")
		link.store = row.store
		link.item_code = row.template_item
		link.product_gid = row.product_gid
		link.is_template = 1
		link.is_variant = 0
		# A template that is already live has long since had its opening push; the children
		# carry the watermarks for it. Without this the first drain after the upgrade would
		# re-send stock, price and images for every published range at once.
		link.initial_state_pushed = 1
		link.insert(ignore_permissions=True)
		written += 1

	frappe.db.commit()
	if written or skipped:
		print(
			f"shopify_integration: linked {written} published template(s) to their Shopify "
			f"product" + (f"; skipped {skipped} with conflicting products" if skipped else "")
		)
