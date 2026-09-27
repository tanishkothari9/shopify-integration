"""A fully refunded order must stop reserving stock, and the cancel log must fit its column.

Both were found on live orders. A refunded order that goes back to "To Deliver" holds a unit
back from the website for ever and invites a second shipment; a cancellation that overflows
`ref_docname` marks a finished job as Error.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from shopify_integration.inbound.refund import owed_by_sku, settle_after_refund
from shopify_integration.tests.test_orders import OrderTestCase


def refund_payload(order_gid, lines, name="#1075"):
	"""A refund in the shape refund_by_id.graphql returns.

	`lines` is [(sku, ordered, remaining)] or [(sku, ordered, remaining, unfulfilled)].
	`remaining` is Shopify's currentQuantity -- what is left on the line after every refund
	against the order, not just this one -- and `unfulfilled` is what has not shipped,
	defaulting to the same so the common cases read short.
	"""
	nodes = []
	for i, line in enumerate(lines, start=1):
		sku, ordered, remaining = line[0], line[1], line[2]
		unfulfilled = line[3] if len(line) > 3 else remaining
		nodes.append(
			{
				"id": f"gid://shopify/LineItem/{i}",
				"sku": sku,
				"quantity": ordered,
				"currentQuantity": remaining,
				"unfulfilledQuantity": unfulfilled,
			}
		)
	return {
		"id": "gid://shopify/Refund/1",
		"createdAt": "2026-09-26T20:22:49Z",
		"order": {"id": order_gid, "name": name, "lineItems": {"nodes": nodes}},
	}


class TestDecidingWhetherAnythingIsStillOwed(FrappeTestCase):
	"""The judgement, without touching a Sales Order."""

	class _Store:
		name = "Test Store A"

	def test_a_partly_refunded_order_is_left_open(self):
		"""One of two lines returned. The customer is still owed the other one."""
		refund = refund_payload("gid://shopify/Order/9001", [("TEE", 1, 0), ("MUG", 1, 1)])
		self.assertIsNone(settle_after_refund(self._Store(), refund))

	def test_a_partly_refunded_line_is_left_open(self):
		"""Two of three units back; one is still owed."""
		refund = refund_payload("gid://shopify/Order/9002", [("TEE", 3, 1)])
		self.assertIsNone(settle_after_refund(self._Store(), refund))

	def test_a_refund_with_no_line_data_changes_nothing(self):
		"""Shipping-only refunds and older payloads. Leaving it open is the safe half of the
		guess: a stuck reservation is visible and fixable, closing an order that still owes
		goods is not."""
		refund = {"id": "r", "order": {"id": "gid://shopify/Order/9003", "lineItems": {"nodes": []}}}
		self.assertIsNone(settle_after_refund(self._Store(), refund))

	def test_a_refund_naming_no_order_changes_nothing(self):
		self.assertIsNone(settle_after_refund(self._Store(), {"id": "r", "order": {}}))
		self.assertIsNone(settle_after_refund(self._Store(), {"id": "r"}))

	def test_an_order_this_site_does_not_have_changes_nothing(self):
		refund = refund_payload("gid://shopify/Order/does-not-exist", [("TEE", 1, 0)])
		self.assertIsNone(settle_after_refund(self._Store(), refund))


class TestTheEventLogFitsItsColumn(FrappeTestCase):
	"""`ref_docname` is a Data column: 140 characters is a hard limit, not a preference."""

	def _log(self):
		doc = frappe.new_doc("Shopify Event Log")
		doc.store = frappe.get_all("Shopify Store", limit=1, pluck="name")[0]
		doc.topic = "orders/cancelled"
		doc.webhook_id = frappe.generate_hash(length=20)
		doc.payload = "{}"
		doc.status = "Queued"
		doc.insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Shopify Event Log", doc.name, force=True, ignore_permissions=True)
		return doc

	def test_a_long_ref_docname_does_not_raise(self):
		"""The regression: the insert failed *after* every cancellation had been committed, so
		the work was done and the log said Error."""
		log = self._log()
		log.mark_success(ref_doctype="Sales Order", ref_docname="X" * 300)
		saved = frappe.db.get_value(
			"Shopify Event Log", log.name, ["status", "ref_docname", "result"], as_dict=True
		)
		self.assertEqual(saved.status, "Success")
		self.assertLessEqual(len(saved.ref_docname), 140)

	def test_what_was_truncated_is_kept_in_result(self):
		log = self._log()
		long_list = ", ".join(f"ACC-PAY-2026-{n:05d}" for n in range(20))
		log.mark_success(ref_doctype="Sales Order", ref_docname=long_list)
		self.assertEqual(frappe.db.get_value("Shopify Event Log", log.name, "result"), long_list)

	def test_a_short_name_is_stored_untouched(self):
		log = self._log()
		log.mark_success(ref_doctype="Sales Order", ref_docname="SAL-ORD-2026-00001")
		saved = frappe.db.get_value("Shopify Event Log", log.name, ["ref_docname", "result"], as_dict=True)
		self.assertEqual(saved.ref_docname, "SAL-ORD-2026-00001")
		self.assertIsNone(saved.result)

	def test_the_full_list_goes_to_result_and_the_order_to_ref_docname(self):
		"""What the cancel handler now does: one linkable name, the rest as detail."""
		log = self._log()
		log.mark_success(
			ref_doctype="Sales Order",
			ref_docname="SAL-ORD-2026-00042",
			result="ACC-PAY-0001, SRET-26-0001, SINV-26-0001, SAL-ORD-2026-00042",
		)
		saved = frappe.db.get_value(
			"Shopify Event Log", log.name, ["status", "ref_docname", "result"], as_dict=True
		)
		self.assertEqual(saved.status, "Success")
		self.assertEqual(saved.ref_docname, "SAL-ORD-2026-00042")
		self.assertIn("SINV-26-0001", saved.result)


class TestClosingARealSalesOrder(OrderTestCase):
	"""The point of the whole thing: the reservation has to actually go away."""

	def setUp(self):
		self.made = []
		frappe.db.delete("Shopify Sync Queue", {"store": self.store})
		frappe.db.commit()

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

	def _reserved(self, item_code):
		return flt(
			frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": self.warehouse}, "reserved_qty")
		)

	def _sales_order(self, order_gid, qty=1, item_code="ORD-TEE"):
		doc = frappe.new_doc("Sales Order")
		doc.customer = self.customer
		doc.company = self.store_doc.company
		doc.currency = frappe.get_cached_value("Company", self.store_doc.company, "default_currency")
		doc.conversion_rate = 1
		doc.transaction_date = frappe.utils.nowdate()
		doc.delivery_date = frappe.utils.add_days(frappe.utils.nowdate(), 3)
		doc.shopify_store = self.store
		doc.shopify_order_gid = order_gid
		doc.append(
			"items",
			{
				"item_code": item_code,
				"qty": qty,
				"rate": 50,
				"warehouse": self.warehouse,
				"delivery_date": doc.delivery_date,
			},
		)
		doc.insert(ignore_permissions=True)
		doc.submit()
		self.made.append(("Sales Order", doc.name))
		frappe.db.commit()
		return doc.name

	# -- the regression -------------------------------------------------------------

	def test_a_fully_refunded_order_is_closed_and_stops_reserving(self):
		gid = "gid://shopify/Order/9101"
		before = self._reserved("ORD-TEE")
		name = self._sales_order(gid)

		self.assertGreater(self._reserved("ORD-TEE"), before, "the order should reserve while open")

		closed = settle_after_refund(self.store_doc, refund_payload(gid, [("ORD-TEE", 1, 0)]))
		frappe.db.commit()

		self.assertEqual(closed, name)
		self.assertEqual(frappe.db.get_value("Sales Order", name, "status"), "Closed")
		self.assertEqual(self._reserved("ORD-TEE"), before, "closing must release the reservation")

	def test_closing_queues_the_stock_back_to_shopify(self):
		"""Closing is neither a submit nor a cancel, so the reservation hook never fires. The
		freed unit would sit unsent until the nightly reconciliation noticed."""
		gid = "gid://shopify/Order/9102"
		self._sales_order(gid)

		# The shared store fixture has inventory sync off and no location map, so a push would
		# correctly be skipped. Turn it on for this one assertion.
		self.store_doc.sync_inventory = 1
		self.store_doc.set("location_map", [])
		self.store_doc.append(
			"location_map",
			{
				"location_gid": "gid://shopify/Location/1",
				"location_name": "Shop",
				"warehouse": self.warehouse,
			},
		)
		self.store_doc.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_document_cache("Shopify Store", self.store)
		self.addCleanup(self._restore_inventory_settings)

		frappe.db.delete("Shopify Sync Queue", {"store": self.store})
		frappe.db.commit()

		settle_after_refund(
			frappe.get_cached_doc("Shopify Store", self.store), refund_payload(gid, [("ORD-TEE", 1, 0)])
		)
		frappe.db.commit()

		self.assertTrue(
			frappe.db.count("Shopify Sync Queue", {"store": self.store, "operation": "inventory"}),
			"closing should queue an inventory push",
		)

	def _restore_inventory_settings(self):
		doc = frappe.get_doc("Shopify Store", self.store)
		doc.sync_inventory = 0
		doc.set("location_map", [])
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_document_cache("Shopify Store", self.store)

	def test_a_second_delivery_of_the_same_webhook_does_nothing(self):
		gid = "gid://shopify/Order/9103"
		name = self._sales_order(gid)
		refund = refund_payload(gid, [("ORD-TEE", 1, 0)])

		self.assertEqual(settle_after_refund(self.store_doc, refund), name)
		frappe.db.commit()
		self.assertIsNone(settle_after_refund(self.store_doc, refund), "must be idempotent")
		self.assertEqual(frappe.db.get_value("Sales Order", name, "status"), "Closed")

	def test_a_partly_refunded_unshipped_order_is_shrunk_to_what_is_owed(self):
		"""Three ordered, one refunded before anything shipped. The order stays open because
		two are still coming, but holding three reserves stock for a unit nobody will get."""
		gid = "gid://shopify/Order/9104"
		before = self._reserved("ORD-TEE")
		name = self._sales_order(gid, qty=3)
		self.assertEqual(self._reserved("ORD-TEE"), before + 3)

		self.assertEqual(settle_after_refund(self.store_doc, refund_payload(gid, [("ORD-TEE", 3, 2)])), name)
		frappe.db.commit()

		self.assertNotEqual(frappe.db.get_value("Sales Order", name, "status"), "Closed")
		self.assertEqual(frappe.get_doc("Sales Order", name).items[0].qty, 2)
		self.assertEqual(self._reserved("ORD-TEE"), before + 2, "only what is still owed")

	def test_a_delivered_order_partly_returned_is_closed(self):
		"""#1122: two delivered, one returned and refunded, one kept. currentQuantity is 1 and
		reads as "something is owed", but nothing is -- both shipped and one came back."""
		gid = "gid://shopify/Order/9107"
		before = self._reserved("ORD-TEE")
		name = self._sales_order(gid, qty=2)

		self.assertEqual(
			settle_after_refund(self.store_doc, refund_payload(gid, [("ORD-TEE", 2, 1, 0)])), name
		)
		frappe.db.commit()

		self.assertEqual(frappe.db.get_value("Sales Order", name, "status"), "Closed")
		self.assertEqual(self._reserved("ORD-TEE"), before, "the returned piece goes back on sale")

	def test_an_order_with_nothing_refunded_is_left_alone(self):
		gid = "gid://shopify/Order/9108"
		name = self._sales_order(gid, qty=2)
		reserved = self._reserved("ORD-TEE")

		self.assertIsNone(settle_after_refund(self.store_doc, refund_payload(gid, [("ORD-TEE", 2, 2)])))
		frappe.db.commit()
		self.assertEqual(frappe.get_doc("Sales Order", name).items[0].qty, 2)
		self.assertEqual(self._reserved("ORD-TEE"), reserved)

	def test_a_cancelled_order_is_left_alone(self):
		gid = "gid://shopify/Order/9105"
		name = self._sales_order(gid)
		frappe.get_doc("Sales Order", name).cancel()
		frappe.db.commit()

		self.assertIsNone(settle_after_refund(self.store_doc, refund_payload(gid, [("ORD-TEE", 1, 0)])))

	def test_another_stores_order_is_not_touched(self):
		"""Only Sales Orders this store owns."""
		gid = "gid://shopify/Order/9106"
		name = self._sales_order(gid)
		frappe.db.set_value("Sales Order", name, "shopify_store", "Test Store B", update_modified=False)
		frappe.db.commit()

		self.assertIsNone(settle_after_refund(self.store_doc, refund_payload(gid, [("ORD-TEE", 1, 0)])))
		self.assertNotEqual(frappe.db.get_value("Sales Order", name, "status"), "Closed")


class TestARefundOfAnAlreadyCancelledOrder(OrderTestCase):
	"""Shopify sends orders/cancelled and refunds/create at the same moment.

	If the cancellation is handled first it cancels the invoice, and the refund then finds
	nothing to credit -- correctly, because the cancellation already undid everything the
	credit note would have. Raising there marked a healthy final state as an error, on every
	cancellation of a paid order.
	"""

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

	def _cancelled_sales_order(self, order_gid):
		doc = frappe.new_doc("Sales Order")
		doc.customer = self.customer
		company = frappe.db.get_value("Shopify Store", self.store, "company")
		doc.company = company
		doc.currency = frappe.get_cached_value("Company", company, "default_currency")
		doc.conversion_rate = 1
		doc.transaction_date = frappe.utils.nowdate()
		doc.delivery_date = frappe.utils.add_days(frappe.utils.nowdate(), 3)
		doc.shopify_store = self.store
		doc.shopify_order_gid = order_gid
		doc.append(
			"items",
			{
				"item_code": "ORD-TEE",
				"qty": 1,
				"rate": 50,
				"warehouse": self.warehouse,
				"delivery_date": doc.delivery_date,
			},
		)
		doc.insert(ignore_permissions=True)
		doc.submit()
		self.made.append(("Sales Order", doc.name))
		doc.cancel()
		frappe.db.commit()
		return doc.name

	def test_the_order_is_recognised_as_already_cancelled(self):
		from shopify_integration.inbound.refund import _order_already_cancelled

		gid = "gid://shopify/Order/9201"
		self._cancelled_sales_order(gid)
		self.assertTrue(_order_already_cancelled(self.store, gid))

	def test_a_live_order_is_not_mistaken_for_a_cancelled_one(self):
		from shopify_integration.inbound.refund import _order_already_cancelled

		gid = "gid://shopify/Order/9202"
		doc = frappe.new_doc("Sales Order")
		doc.customer = self.customer
		company = frappe.db.get_value("Shopify Store", self.store, "company")
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
				"rate": 50,
				"warehouse": self.warehouse,
				"delivery_date": doc.delivery_date,
			},
		)
		doc.insert(ignore_permissions=True)
		doc.submit()
		self.made.append(("Sales Order", doc.name))
		frappe.db.commit()

		self.assertFalse(_order_already_cancelled(self.store, gid))

	def test_an_order_this_site_never_had_is_not_called_cancelled(self):
		"""The real gap -- invoice sync off, say -- must still raise, not be quietly skipped."""
		from shopify_integration.inbound.refund import _order_already_cancelled

		self.assertFalse(_order_already_cancelled(self.store, "gid://shopify/Order/never-seen"))
		self.assertFalse(_order_already_cancelled(self.store, None))

	def test_the_refund_is_skipped_rather_than_failed(self):
		from shopify_integration.inbound.refund import create_credit_note

		gid = "gid://shopify/Order/9203"
		self._cancelled_sales_order(gid)
		before = frappe.db.count("Sales Invoice")

		result = create_credit_note(
			frappe.get_cached_doc("Shopify Store", self.store),
			{
				"id": "gid://shopify/Refund/9203",
				"createdAt": "2026-09-26T20:22:49Z",
				"order": {"id": gid, "name": "#1090"},
				"totalRefundedSet": {
					"shopMoney": {"amount": "50.00"},
					"presentmentMoney": {"amount": "50.00"},
				},
				"refundLineItems": {
					"nodes": [
						{
							"quantity": 1,
							"restockType": "RETURN",
							"lineItem": {"id": "gid://shopify/LineItem/1", "sku": "ORD-TEE"},
						}
					]
				},
			},
		)

		self.assertIn("already cancelled", result.get("skipped", ""))
		self.assertEqual(frappe.db.count("Sales Invoice"), before, "nothing should be written")

	def test_a_refund_for_an_order_that_was_never_invoiced_still_raises(self):
		"""Invoice sync off is a real gap and must keep reporting itself."""
		from shopify_integration.inbound.refund import create_credit_note

		with self.assertRaises(frappe.ValidationError) as caught:
			create_credit_note(
				frappe.get_cached_doc("Shopify Store", self.store),
				{
					"id": "gid://shopify/Refund/9204",
					"createdAt": "2026-09-26T20:22:49Z",
					"order": {"id": "gid://shopify/Order/never-invoiced", "name": "#9204"},
					"totalRefundedSet": {
						"shopMoney": {"amount": "50.00"},
						"presentmentMoney": {"amount": "50.00"},
					},
					"refundLineItems": {
						"nodes": [
							{
								"quantity": 1,
								"restockType": "RETURN",
								"lineItem": {"id": "gid://shopify/LineItem/1", "sku": "ORD-TEE"},
							}
						]
					},
				},
			)
		self.assertIn("no Sales Invoice", str(caught.exception))


class TestTheLogCanSaySkipped(FrappeTestCase):
	def test_mark_skipped_records_the_reason(self):
		doc = frappe.new_doc("Shopify Event Log")
		doc.store = frappe.get_all("Shopify Store", limit=1, pluck="name")[0]
		doc.topic = "refunds/create"
		doc.webhook_id = frappe.generate_hash(length=20)
		doc.payload = "{}"
		doc.status = "Queued"
		doc.insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Shopify Event Log", doc.name, force=True, ignore_permissions=True)

		doc.mark_skipped("order already cancelled in ERPNext; nothing to credit")
		saved = frappe.db.get_value("Shopify Event Log", doc.name, ["status", "result"], as_dict=True)
		self.assertEqual(saved.status, "Skipped")
		self.assertIn("already cancelled", saved.result)


class TestCancellingAnUnpaidOrderWithRestock(FrappeTestCase):
	"""An unpaid order cancelled with restock makes Shopify write a zero-money refund.

	orders/cancelled and refunds/create arrive together and either can win. Arriving first,
	the refund finds no invoice -- because an unpaid order never had one -- and used to raise.
	The stock comes back when the cancellation is handled a moment later, so there is nothing
	to credit and nothing to fix except the log.
	"""

	def _refund(self, amount="0.00", restock="CANCEL", transactions=None):
		return {
			"id": "gid://shopify/Refund/1118",
			"createdAt": "2026-09-26T20:22:49Z",
			"order": {"id": "gid://shopify/Order/1118", "name": "#1118"},
			"totalRefundedSet": {
				"shopMoney": {"amount": amount},
				"presentmentMoney": {"amount": amount},
			},
			"refundLineItems": {
				"nodes": [
					{
						"quantity": 1,
						"restockType": restock,
						"lineItem": {"id": "gid://shopify/LineItem/1", "sku": "ORD-TEE"},
					}
				]
			},
			"transactions": {"nodes": transactions or []},
		}

	def test_a_restocking_refund_for_zero_moves_no_money(self):
		from shopify_integration.inbound.refund import _moves_no_money

		self.assertTrue(_moves_no_money(self._refund()))

	def test_a_refund_that_returns_money_does_not_qualify(self):
		from shopify_integration.inbound.refund import _moves_no_money

		self.assertFalse(_moves_no_money(self._refund(amount="499.00")))

	def test_a_settled_refund_transaction_counts_as_money(self):
		"""Zero on the header but a successful refund transaction is still real money."""
		from shopify_integration.inbound.refund import _moves_no_money

		refund = self._refund(
			transactions=[
				{
					"kind": "REFUND",
					"status": "SUCCESS",
					"amountSet": {
						"shopMoney": {"amount": "250.00"},
						"presentmentMoney": {"amount": "250.00"},
					},
				}
			]
		)
		self.assertFalse(_moves_no_money(refund))

	def test_a_failed_transaction_does_not_count(self):
		from shopify_integration.inbound.refund import _moves_no_money

		refund = self._refund(
			transactions=[
				{
					"kind": "REFUND",
					"status": "FAILURE",
					"amountSet": {
						"shopMoney": {"amount": "250.00"},
						"presentmentMoney": {"amount": "250.00"},
					},
				}
			]
		)
		self.assertTrue(_moves_no_money(refund))

	def test_refunded_shipping_counts_as_money(self):
		from shopify_integration.inbound.refund import _moves_no_money

		refund = self._refund()
		refund["refundShippingLines"] = {"nodes": [{"shippingLine": {"title": "Standard"}}]}
		self.assertFalse(_moves_no_money(refund))

	def test_the_restocking_refund_is_skipped_rather_than_failed(self):
		"""The regression: #1118 logged 'has no Sales Invoice in ERPNext'."""
		from shopify_integration.inbound.refund import create_credit_note

		store = frappe.get_cached_doc(
			"Shopify Store", frappe.get_all("Shopify Store", limit=1, pluck="name")[0]
		)
		result = create_credit_note(store, self._refund())
		self.assertIn("no money refunded", result.get("skipped", ""))

	def test_a_real_refund_with_no_invoice_still_raises(self):
		"""Money went back and there is nothing to credit it against. That is a real gap."""
		from shopify_integration.inbound.refund import create_credit_note

		store = frappe.get_cached_doc(
			"Shopify Store", frappe.get_all("Shopify Store", limit=1, pluck="name")[0]
		)
		with self.assertRaises(frappe.ValidationError) as caught:
			create_credit_note(store, self._refund(amount="499.00"))
		self.assertIn("no Sales Invoice", str(caught.exception))


