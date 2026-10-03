"""Mark links that have already had their opening push.

`initial_state_pushed` defaults to 0, and on an existing site that would read as "this link
has never sent its stock, price or images" for every link there is -- re-queueing the
opening push for the whole catalogue on the first drain after the upgrade.

A link that carries either watermark, or any media record, has demonstrably had it. Links
with none of the three are left at 0 deliberately: those are the ones the 1 October fault
produced, linked but never pushed, and this is the upgrade that finally sends them.
"""

from __future__ import annotations

import frappe

ALREADY_PUSHED = """
	IFNULL(initial_state_pushed, 0) = 0
	AND (
		inventory_synced_on IS NOT NULL
		OR price_synced_on IS NOT NULL
		OR (app_media IS NOT NULL AND app_media != '')
	)
"""


def execute():
	if not frappe.db.has_column("Shopify Item Link", "initial_state_pushed"):
		return

	pending = frappe.db.sql(
		f"SELECT COUNT(*) FROM `tabShopify Item Link` WHERE {ALREADY_PUSHED}"
	)[0][0]
	if not pending:
		return

	frappe.db.sql(
		f"UPDATE `tabShopify Item Link` SET initial_state_pushed = 1 WHERE {ALREADY_PUSHED}"
	)
	frappe.db.commit()
	print(f"shopify_integration: marked {pending} link(s) as already past their opening push")
