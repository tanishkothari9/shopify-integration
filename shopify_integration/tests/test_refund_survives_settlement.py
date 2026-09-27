"""Settling the Sales Order must never undo the refund that prompted it.

Order #1129: three earrings, invoiced in full, not shipped. Refund one, and shrinking the
Sales Order line hits ERPNext's "Cannot set Rate if the billed amount is greater than the
amount for Item" -- the line is billed for all three. That exception rolled back the whole
handler, so a refund Shopify had genuinely made left no credit note, no payment, and an Error.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.inbound import refund as refund_module
from shopify_integration.inbound.refund import _settle_safely


class TestSettlingIsIsolated(FrappeTestCase):
	"""Whatever ERPNext refuses, the money side stays."""

	def test_a_refused_settlement_is_a_warning_not_a_failure(self):
		with patch.object(
			refund_module,
			"settle_after_refund",
			side_effect=frappe.ValidationError(
				"Row #1: Cannot set Rate if the billed amount is greater than the amount for Item"
			),
		):
			settled, warning = _settle_safely(object(), {"id": "gid://shopify/Refund/1"})

		self.assertIsNone(settled)
		self.assertIn("could not be brought in line", warning)
		self.assertIn("billed amount", warning, "the reason has to reach whoever reads it")

	def test_a_successful_settlement_reports_the_order_and_no_warning(self):
		with patch.object(refund_module, "settle_after_refund", return_value="SAL-ORD-2026-00001"):
			settled, warning = _settle_safely(object(), {"id": "gid://shopify/Refund/1"})

		self.assertEqual(settled, "SAL-ORD-2026-00001")
		self.assertIsNone(warning)

	def test_the_savepoint_is_released_on_the_way_out(self):
		"""A `return` inside the `try` would skip an `else`, and the savepoint would leak."""
		with (
			patch.object(refund_module, "settle_after_refund", return_value="SO-1"),
			patch.object(frappe.db, "release_savepoint") as released,
		):
			_settle_safely(object(), {"id": "gid://shopify/Refund/1"})
		released.assert_called_once()

	def test_a_failure_rolls_back_only_to_the_savepoint(self):
		with (
			patch.object(refund_module, "settle_after_refund", side_effect=RuntimeError("nope")),
			patch.object(frappe.db, "rollback") as rolled_back,
		):
			_settle_safely(object(), {"id": "gid://shopify/Refund/1"})

		rolled_back.assert_called_once()
		self.assertEqual(
			rolled_back.call_args.kwargs.get("save_point"),
			"shopify_settle_after_refund",
			"a bare rollback here would throw away the credit note too",
		)


class TestTheRefundIsStillRecorded(FrappeTestCase):
	"""The whole point: the credit note and payment survive a refused settlement."""

	def test_create_credit_note_returns_the_documents_and_the_warning(self):
		calls = {}

		def fake_build(store_doc, refund, invoice_name, quantities, side):
			calls["credit_note"] = "SRET-26-00001"
			return "SRET-26-00001"

		with (
			patch.object(refund_module, "_original_invoice", return_value="SINV-26-00001"),
			patch.object(refund_module, "refund_quantities", return_value={"EAR": {"qty": 1}}),
			patch.object(refund_module, "_side_for", return_value="shopMoney"),
			patch.object(refund_module, "_refunded_nothing", return_value=False),
			patch.object(refund_module, "_build_return_invoice", side_effect=fake_build),
			patch.object(refund_module, "_restock", return_value=None),
			patch.object(refund_module, "_reverse_payment", return_value="ACC-PAY-0001"),
			patch.object(
				refund_module,
				"settle_after_refund",
				side_effect=frappe.ValidationError("billed amount is greater than the amount"),
			),
		):
			result = refund_module.create_credit_note(
				frappe._dict({"name": "Test Store A", "company": "X", "sync_refunds": 1}),
				{"id": "gid://shopify/Refund/1129", "order": {"id": "gid://shopify/Order/1129"}},
			)

		self.assertEqual(result["credit_note"], "SRET-26-00001", "the credit note must survive")
		self.assertEqual(result["payment_entry"], "ACC-PAY-0001", "and so must the refund payment")
		self.assertIsNone(result["closed_sales_order"])
		self.assertIn("warning", result, "and the failure has to be reported, not swallowed")


class TestTheCreditNoteKeepsItsSalesOrderLink(FrappeTestCase):
	"""Without sales_order / so_detail on the rows, ERPNext never lowers the order's billed
	amount for the refunded quantity -- and the line can then never be shortened."""

	def test_the_rebuild_carries_the_link(self):
		import inspect

		source = inspect.getsource(refund_module._build_return_invoice)
		self.assertIn("appended.so_detail", source)
		self.assertIn("appended.sales_order", source)

	def test_the_shrink_falls_back_instead_of_raising(self):
		import inspect

		source = inspect.getsource(refund_module._shrink_to_what_is_owed)
		self.assertIn("rollback", source, "a refused shrink must not poison the transaction")
		self.assertIn("release_savepoint", source)
