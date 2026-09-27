"""Shopify Event Log -- one row per received webhook (spec §5.6)."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class ShopifyEventLog(Document):
	@frappe.whitelist()
	def retry(self):
		"""Re-run the handler against the stored payload (spec §5.6).

		Replays the body exactly as Shopify sent it, which is the whole reason the raw body
		is stored rather than our parse of it. Handlers are idempotent, so replaying an event
		that partly succeeded is safe.
		"""
		# Frappe gates whitelisted *document* methods on read permission alone, and this one
		# enqueues a handler that runs as Administrator and writes Sales Orders, Invoices and
		# Customers from the stored payload. Read must not be enough to do that.
		self.check_permission("write")
		from shopify_integration.inbound.webhook import TOPIC_HANDLERS, run_handler

		handler = TOPIC_HANDLERS.get(self.topic)
		if not handler:
			frappe.throw(_("No handler is registered for topic '{0}'.").format(self.topic))

		self.db_set("status", "Queued")
		self.db_set("traceback", None)
		frappe.enqueue(
			run_handler,
			queue="default",
			job_id=f"shopify_webhook_retry::{self.name}",
			deduplicate=True,
			enqueue_after_commit=True,
			event_log=self.name,
			handler=handler,
		)
		return {"status": "Queued"}

	#: ``ref_docname`` is a Data column, so 140 characters is a hard limit rather than a
	#: preference. Cancelling a paid order produces payment entries, a credit note, an invoice
	#: and the order itself; joined, those overflowed it, and the insert failed *after* every
	#: cancellation had been committed -- so the work was done and the log said Error.
	MAX_REF_DOCNAME = 140

	def mark_success(
		self,
		ref_doctype: str | None = None,
		ref_docname: str | None = None,
		result: str | None = None,
	):
		"""Record the success. ``result`` carries detail too long for ``ref_docname``."""
		if ref_docname and len(ref_docname) > self.MAX_REF_DOCNAME:
			# Never let bookkeeping undo real work. Truncating loses a label; raising here
			# loses the record that anything happened at all.
			result = result or ref_docname
			ref_docname = ref_docname[: self.MAX_REF_DOCNAME]

		self.db_set(
			{
				"status": "Success",
				"processed_on": now_datetime(),
				"ref_doctype": ref_doctype,
				"ref_docname": ref_docname,
				"result": result,
				"traceback": None,
			}
		)

	def mark_skipped(self, reason: str):
		"""Nothing to do, and that is the right answer.

		Distinct from Success, which implies documents were written, and from Error, which
		asks an operator to look. A skipped event is one whose work another event already did
		or which never applied -- noise on the dashboard if it were flagged either way.
		"""
		self.db_set(
			{
				"status": "Skipped",
				"processed_on": now_datetime(),
				"result": reason,
				"traceback": None,
			}
		)

	def mark_error(self, traceback: str):
		"""Record a failure so it survives the rollback that follows.

		Handlers re-raise after calling this, so the worker fails the job and Frappe rolls the
		transaction back -- taking the error record with it. The event would then sit at Queued
		for ever with an empty traceback, which is indistinguishable from a job that never ran
		and is the hardest possible thing to diagnose.

		The rollback comes first, deliberately: it discards whatever the failed handler managed
		to write before it broke, so the commit that follows persists the error record and
		nothing else.
		"""
		frappe.db.rollback()
		self.db_set({"status": "Error", "processed_on": now_datetime(), "traceback": traceback})
		frappe.db.commit()
