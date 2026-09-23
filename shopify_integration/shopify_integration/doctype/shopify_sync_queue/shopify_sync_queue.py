"""Shopify Sync Queue -- the outbox row (spec §5.5)."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class ShopifySyncQueue(Document):
	def before_save(self):
		self.sync_pending_dedupe_key()

	def sync_pending_dedupe_key(self):
		"""Hold the invariant that backs the coalescing guarantee.

		``pending_dedupe_key`` mirrors ``dedupe_key`` while the row is Pending and is NULL
		otherwise. A unique index on a nullable column ignores NULLs in both MariaDB and
		Postgres, which is how we get 'unique on dedupe_key WHERE state = Pending' on a
		database that has no partial indexes.
		"""
		self.pending_dedupe_key = self.dedupe_key if self.state == "Pending" else None

	@frappe.whitelist()
	def requeue(self):
		"""Put a Failed row back in the queue (spec §14).

		If a newer Pending row already claims this dedupe_key, that row will push the same
		current state -- so this one is Superseded rather than requeued. Retrying it anyway
		would trip the unique index and surface a confusing database error to the user.
		"""
		# Saves with ignore_permissions and schedules a drain -- not a read-only action.
		self.check_permission("write")
		if self.state not in ("Failed", "Superseded"):
			frappe.throw(_("Only Failed or Superseded rows can be requeued."))

		conflict = frappe.db.exists(
			"Shopify Sync Queue",
			{"pending_dedupe_key": self.dedupe_key, "name": ["!=", self.name]},
		)
		if conflict:
			self.state = "Superseded"
			self.last_error = _("A newer pending row already covers this key: {0}").format(conflict)
			self.save(ignore_permissions=True)
			return {"state": self.state, "superseded_by": conflict}

		self.state = "Pending"
		self.attempts = 0
		self.next_attempt_at = now_datetime()
		self.last_error = None
		self.save(ignore_permissions=True)

		from shopify_integration.sync.engine import schedule_drain

		schedule_drain(self.store)
		return {"state": self.state}
