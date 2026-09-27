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
from frappe.utils import flt

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


class TestTheRealOrder1129(FrappeTestCase):
	"""The actual shape: three invoiced, none shipped, one refunded.

	Built out of real ERPNext documents rather than mocks, because the thing worth knowing is
	what ERPNext does, not what a stub says it does.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from shopify_integration.tests.test_integration import SECRET_A, make_store

		cls.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)
		cls.store_doc = frappe.get_cached_doc("Shopify Store", cls.store)
		cls.company = cls.store_doc.company
		cls.warehouse = frappe.db.get_value("Warehouse", {"company": cls.company, "is_group": 0}, "name")
		cls.customer = frappe.get_all("Customer", limit=1, pluck="name")[0]

	def setUp(self):
		self.made = []

	def tearDown(self):
		for doctype, name in reversed(self.made):
			try:
				doc = frappe.get_doc(doctype, name)
				if doc.docstatus == 1:
					doc.cancel()
			except Exception:
				pass
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_missing=True)
		frappe.db.commit()

	def _reserved(self):
		frappe.db.rollback()
		return flt(
			frappe.db.get_value("Bin", {"item_code": "ORD-TEE", "warehouse": self.warehouse}, "reserved_qty")
		)

	def _order(self, gid, qty):
		doc = frappe.new_doc("Sales Order")
		doc.customer = self.customer
		doc.company = self.company
		doc.currency = frappe.get_cached_value("Company", self.company, "default_currency")
		doc.conversion_rate = 1
		doc.transaction_date = frappe.utils.nowdate()
		doc.delivery_date = frappe.utils.add_days(frappe.utils.nowdate(), 3)
		doc.shopify_store = self.store
		doc.shopify_order_gid = gid
		doc.append(
			"items",
			{
				"item_code": "ORD-TEE",
				"qty": qty,
				"rate": 400,
				"warehouse": self.warehouse,
				"delivery_date": doc.delivery_date,
			},
		)
		doc.insert(ignore_permissions=True)
		doc.submit()
		self.made.append(("Sales Order", doc.name))
		frappe.db.commit()
		return doc.name

	def _invoice(self, so_name):
		from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

		doc = make_sales_invoice(so_name, ignore_permissions=True)
		doc.update_stock = 0
		doc.insert(ignore_permissions=True)
		doc.submit()
		self.made.append(("Sales Invoice", doc.name))
		frappe.db.commit()
		return doc.name

	def _payload(self, gid, remaining, unfulfilled=None):
		return {
			"id": f"gid://shopify/Refund/{gid.rsplit('/', 1)[-1]}",
			"createdAt": "2026-09-27T10:00:00Z",
			"order": {
				"id": gid,
				"name": "#1129",
				"lineItems": {
					"nodes": [
						{
							"id": "L1",
							"sku": "ORD-TEE",
							"quantity": 3,
							"currentQuantity": remaining,
							"unfulfilledQuantity": remaining if unfulfilled is None else unfulfilled,
						}
					]
				},
			},
		}

	def test_a_partial_refund_of_a_fully_invoiced_order_does_not_raise(self):
		"""The regression: this used to take the credit note and payment down with it."""
		gid = "gid://shopify/Order/1129A"
		base = self._reserved()
		name = self._order(gid, 3)
		self._invoice(name)

		settled, warning = _settle_safely(self.store_doc, self._payload(gid, 2))
		frappe.db.commit()

		self.assertIsNone(settled, "the line cannot be shortened once it is billed in full")
		self.assertIsNotNone(warning, "and that has to be said out loud, not swallowed")
		self.assertIn("reserving stock", warning)
		self.assertEqual(frappe.db.get_value("Sales Order", name, "docstatus"), 1, "the order survives")
		self.assertEqual(self._reserved(), base + 3, "still reserved, which the warning explains")

	def test_a_partial_refund_of_an_uninvoiced_order_does_shrink(self):
		"""Nothing billed, so ERPNext allows it and the reservation follows."""
		gid = "gid://shopify/Order/1129B"
		base = self._reserved()
		name = self._order(gid, 3)

		settled, warning = _settle_safely(self.store_doc, self._payload(gid, 2))
		frappe.db.commit()

		self.assertEqual(settled, name)
		self.assertIsNone(warning)
		self.assertEqual(frappe.get_doc("Sales Order", name).items[0].qty, 2)
		self.assertEqual(self._reserved(), base + 2)

	def test_a_full_refund_of_a_fully_invoiced_order_still_closes_it(self):
		"""Closing needs no quantity change, so being fully billed does not block it."""
		gid = "gid://shopify/Order/1129C"
		base = self._reserved()
		name = self._order(gid, 3)
		self._invoice(name)

		settled, warning = _settle_safely(self.store_doc, self._payload(gid, 0))
		frappe.db.commit()

		self.assertEqual(settled, name)
		self.assertIsNone(warning)
		self.assertEqual(frappe.db.get_value("Sales Order", name, "status"), "Closed")
		self.assertEqual(self._reserved(), base, "closing releases the whole reservation")

	def test_a_credit_note_does_not_reduce_the_orders_billed_amount(self):
		"""Pinning the ERPNext behaviour the fix is built around.

		If a future version starts reducing billed_amt for a return, this fails and the
		fallback above can be replaced by a real shrink.
		"""
		from erpnext.controllers.sales_and_purchase_return import make_return_doc

		gid = "gid://shopify/Order/1129D"
		name = self._order(gid, 3)
		invoice = self._invoice(name)
		self.assertEqual(frappe.get_doc("Sales Order", name).items[0].billed_amt, 1200)

		credit = make_return_doc("Sales Invoice", invoice)
		credit.update_stock = 0
		row = credit.items[0]
		row.qty = -1
		row.stock_qty = -1 * (row.conversion_factor or 1)
		credit.set("items", [])
		appended = credit.append("items", row.as_dict())
		appended.sales_order = row.get("sales_order")
		appended.so_detail = row.get("so_detail")
		credit.insert(ignore_permissions=True)
		credit.submit()
		self.made.append(("Sales Invoice", credit.name))
		frappe.db.commit()

		self.assertTrue(credit.items[0].so_detail, "the link is set, so this is not a wiring gap")
		self.assertEqual(
			frappe.get_doc("Sales Order", name).items[0].billed_amt,
			1200,
			"ERPNext v15 does not credit the order back; this is why the shrink cannot work",
		)
