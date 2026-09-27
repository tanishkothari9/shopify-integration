"""Cancelling a paid order in Shopify sends two webhooks at once, and either can win.

orders/cancelled and refunds/create arrive at the same moment. Whichever is handled second
finds the work already done, and used to report that as a failure.

  cancel first  -> the refund finds no invoice to credit          (handled in c25fed8)
  refund first  -> the cancel finds a Closed Sales Order          (this)

Either way the books end up right. Only the log was wrong.
"""

from __future__ import annotations

import frappe

from shopify_integration.inbound.order import _closed_sales_order, _fully_refunded
from shopify_integration.tests.test_orders import OrderTestCase


class TestRecognisingTheSettledState(OrderTestCase):
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

	def _company(self):
		return frappe.db.get_value("Shopify Store", self.store, "company")

	def _sales_order(self, gid, close=False):
		company = self._company()
		doc = frappe.new_doc("Sales Order")
		doc.customer = self.customer
		doc.company = company
		doc.currency = frappe.get_cached_value("Company", company, "default_currency")
		doc.conversion_rate = 1
		doc.transaction_date = frappe.utils.nowdate()
		doc.delivery_date = frappe.utils.add_days(frappe.utils.nowdate(), 3)
		doc.shopify_store = self.store
		doc.shopify_order_gid = gid
		doc.append(
			"items",
			{
				"item_code": "ORD-TEE",
				"qty": 1,
				"rate": 100,
				"warehouse": self.warehouse,
				"delivery_date": doc.delivery_date,
			},
		)
		doc.insert(ignore_permissions=True)
		doc.submit()
		self.made.append(("Sales Order", doc.name))
		if close:
			frappe.clear_document_cache("Sales Order", doc.name)
			frappe.get_doc("Sales Order", doc.name).update_status("Closed")
		frappe.db.commit()
		return doc.name

	def _invoice(self, gid, rate=100, is_return=False, against=None):
		company = self._company()
		doc = frappe.new_doc("Sales Invoice")
		doc.customer = self.customer
		doc.company = company
		doc.currency = frappe.get_cached_value("Company", company, "default_currency")
		doc.conversion_rate = 1
		doc.shopify_store = self.store
		doc.shopify_order_gid = gid
		doc.update_stock = 0
		if is_return:
			doc.is_return = 1
			doc.return_against = against
		doc.append(
			"items",
			{
				"item_code": "ORD-TEE",
				"qty": -1 if is_return else 1,
				"rate": rate,
				"warehouse": self.warehouse,
			},
		)
		doc.insert(ignore_permissions=True)
		doc.submit()
		self.made.append(("Sales Invoice", doc.name))
		frappe.db.commit()
		return doc.name

	# -- the state machine ----------------------------------------------------------

	def test_an_open_order_is_not_reported_as_closed(self):
		gid = "gid://shopify/Order/9301"
		self._sales_order(gid)
		self.assertIsNone(_closed_sales_order(self.store, gid))

	def test_a_closed_order_is_found(self):
		gid = "gid://shopify/Order/9302"
		name = self._sales_order(gid, close=True)
		self.assertEqual(_closed_sales_order(self.store, name and gid), name)

	def test_an_order_this_site_does_not_have_is_not_closed(self):
		self.assertIsNone(_closed_sales_order(self.store, "gid://shopify/Order/never-seen"))
		self.assertIsNone(_closed_sales_order(self.store, None))

	def test_an_invoice_with_a_matching_credit_note_is_fully_refunded(self):
		gid = "gid://shopify/Order/9303"
		self._sales_order(gid)
		invoice = self._invoice(gid)
		self._invoice(gid, is_return=True, against=invoice)
		self.assertTrue(_fully_refunded(self.store, gid))

	def test_an_invoice_with_no_credit_note_is_not_fully_refunded(self):
		"""Closed by hand while the customer's money is still held."""
		gid = "gid://shopify/Order/9304"
		self._sales_order(gid)
		self._invoice(gid)
		self.assertFalse(_fully_refunded(self.store, gid))

	def test_an_order_with_no_invoice_at_all_is_not_fully_refunded(self):
		"""Nothing was ever charged, so nothing was refunded. It must not be swept up."""
		gid = "gid://shopify/Order/9305"
		self._sales_order(gid)
		self.assertFalse(_fully_refunded(self.store, gid))

	def test_a_partial_credit_note_is_not_fully_refunded(self):
		gid = "gid://shopify/Order/9306"
		self._sales_order(gid)
		invoice = self._invoice(gid, rate=100)
		self._invoice(gid, rate=40, is_return=True, against=invoice)
		self.assertFalse(_fully_refunded(self.store, gid))

	# -- the two orderings ----------------------------------------------------------

	def test_refund_first_then_cancel_is_a_no_op(self):
		"""The regression. The refund closed the order and credited the invoice; the cancel
		then found a Closed Sales Order and raised 'Sales Order ... is Closed'."""
		gid = "gid://shopify/Order/9307"
		name = self._sales_order(gid, close=True)
		invoice = self._invoice(gid)
		credit = self._invoice(gid, is_return=True, against=invoice)

		self.assertEqual(_closed_sales_order(self.store, gid), name)
		self.assertTrue(_fully_refunded(self.store, gid))

		# nothing is torn up: invoice and credit note are the record of a sale and its refund
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoice, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Sales Invoice", credit, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Sales Order", name, "status"), "Closed")

	def test_closed_by_hand_with_money_still_held_is_not_swept_up(self):
		"""Must keep raising: a person has to decide what happened."""
		gid = "gid://shopify/Order/9308"
		name = self._sales_order(gid, close=True)
		self._invoice(gid)

		self.assertEqual(_closed_sales_order(self.store, gid), name)
		self.assertFalse(_fully_refunded(self.store, gid))

	def test_an_ordinary_open_order_still_goes_down_the_cancel_path(self):
		"""Cancel-first is unchanged: no Closed order, so nothing short-circuits."""
		gid = "gid://shopify/Order/9309"
		self._sales_order(gid)
		self._invoice(gid)
		self.assertIsNone(_closed_sales_order(self.store, gid))