class TestWorkingOutWhatIsOwed(FrappeTestCase):
	"""The measure the whole thing turns on."""

	def test_a_shipped_and_returned_unit_is_not_owed(self):
		"""#1122. currentQuantity says 1, unfulfilled says 0, and 0 is the answer."""
		order = refund_payload("gid://shopify/Order/1", [("EAR", 2, 1, 0)])["order"]
		self.assertEqual(owed_by_sku(order), {"EAR": 0})

	def test_an_unshipped_partly_refunded_line_is_owed_what_is_left(self):
		order = refund_payload("gid://shopify/Order/1", [("EAR", 3, 2, 2)])["order"]
		self.assertEqual(owed_by_sku(order), {"EAR": 2})

	def test_a_fully_refunded_line_is_owed_nothing(self):
		order = refund_payload("gid://shopify/Order/1", [("EAR", 2, 0, 0)])["order"]
		self.assertEqual(owed_by_sku(order), {"EAR": 0})

	def test_an_untouched_line_is_owed_in_full(self):
		order = refund_payload("gid://shopify/Order/1", [("EAR", 2, 2, 2)])["order"]
		self.assertEqual(owed_by_sku(order), {"EAR": 2})

	def test_the_smaller_of_the_two_always_wins(self):
		"""Whichever way Shopify accounts for a refunded unit that had already shipped."""
		order = refund_payload("gid://shopify/Order/1", [("EAR", 5, 4, 1)])["order"]
		self.assertEqual(owed_by_sku(order), {"EAR": 1})
		order = refund_payload("gid://shopify/Order/1", [("EAR", 5, 1, 4)])["order"]
		self.assertEqual(owed_by_sku(order), {"EAR": 1})

	def test_lines_without_a_sku_are_ignored(self):
		order = refund_payload("gid://shopify/Order/1", [("", 2, 2, 2)])["order"]
		self.assertEqual(owed_by_sku(order), {})

	def test_two_lines_of_the_same_sku_add_up(self):
		order = refund_payload("gid://shopify/Order/1", [("EAR", 1, 1, 1), ("EAR", 2, 2, 2)])["order"]
		self.assertEqual(owed_by_sku(order), {"EAR": 3})
