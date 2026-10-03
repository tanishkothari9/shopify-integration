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

	def _cancel_documents_too(self):
		"""Opt this store back into tearing the books up, as a merchant may."""
		frappe.db.set_value("Shopify Store", self.store, "cancel_invoiced_orders", 1)
		frappe.db.commit()
		self.addCleanup(frappe.db.commit)
		self.addCleanup(
			frappe.db.set_value, "Shopify Store", self.store, "cancel_invoiced_orders", 0
		)

	def test_a_cancel_of_an_order_closed_with_money_still_held_still_raises(self):
		"""Only for a store that asked for the documents to be cancelled.

		The throw exists to stop the app guessing at an order somebody closed by hand while
		the money was still held. With the invoice left standing there is nothing to guess:
		closing the order and saying so is the whole of the work.
		"""
		self._cancel_documents_too()
		gid = "gid://shopify/Order/9402"
		self._sales_order(gid, close=True)
		invoice = self._invoice(gid)
		event = self._event(gid)

		with self.assertRaises(frappe.ValidationError) as caught:
			self._run_cancel(event, gid)

		self.assertIn("has not been credited back", str(caught.exception))
		self.assertEqual(frappe.db.get_value("Shopify Event Log", event, "status"), "Error")
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoice, "docstatus"), 1)

	def test_a_store_that_asks_for_it_still_cancels_everything(self):
		self._cancel_documents_too()
		gid = "gid://shopify/Order/9403"
		name = self._sales_order(gid)
		invoice = self._invoice(gid)
		event = self._event(gid)

		result = self._run_cancel(event, gid)

		self.assertNotIn("skipped", result)
		self.assertEqual(frappe.db.get_value("Shopify Event Log", event, "status"), "Success")
		self.assertEqual(frappe.db.get_value("Sales Order", name, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoice, "docstatus"), 2)

	def test_an_order_with_no_invoice_is_still_cancelled_outright(self):
		"""Nothing was booked, so there is no money to protect."""
		gid = "gid://shopify/Order/9404"
		name = self._sales_order(gid)
		event = self._event(gid)

		self._run_cancel(event, gid)

		self.assertEqual(frappe.db.get_value("Sales Order", name, "docstatus"), 2)