class TestTheCancelHandlerItself(TestRecognisingTheSettledState):
	"""Not just the helpers -- what the webhook handler records."""

	def _event(self, gid):
		doc = frappe.new_doc("Shopify Event Log")
		doc.store = self.store
		doc.topic = "orders/cancelled"
		doc.webhook_id = frappe.generate_hash(length=20)
		doc.payload = frappe.as_json({"admin_graphql_api_id": gid})
		doc.status = "Queued"
		doc.insert(ignore_permissions=True)
		self.made.append(("Shopify Event Log", doc.name))
		frappe.db.commit()
		return doc.name

	def _run_cancel(self, event, gid, order_name="#1098"):
		from unittest.mock import patch

		from shopify_integration.inbound import order as order_module

		with patch.object(order_module, "fetch_order", return_value={"id": gid, "name": order_name}):
			return order_module.on_order_cancelled(event)

	def test_a_cancel_after_a_full_refund_is_skipped_not_failed(self):
		gid = "gid://shopify/Order/9401"
		name = self._sales_order(gid, close=True)
		invoice = self._invoice(gid)
		credit = self._invoice(gid, is_return=True, against=invoice)
		event = self._event(gid)

		result = self._run_cancel(event, gid)

		self.assertIn("already fully refunded", result.get("skipped", ""))
		saved = frappe.db.get_value("Shopify Event Log", event, ["status", "result"], as_dict=True)
		self.assertEqual(saved.status, "Skipped")
		self.assertIn("nothing to cancel", saved.result)

		# and nothing was torn up
		self.assertEqual(frappe.db.get_value("Sales Order", name, "status"), "Closed")
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoice, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Sales Invoice", credit, "docstatus"), 1)

	def test_a_cancel_of_an_order_closed_with_money_still_held_still_raises(self):
		gid = "gid://shopify/Order/9402"
		self._sales_order(gid, close=True)
		invoice = self._invoice(gid)
		event = self._event(gid)

		with self.assertRaises(frappe.ValidationError) as caught:
			self._run_cancel(event, gid)

		self.assertIn("has not been credited back", str(caught.exception))
		self.assertEqual(frappe.db.get_value("Shopify Event Log", event, "status"), "Error")
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoice, "docstatus"), 1)

	def test_an_ordinary_cancel_still_cancels_everything(self):
		"""Cancel-first is unchanged: the order is open, so nothing short-circuits."""
		gid = "gid://shopify/Order/9403"
		name = self._sales_order(gid)
		invoice = self._invoice(gid)
		event = self._event(gid)

		result = self._run_cancel(event, gid)

		self.assertNotIn("skipped", result)
		self.assertEqual(frappe.db.get_value("Shopify Event Log", event, "status"), "Success")
		self.assertEqual(frappe.db.get_value("Sales Order", name, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoice, "docstatus"), 2)
