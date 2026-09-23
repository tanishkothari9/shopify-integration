"""Refunds -> Credit Notes against a real site (spec §8.6, §16).

The spec names three cases that must each work and each be tested explicitly: partial
refunds, refunds without restock, and refunds on unfulfilled orders. All three are here,
plus the accounting and idempotency around them.
"""

from __future__ import annotations

from decimal import Decimal

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cstr

from shopify_integration.inbound import order as order_module
from shopify_integration.inbound import refund as refund_module
from shopify_integration.tests.test_orders import OrderTestCase, bag, ensure_stock


def build_refund(
	gid="gid://shopify/Refund/9001",
	order_gid="gid://shopify/Order/5001",
	order_name="#1001",
	qty=1,
	restock_type="RETURN",
	subtotal=None,
	tax=None,
	shipping="0.00",
	shipping_tax="0.00",
	total=None,
	transactions=None,
	sku="ORD-TEE",
	variant_gid=None,
	location_gid=None,
):
	"""A Shopify refund in the shape refund_by_id.graphql returns.

	Amounts scale with `qty` unless given explicitly. Shopify's own figures always agree with
	each other -- a refund of two units never reports the subtotal of one -- and a fixture that
	disagrees tests nothing that can happen.
	"""
	from decimal import Decimal

	subtotal = subtotal if subtotal is not None else f"{Decimal('50.00') * qty:.2f}"
	tax = tax if tax is not None else f"{Decimal('5.00') * qty:.2f}"
	total = total or f"{Decimal(subtotal) + Decimal(tax) + Decimal(shipping) + Decimal(shipping_tax):.2f}"

	if transactions is None:
		transactions = [
			{
				"id": "gid://shopify/OrderTransaction/1",
				"kind": "REFUND",
				"status": "SUCCESS",
				"gateway": "manual",
				"amountSet": bag(total),
			}
		]

	return {
		"id": gid,
		"createdAt": "2026-09-05T10:00:00Z",
		"note": "Customer changed their mind",
		"order": {"id": order_gid, "name": order_name},
		"totalRefundedSet": bag(total),
		"refundLineItems": {
			"nodes": [
				{
					"quantity": qty,
					"restockType": restock_type,
					"location": {"id": location_gid} if location_gid else None,
					"lineItem": {
						"id": "gid://shopify/LineItem/1",
						"sku": sku,
						"title": "Basic Tee",
						"variant": {"id": variant_gid} if variant_gid else None,
					},
					"priceSet": bag(subtotal),
					"subtotalSet": bag(subtotal),
					"totalTaxSet": bag(tax),
				}
			]
		},
		"refundShippingLines": {
			"nodes": (
				[
					{
						"shippingLine": {"title": "Standard"},
						"subtotalAmountSet": bag(shipping),
						"taxAmountSet": bag(shipping_tax),
					}
				]
				if shipping != "0.00"
				else []
			)
		},
		"transactions": {"nodes": transactions},
	}