class TestCancellingAPaidOrderKeepsTheMoney(TestTheCancelHandlerItself):
	"""Order #1002, 3 October: cancelled with "Refund later", and ERPNext lost the receipt.

	Shopify's cancel dialog offers "Refund later". Choosing it cancels the order and keeps
	the customer's money, and the app read that as permission to cancel the Payment Entry
	and the Sales Invoice behind it -- so money genuinely received had no record at all.
	"""

	def _payment(self, invoice):
		company = self._company()
		doc = frappe.new_doc("Payment Entry")
		doc.payment_type = "Receive"
		doc.company = company
		doc.party_type = "Customer"
		doc.party = self.customer
		doc.paid_from = frappe.db.get_value(
			"Company", company, "default_receivable_account"
		) or frappe.db.get_value("Account", {"company": company, "account_type": "Receivable", "is_group": 0}, "name")
		doc.paid_to = frappe.db.get_value(
			"Account", {"company": company, "account_type": "Bank", "is_group": 0}, "name"
		) or frappe.db.get_value("Account", {"company": company, "account_type": "Cash", "is_group": 0}, "name")
		total = frappe.db.get_value("Sales Invoice", invoice, "base_grand_total")
		doc.paid_amount = doc.received_amount = total
		doc.source_exchange_rate = doc.target_exchange_rate = 1
		doc.reference_no, doc.reference_date = "SHOPIFY-TEST", frappe.utils.nowdate()
		doc.append("references", {
			"reference_doctype": "Sales Invoice", "reference_name": invoice,
			"total_amount": total, "outstanding_amount": total, "allocated_amount": total,
		})
		doc.flags.ignore_mandatory = True
		doc.insert(ignore_permissions=True)
		doc.submit()
		self.made.append(("Payment Entry", doc.name))
		frappe.db.commit()
		return doc.name

	def test_the_invoice_and_the_payment_survive(self):
		gid = "gid://shopify/Order/1002"
		name = self._sales_order(gid)
		invoice = self._invoice(gid)
		payment = self._payment(invoice)
		event = self._event(gid)

		self._run_cancel(event, gid, order_name="#1002")

		self.assertEqual(
			frappe.db.get_value("Sales Invoice", invoice, "docstatus"), 1, "the invoice was torn up"
		)
		self.assertEqual(
			frappe.db.get_value("Payment Entry", payment, "docstatus"),
			1,
			"money the shop actually received lost its only record",
		)

	def test_the_order_is_closed_so_the_stock_goes_back_on_sale(self):
		gid = "gid://shopify/Order/1003"
		name = self._sales_order(gid)
		self._invoice(gid)
		event = self._event(gid)

		result = self._run_cancel(event, gid, order_name="#1003")

		self.assertEqual(result["closed"], name)
		self.assertEqual(frappe.db.get_value("Sales Order", name, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("Sales Order", name, "status"), "Closed")

	def test_money_still_held_is_reported_where_someone_will_see_it(self):
		gid = "gid://shopify/Order/1004"
		self._sales_order(gid)
		self._invoice(gid, rate=250)
		event = self._event(gid)

		before = frappe.db.count("Error Log")
		result = self._run_cancel(event, gid, order_name="#1004")

		self.assertGreater(result["unrefunded"], 0)
		self.assertGreater(frappe.db.count("Error Log"), before, "nothing was raised for a person")
		self.assertIn("Refund pending", frappe.db.get_value("Shopify Event Log", event, "result"))

	def test_an_open_order_whose_money_is_already_back_warns_about_nothing(self):
		"""Refunded but never closed: close it, and say nothing about money owed."""
		gid = "gid://shopify/Order/1005"
		name = self._sales_order(gid)
		invoice = self._invoice(gid)
		self._invoice(gid, is_return=True, against=invoice)
		event = self._event(gid)

		result = self._run_cancel(event, gid, order_name="#1005")

		self.assertEqual(result["closed"], name)
		self.assertEqual(result["unrefunded"], 0)
		self.assertNotIn("Refund pending", frappe.db.get_value("Shopify Event Log", event, "result"))

	def test_a_refund_arriving_after_the_cancel_still_credits_the_invoice(self):
		"""The case the old behaviour made impossible: cancel and refund together.

		Cancelling used to cancel the invoice, so the refund that followed a moment later
		had nothing left to credit. With the invoice standing, the refund books against it
		exactly as it does for a return.
		"""
		gid = "gid://shopify/Order/1006"
		self._sales_order(gid)
		invoice = self._invoice(gid)
		event = self._event(gid)

		self._run_cancel(event, gid, order_name="#1006")
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoice, "docstatus"), 1)

		# What the refund handler would do next: a credit note against a live invoice.
		credit = self._invoice(gid, is_return=True, against=invoice)
		self.assertEqual(frappe.db.get_value("Sales Invoice", credit, "docstatus"), 1)
		self.assertEqual(
			frappe.db.get_value("Sales Invoice", credit, "return_against"),
			invoice,
			"the credit note had nothing to point at",
		)

	def test_the_refund_is_not_skipped_as_already_cancelled(self):
		"""The exact guard that used to swallow a refund arriving with its cancellation.

		`_order_already_cancelled` reports True when every document for the order is
		cancelled, and the refund handler then does nothing -- which was right while the
		cancellation tore the invoice up, because there was genuinely nothing left to
		credit. With the invoice standing it must report False, or the refund would be
		silently dropped and the customer's money would never come back in the books.
		"""
		from shopify_integration.inbound.refund import _order_already_cancelled

		gid = "gid://shopify/Order/1007"
		self._sales_order(gid)
		invoice = self._invoice(gid)
		event = self._event(gid)

		self._run_cancel(event, gid, order_name="#1007")

		self.assertEqual(frappe.db.get_value("Sales Invoice", invoice, "docstatus"), 1)
		self.assertFalse(
			_order_already_cancelled(self.store, gid),
			"the refund would have been skipped and the money never credited back",
		)

	def test_a_store_that_cancels_everything_does_skip_the_refund(self):
		"""The other half: that behaviour is still right when the documents really are gone."""
		from shopify_integration.inbound.refund import _order_already_cancelled

		self._cancel_documents_too()
		gid = "gid://shopify/Order/1008"
		self._sales_order(gid)
		self._invoice(gid)
		event = self._event(gid)

		self._run_cancel(event, gid, order_name="#1008")

		self.assertTrue(_order_already_cancelled(self.store, gid))
