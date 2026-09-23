"""Shopify Bulk Operation -- state machine for Shopify's async jobs (spec §5.7, §11).

Shopify runs one QUERY and one MUTATION per shop at a time. This document is how we know
whether a slot is free, where the result lives, and -- crucially -- how far a previous run
got, so a crash partway through a 50,000-product import resumes instead of restarting.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

#: Statuses that still occupy the shop's concurrency slot for this operation type.
ACTIVE_STATUSES = ("Created", "Running")

#: Shopify's own status strings, mapped to ours.
SHOPIFY_STATUS_MAP = {
	"CREATED": "Created",
	"RUNNING": "Running",
	"COMPLETED": "Completed",
	"FAILED": "Failed",
	"CANCELED": "Cancelled",
	"CANCELING": "Running",
	"EXPIRED": "Failed",
}


class ShopifyBulkOperation(Document):
	@frappe.whitelist()
	def cancel_operation(self):
		"""Ask Shopify to stop this operation and free the shop's slot."""
		# Fires an outbound mutation at Shopify. Read permission must not be enough.
		self.check_permission("write")
		from shopify_integration.api import bulk

		if self.status not in ACTIVE_STATUSES:
			frappe.throw(_("Only a running operation can be cancelled."))
		return bulk.cancel(self.name)


def active_operation(store: str, type: str = "query") -> str | None:
	"""Name of this store's in-flight operation of the given type, if any.

	The guard behind "one at a time": a second import must queue behind this rather than be
	rejected by Shopify with an error the user has to interpret.
	"""
	return frappe.db.get_value(
		"Shopify Bulk Operation",
		{"store": store, "type": type, "status": ["in", ACTIVE_STATUSES]},
		"name",
	)