class RefundTestCase(OrderTestCase):
	"""Reuses the order fixture, then adds stock and an invoiced order to refund against."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.db.set_value("Shopify Store", cls.store, "sync_refunds", 1)
		cls.store_doc.reload()
		ensure_stock("ORD-TEE", cls.warehouse, 200)
		frappe.db.commit()

	def invoiced_order(self, order_gid, *, fulfil=True, qty=2):
		"""Create a paid order, optionally delivered, and return its identifiers."""
		fulfillments = []
		if fulfil:
			fulfillments = [
				{
					"id": f"{order_gid}/fulfillment/1",
					"status": "SUCCESS",
					"createdAt": "2026-09-02T10:00:00Z",
					"location": None,
					"fulfillmentLineItems": {
						"nodes": [
							{
								"id": "f/1",
								"quantity": qty,
								"lineItem": {
									"id": "gid://shopify/LineItem/1",
									"sku": "ORD-TEE",
									"variant": {"id": self.variant_gid},
								},
							}
						]
					},
				}
			]

		payload = self.order(gid=order_gid, qty=qty, fulfillments=fulfillments)
		result = order_module.create_sales_invoice(self.store_doc, payload)
		if fulfil:
			order_module.create_delivery_notes(self.store_doc, payload)
		return result["sales_invoice"]


class TestCreditNote(RefundTestCase):
	def test_full_refund_creates_a_submitted_credit_note(self):
		self.invoiced_order("gid://shopify/Order/9101")
		refund = build_refund(
			gid="gid://shopify/Refund/9101",
			order_gid="gid://shopify/Order/9101",
			qty=2,
			subtotal="100.00",
			tax="10.00",
			variant_gid=self.variant_gid,
		)
		result = refund_module.create_credit_note(self.store_doc, refund)

		credit_note = frappe.get_doc("Sales Invoice", result["credit_note"])
		self.assertEqual(credit_note.docstatus, 1)
		self.assertEqual(credit_note.is_return, 1)
		self.assertTrue(credit_note.return_against)
		self.assertEqual(credit_note.shopify_refund_gid, "gid://shopify/Refund/9101")

	def test_credit_note_quantities_are_negative(self):
		self.invoiced_order("gid://shopify/Order/9102")
		refund = build_refund(
			gid="gid://shopify/Refund/9102",
			order_gid="gid://shopify/Order/9102",
			qty=2,
			variant_gid=self.variant_gid,
		)
		result = refund_module.create_credit_note(self.store_doc, refund)

		credit_note = frappe.get_doc("Sales Invoice", result["credit_note"])
		self.assertTrue(all(row.qty < 0 for row in credit_note.items))

	def test_partial_refund_credits_only_the_refunded_quantity(self):
		"""One of the three cases the spec names explicitly."""
		self.invoiced_order("gid://shopify/Order/9103", qty=2)
		refund = build_refund(
			gid="gid://shopify/Refund/9103",
			order_gid="gid://shopify/Order/9103",
			qty=1,
			subtotal="50.00",
			tax="5.00",
			variant_gid=self.variant_gid,
		)
		result = refund_module.create_credit_note(self.store_doc, refund)

		credit_note = frappe.get_doc("Sales Invoice", result["credit_note"])
		self.assertEqual(abs(credit_note.items[0].qty), 1, "only the refunded unit is credited")

	def test_redelivered_refund_does_not_credit_twice(self):
		self.invoiced_order("gid://shopify/Order/9104")
		refund = build_refund(
			gid="gid://shopify/Refund/9104",
			order_gid="gid://shopify/Order/9104",
			variant_gid=self.variant_gid,
		)
		first = refund_module.create_credit_note(self.store_doc, refund)
		second = refund_module.create_credit_note(self.store_doc, refund)

		self.assertEqual(first["credit_note"], second["credit_note"])
		self.assertEqual(
			frappe.db.count(
				"Sales Invoice",
				{"shopify_refund_gid": "gid://shopify/Refund/9104", "docstatus": 1},
			),
			1,
		)

	def test_refund_for_an_uninvoiced_order_fails_clearly(self):
		refund = build_refund(
			gid="gid://shopify/Refund/9105",
			order_gid="gid://shopify/Order/does-not-exist",
			variant_gid=self.variant_gid,
		)
		with self.assertRaises(frappe.ValidationError) as caught:
			refund_module.create_credit_note(self.store_doc, refund)
		self.assertIn("no Sales Invoice", str(caught.exception))

	def test_refunded_shipping_is_credited(self):
		self.invoiced_order("gid://shopify/Order/9106")
		refund = build_refund(
			gid="gid://shopify/Refund/9106",
			order_gid="gid://shopify/Order/9106",
			qty=1,
			subtotal="50.00",
			tax="5.00",
			shipping="9.99",
			variant_gid=self.variant_gid,
		)
		result = refund_module.create_credit_note(self.store_doc, refund)

		credit_note = frappe.get_doc("Sales Invoice", result["credit_note"])
		descriptions = " ".join(cstr(row.description) for row in credit_note.taxes)
		self.assertIn("shipping", descriptions.lower())


class TestRestocking(RefundTestCase):
	def test_restocked_refund_returns_the_stock(self):
		"""A delivered order refunded with restock: the units come back."""
		self.invoiced_order("gid://shopify/Order/9201", qty=2)
		before = frappe.db.get_value(
			"Bin", {"item_code": "ORD-TEE", "warehouse": self.warehouse}, "actual_qty"
		)

		refund = build_refund(
			gid="gid://shopify/Refund/9201",
			order_gid="gid://shopify/Order/9201",
			qty=2,
			restock_type="RETURN",
			variant_gid=self.variant_gid,
		)
		result = refund_module.create_credit_note(self.store_doc, refund)

		self.assertIsNotNone(result["return_delivery_note"], "a return delivery note is required")
		after = frappe.db.get_value(
			"Bin", {"item_code": "ORD-TEE", "warehouse": self.warehouse}, "actual_qty"
		)
		self.assertEqual(after, before + 2)

	def test_refund_without_restock_leaves_stock_out(self):
		"""The second case the spec names: money back, goods written off."""
		self.invoiced_order("gid://shopify/Order/9202", qty=2)
		before = frappe.db.get_value(
			"Bin", {"item_code": "ORD-TEE", "warehouse": self.warehouse}, "actual_qty"
		)

		refund = build_refund(
			gid="gid://shopify/Refund/9202",
			order_gid="gid://shopify/Order/9202",
			qty=2,
			restock_type="NO_RESTOCK",
			variant_gid=self.variant_gid,
		)
		result = refund_module.create_credit_note(self.store_doc, refund)

		self.assertIsNone(result["return_delivery_note"])
		after = frappe.db.get_value(
			"Bin", {"item_code": "ORD-TEE", "warehouse": self.warehouse}, "actual_qty"
		)
		self.assertEqual(after, before, "damaged goods must not reappear in stock")

	def test_refund_on_an_unfulfilled_order_invents_no_stock(self):
		"""The third case, and the one that quietly corrupts a stock ledger.

		Nothing ever left the warehouse, so nothing may come back -- even though Shopify says
		restock, because Shopify is describing its own inventory, not ours."""
		self.invoiced_order("gid://shopify/Order/9203", fulfil=False, qty=2)
		before = frappe.db.get_value(
			"Bin", {"item_code": "ORD-TEE", "warehouse": self.warehouse}, "actual_qty"
		)

		refund = build_refund(
			gid="gid://shopify/Refund/9203",
			order_gid="gid://shopify/Order/9203",
			qty=2,
			restock_type="RETURN",
			variant_gid=self.variant_gid,
		)
		result = refund_module.create_credit_note(self.store_doc, refund)

		self.assertIsNone(result["return_delivery_note"])
		after = frappe.db.get_value(
			"Bin", {"item_code": "ORD-TEE", "warehouse": self.warehouse}, "actual_qty"
		)
		self.assertEqual(after, before, "stock that never left must not return")
		self.assertIsNotNone(result["credit_note"], "the customer is still credited")

	def test_cancel_restock_type_also_returns_stock(self):
		self.invoiced_order("gid://shopify/Order/9204", qty=2)
		refund = build_refund(
			gid="gid://shopify/Refund/9204",
			order_gid="gid://shopify/Order/9204",
			qty=1,
			restock_type="CANCEL",
			variant_gid=self.variant_gid,
		)
		result = refund_module.create_credit_note(self.store_doc, refund)
		self.assertIsNotNone(result["return_delivery_note"])


class TestRefundPayment(RefundTestCase):
	def test_settled_refund_creates_a_reversing_payment(self):
		self.invoiced_order("gid://shopify/Order/9301")
		refund = build_refund(
			gid="gid://shopify/Refund/9301",
			order_gid="gid://shopify/Order/9301",
			variant_gid=self.variant_gid,
		)
		result = refund_module.create_credit_note(self.store_doc, refund)
		self.assertIsNotNone(result["payment_entry"])

	def test_refund_with_no_transaction_creates_no_payment(self):
		"""A store-credit exchange is recorded as a refund but moves no money."""
		self.invoiced_order("gid://shopify/Order/9302")
		refund = build_refund(
			gid="gid://shopify/Refund/9302",
			order_gid="gid://shopify/Order/9302",
			transactions=[],
			variant_gid=self.variant_gid,
		)
		result = refund_module.create_credit_note(self.store_doc, refund)
		self.assertIsNone(result["payment_entry"])

	def test_failed_transaction_creates_no_payment(self):
		self.invoiced_order("gid://shopify/Order/9303")
		refund = build_refund(
			gid="gid://shopify/Refund/9303",
			order_gid="gid://shopify/Order/9303",
			transactions=[
				{
					"id": "t/1",
					"kind": "REFUND",
					"status": "FAILURE",
					"gateway": "manual",
					"amountSet": bag("55.00"),
				}
			],
			variant_gid=self.variant_gid,
		)
		result = refund_module.create_credit_note(self.store_doc, refund)
		self.assertIsNone(result["payment_entry"])

	def test_non_refund_transactions_are_ignored(self):
		"""A SALE transaction on the refund must not be read as money going back."""
		from shopify_integration.utils.taxes import SHOP_MONEY

		refund = build_refund(
			transactions=[
				{"id": "t/1", "kind": "SALE", "status": "SUCCESS", "amountSet": bag("50.00")},
				{"id": "t/2", "kind": "REFUND", "status": "SUCCESS", "amountSet": bag("20.00")},
			]
		)
		from decimal import Decimal

		self.assertEqual(refund_module.refunded_amount(refund, SHOP_MONEY), Decimal("20.00"))


class TestCancellingARefundedOrder(RefundTestCase):
	"""Found live: a refunded order could not be cancelled at all."""

	def test_credit_note_records_the_order_it_belongs_to(self):
		"""Without this the credit note is findable only by its refund, so cancelling the
		order never finds it -- and ERPNext then refuses to cancel the invoice it points at."""
		self.invoiced_order("gid://shopify/Order/9401")
		refund = build_refund(
			gid="gid://shopify/Refund/9401",
			order_gid="gid://shopify/Order/9401",
			variant_gid=self.variant_gid,
		)
		result = refund_module.create_credit_note(self.store_doc, refund)

		self.assertEqual(
			frappe.db.get_value("Sales Invoice", result["credit_note"], "shopify_order_gid"),
			"gid://shopify/Order/9401",
		)

	def test_a_refunded_order_can_be_cancelled(self):
		"""The whole chain: payment entries first, then returns, then what they return
		against. Cancelling the original first is refused by ERPNext."""
		self.invoiced_order("gid://shopify/Order/9402")
		refund = build_refund(
			gid="gid://shopify/Refund/9402",
			order_gid="gid://shopify/Order/9402",
			variant_gid=self.variant_gid,
		)
		refund_module.create_credit_note(self.store_doc, refund)

		cancelled = order_module.cancel_linked_documents(self.store, "gid://shopify/Order/9402")

		self.assertTrue(any("Payment Entry" in c for c in cancelled), "payments must cancel first")
		self.assertTrue(any("Sales Invoice" in c for c in cancelled))
		self.assertEqual(
			frappe.db.get_value(
				"Sales Order", {"shopify_order_gid": "gid://shopify/Order/9402"}, "docstatus"
			),
			2,
		)

	def test_nothing_is_left_submitted_after_cancelling(self):
		self.invoiced_order("gid://shopify/Order/9403")
		refund = build_refund(
			gid="gid://shopify/Refund/9403",
			order_gid="gid://shopify/Order/9403",
			variant_gid=self.variant_gid,
		)
		refund_module.create_credit_note(self.store_doc, refund)
		order_module.cancel_linked_documents(self.store, "gid://shopify/Order/9403")

		for doctype in ("Sales Order", "Sales Invoice", "Delivery Note"):
			left = frappe.get_all(
				doctype,
				filters={"shopify_order_gid": "gid://shopify/Order/9403", "docstatus": 1},
				pluck="name",
			)
			self.assertEqual(left, [], f"{doctype} still submitted: {left}")


class TestRefundedTaxGoesToTheRightAccounts(FrappeTestCase):
	"""Which ledger account a refunded amount lands on.

	The first version took `rows[0]` as "the tax account" -- but `_add_charges` appends the
	shipping row *before* the tax rows, so every refund on a store that books shipping as a
	charge credited the tax to freight and the freight to tax. Exactly inverted, silently, on
	every return.
	"""

	def _rows(self, *pairs):
		return [frappe._dict({"account_head": a, "tax_amount": t}) for a, t in pairs]

	def test_a_single_tax_account_takes_the_whole_refund(self):
		split = refund_module._split_like(self._rows(("VAT - TC", 20.0)), Decimal("7.50"))
		self.assertEqual(split, {"VAT - TC": Decimal("7.50")})

	def test_cgst_and_sgst_keep_their_halves(self):
		"""Crediting both halves to CGST misstates every line of the GST return."""
		split = refund_module._split_like(
			self._rows(("Output Tax CGST - TC", 90.0), ("Output Tax SGST - TC", 90.0)),
			Decimal("180.00"),
		)
		self.assertEqual(
			split, {"Output Tax CGST - TC": Decimal("90.00"), "Output Tax SGST - TC": Decimal("90.00")}
		)

	def test_uneven_accounts_split_in_their_own_proportion(self):
		split = refund_module._split_like(self._rows(("A - TC", 75.0), ("B - TC", 25.0)), Decimal("40.00"))
		self.assertEqual(split["A - TC"], Decimal("30.00"))
		self.assertEqual(split["B - TC"], Decimal("10.00"))

	def test_the_parts_always_sum_to_the_total(self):
		"""A credit note whose rows do not add up to the refund is refused before it posts."""
		split = refund_module._split_like(
			self._rows(("A - TC", 1.0), ("B - TC", 1.0), ("C - TC", 1.0)), Decimal("10.00")
		)
		self.assertEqual(sum(split.values()), Decimal("10.00"))

	def test_all_zero_rows_still_place_the_refund_somewhere(self):
		split = refund_module._split_like(self._rows(("A - TC", 0.0), ("B - TC", 0.0)), Decimal("5.00"))
		self.assertEqual(sum(split.values()), Decimal("5.00"))

	def test_nothing_to_split_yields_nothing(self):
		self.assertEqual(refund_module._split_like([], Decimal("5.00")), {})
		self.assertEqual(refund_module._split_like(self._rows(("A - TC", 1.0)), Decimal("0")), {})


class TestReversingPaymentAmount(OrderTestCase):
	"""A reversing Payment Entry moves what the gateway returned, not the credit note's total."""

	def test_only_the_settled_part_leaves_the_bank(self):
		order = self.order(gid="gid://shopify/Order/9401")
		order_module.create_sales_order(self.store_doc, order)
		order_module.create_sales_invoice(self.store_doc, order)

		# 55.00 credited, but only 20.00 actually went back to the card
		refund = build_refund(
			gid="gid://shopify/Refund/9401",
			order_gid="gid://shopify/Order/9401",
			transactions=[
				{
					"id": "gid://shopify/OrderTransaction/9401",
					"kind": "REFUND",
					"status": "SUCCESS",
					"gateway": "manual",
					"amountSet": bag("20.00"),
				}
			],
		)
		result = refund_module.create_credit_note(self.store_doc, refund)

		self.assertIsNotNone(result["payment_entry"], "a settled refund must move money")
		entry = frappe.get_doc("Payment Entry", result["payment_entry"])
		credited = abs(frappe.db.get_value("Sales Invoice", result["credit_note"], "grand_total"))

		self.assertAlmostEqual(entry.paid_amount, 20.00, places=2)
		self.assertNotAlmostEqual(
			entry.paid_amount,
			credited,
			places=2,
			msg="paying out the whole credit note overstates the bank by the store-credit part",
		)
		self.assertTrue(
			all(r.allocated_amount <= 20.00 + 0.01 for r in entry.references),
			"no reference may be allocated more than was actually paid",
		)


class TestCreditNoteTotalIsGuarded(OrderTestCase):
	"""Orders have always been refused when they disagree with Shopify. Refunds were not, and
	that is precisely why every arithmetic error in this module used to be silent."""

	def test_a_credit_note_that_does_not_match_the_refund_is_refused(self):
		order = self.order(gid="gid://shopify/Order/9402")
		order_module.create_sales_order(self.store_doc, order)
		order_module.create_sales_invoice(self.store_doc, order)

		# Shopify says it refunded 10.00 while the lines add up to far more
		refund = build_refund(
			gid="gid://shopify/Refund/9402", order_gid="gid://shopify/Order/9402", qty=1, total="10.00"
		)

		with self.assertRaises(frappe.ValidationError) as caught:
			refund_module.create_credit_note(self.store_doc, refund)

		self.assertIn("disagrees with the refund", str(caught.exception))


class TestRefundingATaxInclusiveInvoice(FrappeTestCase):
	"""A return against an MRP invoice must keep the invoice's tax shape.

	`_apply_refund_taxes` used to replace every copied row with an `Actual` amount. That is
	right for tax-on-top, but an inclusive invoice books tax as a percentage row marked
	"included in print rate", and every line carries its own Item Tax Template.

	Flattening it broke the note outright -- `india_compliance` recomputes per-item tax from
	the rows and refused it:

	    Tax Amount -107.14 as computed for Item SAREE-001 is incorrect.
	    Try setting the Charge Type to On Net Total

	The refund then never reached ERPNext at all. Found on a live return of one 5% saree out
	of a mixed 5/18/3% order.
	"""

	def test_inclusive_rows_are_recognised(self):
		rows = [
			frappe._dict(charge_type="On Net Total", included_in_print_rate=1, account_head="CGST"),
			frappe._dict(charge_type="On Net Total", included_in_print_rate=1, account_head="SGST"),
		]
		self.assertTrue(refund_module._is_inclusive(rows))

	def test_tax_on_top_rows_are_not(self):
		rows = [
			frappe._dict(charge_type="On Net Total", included_in_print_rate=0, account_head="CGST"),
		]
		self.assertFalse(refund_module._is_inclusive(rows))

	def test_actual_rows_are_not(self):
		"""A charge booked as a flat amount is not an inclusive percentage row."""
		rows = [frappe._dict(charge_type="Actual", included_in_print_rate=1, account_head="CGST")]
		self.assertFalse(refund_module._is_inclusive(rows))

	def test_no_rows_is_not_inclusive(self):
		"""An order with no tax at all must not be mistaken for an inclusive one."""
		self.assertFalse(refund_module._is_inclusive([]))

	def test_an_inclusive_refund_keeps_the_percentage_rows(self):
		"""The shape survives: percentage, the same rate, still marked included."""
		credit_note = _StubNote(
			[
				frappe._dict(
					charge_type="On Net Total",
					included_in_print_rate=1,
					account_head="Output Tax CGST - X",
					rate=2.5,
					description="CGST",
				),
				frappe._dict(
					charge_type="On Net Total",
					included_in_print_rate=1,
					account_head="Output Tax SGST - X",
					rate=2.5,
					description="SGST",
				),
			]
		)
		store = frappe._dict(tax_map=[])
		quantities = {"SAREE-001": {"qty": 1, "tax": Decimal("214.29")}}

		refund_module._apply_refund_taxes(store, credit_note, {}, quantities, "shopMoney")

		self.assertEqual(len(credit_note.rows), 2)
		for row in credit_note.rows:
			self.assertEqual(row["charge_type"], "On Net Total")
			self.assertEqual(row["included_in_print_rate"], 1)
			self.assertEqual(row["rate"], 2.5)
			self.assertNotIn("tax_amount", row, "an inclusive row must not carry a flat amount as well")


class _StubNote:
	"""Just enough of a credit note for the tax rows: the copied rows in, the new rows out."""

	def __init__(self, taxes):
		self._taxes = taxes
		self.rows = []

	def get(self, field):
		return self._taxes if field == "taxes" else None

	def set(self, field, value):
		if field == "taxes":
			self._taxes = value

	def append(self, field, row):
		if field == "taxes":
			self.rows.append(row)


class TestCancellingAnUnpaidOrderIsNotARefund(FrappeTestCase):
	"""Shopify writes a zero-value refund whenever an unpaid order is cancelled.

	Nothing was charged, so nothing is being given back, and there is no Sales Invoice to
	credit -- the order was never paid. The handler used to raise on it:

	    Refund ... is for Shopify order ..., which has no Sales Invoice in ERPNext.

	Every cancelled unpaid order produced one, and errors like that bury the ones that matter.
	Found by cancelling a live order.
	"""

	def _refund(self, *, amount="0.0", restock="NO_RESTOCK", lines=1, shipping=False):
		return {
			"id": "gid://shopify/Refund/900",
			"order": {"id": "gid://shopify/Order/900", "name": "#900"},
			"totalRefundedSet": {
				"shopMoney": {"amount": amount, "currencyCode": "INR"},
				"presentmentMoney": {"amount": amount, "currencyCode": "INR"},
			},
			"refundLineItems": {
				"nodes": [
					{"quantity": 1, "restockType": restock, "lineItem": {"sku": "X"}} for _ in range(lines)
				]
			},
			"refundShippingLines": {
				"nodes": [{"subtotalAmountSet": {"shopMoney": {"amount": "50.00"}}}] if shipping else []
			},
		}

	def test_a_cancellation_artefact_is_skipped(self):
		self.assertTrue(refund_module._refunded_nothing(self._refund()))

	def test_a_refund_that_moves_money_is_not_skipped(self):
		self.assertFalse(refund_module._refunded_nothing(self._refund(amount="4500.00")))

	def test_a_zero_value_return_of_goods_is_not_skipped(self):
		"""Goods coming back with the money settled elsewhere is a real return."""
		self.assertFalse(refund_module._refunded_nothing(self._refund(restock="RETURN")))
		self.assertFalse(refund_module._refunded_nothing(self._refund(restock="CANCEL")))

	def test_refunded_shipping_alone_is_not_skipped(self):
		self.assertFalse(refund_module._refunded_nothing(self._refund(shipping=True)))

	def test_a_refund_with_no_lines_at_all_is_skipped(self):
		self.assertTrue(refund_module._refunded_nothing(self._refund(lines=0)))
