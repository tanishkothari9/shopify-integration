"""Orders against a real ERPNext site (spec §8.3-§8.5, §13, §16).

The phase-3 acceptance criteria drive these: duplicate webhooks produce one Sales Order,
document totals match Shopify exactly, and partial fulfilment works.

The builders are exercised directly with fabricated order payloads rather than through the
webhook, so no test touches the network. The webhook path itself is covered in
test_integration.py.
"""

from __future__ import annotations

from decimal import Decimal

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.catalogue import mapping
from shopify_integration.inbound import customer as customer_module
from shopify_integration.inbound import order as order_module
from shopify_integration.tests.test_catalogue import simple_product
from shopify_integration.tests.test_integration import SECRET_A, ensure_company, make_store
from shopify_integration.utils import taxes as taxes_module

ORDER_GID = "gid://shopify/Order/5001"


def bag(shop, presentment=None):
	return {
		"shopMoney": {"amount": shop, "currencyCode": "USD"},
		"presentmentMoney": {"amount": presentment or shop, "currencyCode": "USD"},
	}


def build_order(
	gid=ORDER_GID,
	name="#1001",
	sku="ORD-TEE",
	variant_gid=None,
	qty=2,
	unit_price="50.00",
	discounted_price=None,
	tax="10.00",
	shipping="9.99",
	shipping_tax="0.00",
	total=None,
	outstanding="0.00",
	taxes_included=False,
	currency="USD",
	customer=None,
	fulfillments=None,
):
	"""A Shopify order in the shape order_by_id.graphql returns."""
	discounted_price = discounted_price or unit_price
	from decimal import Decimal

	computed = Decimal(discounted_price) * qty + Decimal(tax) + Decimal(shipping) + Decimal(shipping_tax)
	total = total or f"{computed:.2f}"

	return {
		"id": gid,
		"name": name,
		"createdAt": "2026-09-01T10:00:00Z",
		"processedAt": "2026-09-01T10:05:00Z",
		"note": "Leave at the door",
		"currencyCode": currency,
		"taxesIncluded": taxes_included,
		"displayFinancialStatus": "PAID",
		"totalPriceSet": bag(total),
		"totalTaxSet": bag(tax),
		"totalOutstandingSet": bag(outstanding),
		"customer": customer,
		"shippingAddress": None,
		"billingAddress": None,
		"taxLines": [{"title": "State Tax", "rate": 0.1, "priceSet": bag(tax)}],
		"shippingLines": {
			"nodes": [
				{
					"title": "Standard",
					"originalPriceSet": bag(shipping),
					"discountedPriceSet": bag(shipping),
					"taxLines": (
						[{"title": "State Tax", "rate": 0.1, "priceSet": bag(shipping_tax)}]
						if shipping_tax != "0.00"
						else []
					),
				}
			]
		},
		"lineItems": {
			"nodes": [
				{
					"id": "gid://shopify/LineItem/1",
					"title": "Basic Tee",
					"quantity": qty,
					"sku": sku,
					"taxable": True,
					"variant": {"id": variant_gid} if variant_gid else None,
					"originalUnitPriceSet": bag(unit_price),
					"discountedUnitPriceSet": bag(discounted_price),
					"taxLines": [{"title": "State Tax", "rate": 0.1, "priceSet": bag(tax)}],
				}
			],
			"pageInfo": {"hasNextPage": False},
		},
		"fulfillments": fulfillments or [],
	}


class OrderTestCase(FrappeTestCase):
	"""Shared setup: a store wired to a real company, with one mapped product."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)
		cls.store_doc = frappe.get_doc("Shopify Store", cls.store)
		company = cls.store_doc.company

		cls.warehouse = frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")
		cls.tax_account = frappe.db.get_value(
			"Account", {"company": company, "account_type": "Tax", "is_group": 0}, "name"
		) or frappe.db.get_value("Account", {"company": company, "is_group": 0}, "name")
		cls.shipping_account = frappe.db.get_value(
			"Account", {"company": company, "root_type": "Income", "is_group": 0}, "name"
		)
		cls.cash_account = frappe.db.get_value(
			"Account", {"company": company, "account_type": "Cash", "is_group": 0}, "name"
		) or frappe.db.get_value("Account", {"company": company, "root_type": "Asset", "is_group": 0}, "name")
		cls.customer = _ensure_customer()

		cls.store_doc.default_warehouse = cls.warehouse
		cls.store_doc.default_customer = cls.customer
		cls.store_doc.cash_bank_account = cls.cash_account
		cls.store_doc.sync_orders = 1
		cls.store_doc.sync_invoices = 1
		cls.store_doc.sync_delivery_notes = 1
		cls.store_doc.set("tax_map", [])
		cls.store_doc.append(
			"tax_map",
			{"shopify_tax_title": "State Tax", "account_head": cls.tax_account, "charge_type": "sales_tax"},
		)
		cls.store_doc.append(
			"tax_map",
			{
				"shopify_tax_title": "Shipping",
				"account_head": cls.shipping_account,
				"charge_type": "shipping",
			},
		)
		cls.store_doc.flags.ignore_mandatory = True
		cls.store_doc.save(ignore_permissions=True)

		mapping.write_product_mapping(cls.store, simple_product(sku="ORD-TEE"))
		cls.variant_gid = frappe.db.get_value(
			"Shopify Item Link", {"store": cls.store, "item_code": "ORD-TEE"}, "variant_gid"
		)
		frappe.db.commit()

	def order(self, **kwargs):
		kwargs.setdefault("variant_gid", self.variant_gid)
		return build_order(**kwargs)


def ensure_stock(item_code: str, warehouse: str, qty: float = 100) -> None:
	"""Put stock in the warehouse so Delivery Notes can actually ship.

	A Delivery Note moves stock, and ERPNext refuses to move stock that is not there. Without
	this the delivery tests fail on NegativeStockError rather than on anything they are
	meant to be checking.
	"""
	from erpnext.stock.utils import get_stock_balance

	year_start = f"{frappe.utils.getdate(frappe.utils.nowdate()).year}-01-01"

	# Both dates matter, and checking only one is how this fixture rotted before. The
	# backdated balance is what a Delivery Note posted on Shopify's fulfilment date sees; the
	# current balance is what is actually left. Tests that go through the webhook handlers
	# commit, so their shipments are permanent -- the stock genuinely drains run after run
	# while the year-start balance stays put, and a guard on that alone never tops up.
	current = get_stock_balance(item_code, warehouse)
	backdated = get_stock_balance(item_code, warehouse, posting_date=year_start)
	if min(current, backdated) >= qty:
		return

	# Top up with room for a whole run rather than the bare minimum, so the suite does not
	# start failing again a few runs from now.
	qty = max(qty, 1000)

	entry = frappe.new_doc("Stock Entry")
	entry.stock_entry_type = "Material Receipt"
	entry.purpose = "Material Receipt"
	entry.company = frappe.db.get_value("Warehouse", warehouse, "company")
	# Backdated to the start of the fiscal year. The Delivery Notes under test are posted on
	# Shopify's fulfilment date, which is in the past; stock received today would not exist
	# yet as of that date, and ERPNext is right to refuse the movement.
	entry.set_posting_time = 1
	entry.posting_date = f"{frappe.utils.getdate(frappe.utils.nowdate()).year}-01-01"
	entry.append(
		"items",
		{"item_code": item_code, "qty": qty, "t_warehouse": warehouse, "basic_rate": 10},
	)
	entry.flags.ignore_permissions = True
	entry.insert(ignore_permissions=True)
	entry.submit()
	frappe.db.commit()


def clear_shopify_documents(store: str) -> None:
	"""Remove documents this store's tests committed on a previous run.

	The webhook handlers commit deliberately, so their Sales Orders and Delivery Notes survive
	FrappeTestCase's rollback and collide with the next run on the unique Shopify order index.
	Cancelled first, then deleted, and in dependency order.
	"""
	for doctype in ("Payment Entry", "Delivery Note", "Sales Invoice", "Sales Order"):
		filters = {"shopify_store": store} if frappe.db.has_column(doctype, "shopify_store") else None
		if filters is None:
			continue
		for name in frappe.get_all(doctype, filters=filters, pluck="name"):
			doc = frappe.get_doc(doctype, name)
			try:
				if doc.docstatus == 1:
					doc.flags.ignore_links = True
					doc.cancel()
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
			except Exception:
				# A document another test still depends on; leave it rather than cascading.
				frappe.db.rollback()

	# Event logs are committed by the fixture that creates them, for the same reason: a handler
	# never sees an uncommitted one. Left behind, they accumulate across runs and show up as
	# this store's failures in the real Shopify Event Log list.
	frappe.db.delete("Shopify Event Log", {"store": store})
	frappe.db.commit()


def _ensure_customer() -> str:
	if frappe.db.exists("Customer", "Shopify Guest"):
		return "Shopify Guest"
	from shopify_integration.inbound.customer import default_customer_group, default_territory

	doc = frappe.new_doc("Customer")
	doc.customer_name = "Shopify Guest"
	doc.customer_group = default_customer_group()
	doc.territory = default_territory()
	doc.insert(ignore_permissions=True)
	return doc.name


class TestSalesOrder(OrderTestCase):
	def test_creates_a_submitted_sales_order(self):
		name = order_module.create_sales_order(self.store_doc, self.order())
		so = frappe.get_doc("Sales Order", name)

		self.assertEqual(so.docstatus, 1, "the SO must be submitted -- it raises reserved_qty")
		self.assertEqual(so.shopify_order_gid, ORDER_GID)
		self.assertEqual(so.shopify_order_number, "#1001")
		self.assertEqual(len(so.items), 1)
		self.assertEqual(so.items[0].item_code, "ORD-TEE")
		self.assertEqual(so.items[0].qty, 2)

	def test_total_matches_shopify_exactly(self):
		"""The phase-3 acceptance criterion, and the whole point of §13."""
		payload = self.order()
		name = order_module.create_sales_order(self.store_doc, payload)
		so = frappe.get_doc("Sales Order", name)

		self.assertAlmostEqual(so.grand_total, 119.99, places=2)

	def test_duplicate_delivery_produces_one_sales_order(self):
		"""Mandatory case: Shopify retries for ~48h, so this is normal traffic."""
		payload = self.order(gid="gid://shopify/Order/5002")
		first = order_module.create_sales_order(self.store_doc, payload)
		second = order_module.create_sales_order(self.store_doc, payload)

		self.assertEqual(first, second)
		self.assertEqual(
			frappe.db.count("Sales Order", {"shopify_order_gid": "gid://shopify/Order/5002", "docstatus": 1}),
			1,
		)

	def test_shopify_price_wins_over_erpnext_pricing_rules(self):
		name = order_module.create_sales_order(self.store_doc, self.order(gid="gid://shopify/Order/5003"))
		so = frappe.get_doc("Sales Order", name)
		self.assertEqual(so.ignore_pricing_rule, 1)

	def test_line_discount_is_taken_as_shopify_allocated_it(self):
		payload = self.order(gid="gid://shopify/Order/5004", unit_price="50.00", discounted_price="45.00")
		name = order_module.create_sales_order(self.store_doc, payload)
		so = frappe.get_doc("Sales Order", name)

		self.assertAlmostEqual(so.items[0].rate, 45.00, places=2)
		self.assertAlmostEqual(so.items[0].price_list_rate, 50.00, places=2)

	def test_order_note_becomes_a_comment(self):
		name = order_module.create_sales_order(self.store_doc, self.order(gid="gid://shopify/Order/5005"))
		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Sales Order", "reference_name": name, "comment_type": "Comment"},
			pluck="content",
		)
		self.assertTrue(any("Leave at the door" in c for c in comments))

	def test_guest_checkout_uses_the_default_customer(self):
		name = order_module.create_sales_order(
			self.store_doc, self.order(gid="gid://shopify/Order/5006", customer=None)
		)
		self.assertEqual(frappe.db.get_value("Sales Order", name, "customer"), self.customer)

	def test_named_customer_is_created_once_and_reused(self):
		"""Never two ERPNext Customers for one Shopify GID -- it splits their ledger."""
		shopper = {
			"id": "gid://shopify/Customer/777",
			"firstName": "Ada",
			"lastName": "Lovelace",
			"email": "ada@example.com",
		}
		first = order_module.create_sales_order(
			self.store_doc, self.order(gid="gid://shopify/Order/5007", customer=shopper)
		)
		second = order_module.create_sales_order(
			self.store_doc, self.order(gid="gid://shopify/Order/5008", customer=shopper)
		)

		customer = frappe.db.get_value("Sales Order", first, "customer")
		self.assertEqual(customer, frappe.db.get_value("Sales Order", second, "customer"))
		self.assertEqual(
			frappe.db.count("Customer", {"shopify_customer_gid": "gid://shopify/Customer/777"}), 1
		)

	def test_unmapped_tax_fails_with_an_actionable_message(self):
		payload = self.order(gid="gid://shopify/Order/5009")
		payload["lineItems"]["nodes"][0]["taxLines"] = [
			{"title": "Mystery Levy", "rate": 0.1, "priceSet": bag("10.00")}
		]
		with self.assertRaises(frappe.ValidationError) as caught:
			order_module.create_sales_order(self.store_doc, payload)

		message = str(caught.exception)
		self.assertIn("Mystery Levy", message)
		self.assertIn("Tax Map", message)

	def test_unknown_sku_fails_naming_the_sku(self):
		"""'sync failed' tells a user nothing they can act on (spec §14)."""
		payload = self.order(gid="gid://shopify/Order/5010", sku="NEVER-IMPORTED", variant_gid=None)
		with self.assertRaises(frappe.ValidationError) as caught:
			order_module.create_sales_order(self.store_doc, payload)
		self.assertIn("NEVER-IMPORTED", str(caught.exception))

	def test_total_mismatch_refuses_to_post(self):
		"""Books that are quietly wrong are worse than an integration that stops."""
		payload = self.order(gid="gid://shopify/Order/5011", total="999.00")
		with self.assertRaises(frappe.ValidationError) as caught:
			order_module.create_sales_order(self.store_doc, payload)

		message = str(caught.exception)
		self.assertIn("999", message)
		self.assertIn("does not match", message)

	def test_tax_inclusive_order_reconciles(self):
		"""Tax-inclusive pricing keeps gross rates and lets ERPNext do the division.

		Subtracting the tax by hand needs a net rate of 45.455, which two-decimal currency
		precision cannot hold, so the order lands a cent out. The gross rate can be held
		exactly, so the total the customer actually paid is preserved."""
		payload = self.order(
			gid="gid://shopify/Order/5012",
			taxes_included=True,
			unit_price="50.00",
			tax="9.09",
			shipping="0.00",
			total="100.00",
		)
		name = order_module.create_sales_order(self.store_doc, payload)
		so = frappe.get_doc("Sales Order", name)

		# The rate stays gross and ERPNext removes the included tax itself, so the total the
		# customer paid survives exactly rather than landing a cent out.
		self.assertAlmostEqual(so.grand_total, 100.00, places=2)
		self.assertAlmostEqual(so.items[0].rate, 50.00, places=2)
		self.assertTrue(any(t.included_in_print_rate for t in so.taxes))
		self.assertAlmostEqual(so.net_total, 90.91, places=2)
		self.assertAlmostEqual(so.total_taxes_and_charges, 9.09, places=2)


class TestSalesInvoiceAndPayment(OrderTestCase):
	def test_paid_order_creates_invoice_and_payment(self):
		payload = self.order(gid="gid://shopify/Order/6001")
		result = order_module.create_sales_invoice(self.store_doc, payload)

		si = frappe.get_doc("Sales Invoice", result["sales_invoice"])
		self.assertEqual(si.docstatus, 1)
		self.assertEqual(si.shopify_order_gid, "gid://shopify/Order/6001")
		self.assertAlmostEqual(si.grand_total, 119.99, places=2)
		self.assertIsNotNone(result["payment_entry"])

	def test_invoice_posting_date_is_the_shopify_date(self):
		payload = self.order(gid="gid://shopify/Order/6002")
		result = order_module.create_sales_invoice(self.store_doc, payload)
		posting = frappe.db.get_value("Sales Invoice", result["sales_invoice"], "posting_date")
		self.assertEqual(str(posting), "2026-09-01")

	def test_invoicing_twice_does_not_double_invoice(self):
		payload = self.order(gid="gid://shopify/Order/6003")
		first = order_module.create_sales_invoice(self.store_doc, payload)
		second = order_module.create_sales_invoice(self.store_doc, payload)
		self.assertEqual(first["sales_invoice"], second["sales_invoice"])

	def test_partial_payment_records_only_what_was_received(self):
		"""orders/paid does not reliably mean paid in full (spec §8.4)."""
		payload = self.order(gid="gid://shopify/Order/6004", outstanding="19.99")
		payload["displayFinancialStatus"] = "PARTIALLY_PAID"
		result = order_module.create_sales_invoice(self.store_doc, payload)

		self.assertIsNotNone(result["payment_entry"])
		paid = frappe.db.get_value("Payment Entry", result["payment_entry"], "paid_amount")
		self.assertAlmostEqual(paid, 100.00, places=2)

	def test_unpaid_order_gets_an_invoice_but_no_payment(self):
		payload = self.order(gid="gid://shopify/Order/6005", outstanding="119.99")
		payload["displayFinancialStatus"] = "PENDING"
		result = order_module.create_sales_invoice(self.store_doc, payload)

		self.assertIsNotNone(result["sales_invoice"])
		self.assertIsNone(result["payment_entry"])


class TestDeliveryNotes(OrderTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_stock("ORD-TEE", cls.warehouse, 100)

	def fulfilment(self, fid, quantity, created="2026-09-02T10:00:00Z"):
		return {
			"id": fid,
			"status": "SUCCESS",
			"createdAt": created,
			"location": None,
			"fulfillmentLineItems": {
				"nodes": [
					{
						"id": f"{fid}/line/1",
						"quantity": quantity,
						"lineItem": {
							"id": "gid://shopify/LineItem/1",
							"sku": "ORD-TEE",
							"variant": {"id": self.variant_gid},
						},
					}
				]
			},
		}

	def test_full_fulfilment_creates_one_delivery_note(self):
		payload = self.order(
			gid="gid://shopify/Order/7001",
			fulfillments=[self.fulfilment("gid://shopify/Fulfillment/1", 2)],
		)
		created = order_module.create_delivery_notes(self.store_doc, payload)

		self.assertEqual(len(created), 1)
		dn = frappe.get_doc("Delivery Note", created[0])
		self.assertEqual(dn.docstatus, 1)
		self.assertEqual(dn.items[0].qty, 2)

	def test_partial_fulfilment_ships_only_what_was_fulfilled(self):
		"""The phase-3 acceptance criterion."""
		payload = self.order(
			gid="gid://shopify/Order/7002",
			fulfillments=[self.fulfilment("gid://shopify/Fulfillment/2", 1)],
		)
		created = order_module.create_delivery_notes(self.store_doc, payload)

		dn = frappe.get_doc("Delivery Note", created[0])
		self.assertEqual(dn.items[0].qty, 1, "only the fulfilled quantity may ship")

	def test_second_fulfilment_creates_a_second_delivery_note(self):
		payload = self.order(
			gid="gid://shopify/Order/7003",
			fulfillments=[self.fulfilment("gid://shopify/Fulfillment/3", 1)],
		)
		first = order_module.create_delivery_notes(self.store_doc, payload)

		payload["fulfillments"].append(self.fulfilment("gid://shopify/Fulfillment/4", 1))
		second = order_module.create_delivery_notes(self.store_doc, payload)

		self.assertEqual(len(first), 1)
		self.assertEqual(len(second), 1, "only the new fulfilment produces a note")
		self.assertNotEqual(first[0], second[0])

	def test_redelivered_fulfilment_does_not_duplicate(self):
		payload = self.order(
			gid="gid://shopify/Order/7004",
			fulfillments=[self.fulfilment("gid://shopify/Fulfillment/5", 2)],
		)
		order_module.create_delivery_notes(self.store_doc, payload)
		again = order_module.create_delivery_notes(self.store_doc, payload)
		self.assertEqual(again, [])

	def test_cancelled_fulfilment_is_ignored(self):
		fulfilment = self.fulfilment("gid://shopify/Fulfillment/6", 2)
		fulfilment["status"] = "CANCELLED"
		payload = self.order(gid="gid://shopify/Order/7005", fulfillments=[fulfilment])

		self.assertEqual(order_module.create_delivery_notes(self.store_doc, payload), [])


class TestCancellation(OrderTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_stock("ORD-TEE", cls.warehouse, 100)

	def test_cancelling_an_order_cancels_its_documents(self):
		payload = self.order(
			gid="gid://shopify/Order/8001",
			fulfillments=[
				{
					"id": "gid://shopify/Fulfillment/10",
					"status": "SUCCESS",
					"createdAt": "2026-09-02T10:00:00Z",
					"location": None,
					"fulfillmentLineItems": {
						"nodes": [
							{
								"id": "f/1",
								"quantity": 2,
								"lineItem": {
									"id": "gid://shopify/LineItem/1",
									"sku": "ORD-TEE",
									"variant": {"id": self.variant_gid},
								},
							}
						]
					},
				}
			],
		)
		so_name = order_module.create_sales_order(self.store_doc, payload)
		order_module.create_delivery_notes(self.store_doc, payload)

		cancelled = order_module.cancel_linked_documents(self.store, "gid://shopify/Order/8001")

		self.assertTrue(cancelled)
		self.assertEqual(frappe.db.get_value("Sales Order", so_name, "docstatus"), 2)

	def test_cancelling_an_unknown_order_is_a_no_op(self):
		self.assertEqual(
			order_module.cancel_linked_documents(self.store, "gid://shopify/Order/does-not-exist"), []
		)


class TestPriceListSideEffects(OrderTestCase):
	"""Creating the dedicated price list must not change site-wide selling defaults."""

	def test_creating_the_price_list_leaves_selling_settings_alone(self):
		"""ERPNext promotes a new selling price list to the site default when none is set.

		An integration silently repointing every manual Sales Order at a Shopify price list
		is not acceptable, so whatever was there before must survive.
		"""
		from shopify_integration.inbound.order import ensure_price_list

		before = frappe.db.get_single_value("Selling Settings", "selling_price_list")

		store = frappe.get_doc("Shopify Store", self.store)
		store.selling_price_list = None
		store.store_name = store.name
		ensure_price_list(store)

		after = frappe.db.get_single_value("Selling Settings", "selling_price_list")
		self.assertEqual(after, before, "Selling Settings must be untouched")


class TestConnectionReporting(OrderTestCase):
	"""Test Connection is the first button anyone presses, so its failures must be readable."""

	def test_a_rejected_shop_domain_explains_itself(self):
		from unittest.mock import patch

		from shopify_integration.exceptions import ShopifyGraphQLError
		from shopify_integration.shopify_integration.doctype.shopify_store import (
			shopify_store as store_module,
		)

		with patch(
			"shopify_integration.api.client.ShopifyClient.execute",
			side_effect=ShopifyGraphQLError('Shopify returned HTTP 404: {"errors":"Not Found"}'),
		):
			result = store_module.test_connection(self.store)

		self.assertFalse(result["ok"])
		self.assertIn("404", result["problem"])
		self.assertIn("myshopify.com", result["hint"])

	def test_a_rejected_token_says_so(self):
		from unittest.mock import patch

		from shopify_integration.exceptions import ShopifyGraphQLError
		from shopify_integration.shopify_integration.doctype.shopify_store import (
			shopify_store as store_module,
		)

		with patch(
			"shopify_integration.api.client.ShopifyClient.execute",
			side_effect=ShopifyGraphQLError("Shopify returned HTTP 401: ACCESS_DENIED"),
		):
			result = store_module.test_connection(self.store)

		self.assertFalse(result["ok"])
		self.assertIn("token", result["hint"].lower())

	def test_being_unreachable_is_distinguished_from_being_misconfigured(self):
		from unittest.mock import patch

		from shopify_integration.exceptions import ShopifyTransportError
		from shopify_integration.shopify_integration.doctype.shopify_store import (
			shopify_store as store_module,
		)

		with patch(
			"shopify_integration.api.client.ShopifyClient.execute",
			side_effect=ShopifyTransportError("connection reset"),
		):
			result = store_module.test_connection(self.store)

		self.assertFalse(result["ok"])
		self.assertIn("reach", result["problem"].lower())

	def test_a_successful_connection_reports_the_shop(self):
		from unittest.mock import patch

		from shopify_integration.shopify_integration.doctype.shopify_store import (
			shopify_store as store_module,
		)

		with patch(
			"shopify_integration.api.client.ShopifyClient.execute",
			return_value={
				"shop": {
					"name": "Acme",
					"myshopifyDomain": "acme.myshopify.com",
					"currencyCode": "EUR",
					"ianaTimezone": "Europe/Amsterdam",
				}
			},
		):
			result = store_module.test_connection(self.store)

		self.assertTrue(result["ok"])
		self.assertEqual(result["name"], "Acme")


class TestAddressBackfill(FrappeTestCase):
	"""Shopify's Admin API is eventually consistent.

	An order refetched immediately after ``orders/create`` -- which is when the handler runs --
	comes back with ``customer`` populated but the addresses still null. Nothing raises: the
	customer is created without an address, and because addresses are only written when the
	customer is new, the loss is silent and permanent.

	Every order arriving by webhook lost its addresses this way. It was invisible to every test
	that called the handler directly, because by then the refetch had caught up.
	"""

	def test_a_missing_address_is_recovered_from_the_webhook_body(self):
		from shopify_integration.inbound.order import _backfill_addresses

		order = {"id": "gid://shopify/Order/1", "shippingAddress": None, "billingAddress": None}
		payload = {
			"shipping_address": {
				"first_name": "Ada",
				"last_name": "Byron",
				"address1": "12 Analytical Way",
				"city": "London",
				"province": "England",
				"province_code": "ENG",
				"zip": "EC1A 1BB",
				"country": "United Kingdom",
				"country_code": "GB",
			},
			"billing_address": {"address1": "99 Difference Engine Road", "city": "Manchester"},
		}

		_backfill_addresses(order, payload)

		self.assertEqual(order["shippingAddress"]["address1"], "12 Analytical Way")
		self.assertEqual(order["shippingAddress"]["countryCodeV2"], "GB")
		self.assertEqual(order["shippingAddress"]["provinceCode"], "ENG")
		self.assertEqual(order["billingAddress"]["city"], "Manchester")

	def test_the_refetched_address_wins_when_there_is_one(self):
		"""The API read is newer than the webhook body; the fallback must not overwrite it."""
		from shopify_integration.inbound.order import _backfill_addresses

		order = {"shippingAddress": {"address1": "From the API"}, "billingAddress": None}
		_backfill_addresses(order, {"shipping_address": {"address1": "From the webhook"}})

		self.assertEqual(order["shippingAddress"]["address1"], "From the API")

	def test_an_order_with_no_address_anywhere_stays_empty(self):
		"""Digital goods have no shipping address, and inventing one would be worse."""
		from shopify_integration.inbound.order import _backfill_addresses

		order = {"shippingAddress": None, "billingAddress": None}
		_backfill_addresses(order, {"shipping_address": None, "billing_address": {"address1": "  "}})

		self.assertIsNone(order["shippingAddress"])
		self.assertIsNone(order["billingAddress"])


class TestCustomerGroupFallback(OrderTestCase):
	"""ERPNext's global default leaks a group node into our store, and Customer refuses it.

	`All Customer Groups` is the root of the tree. Frappe fills it into any new document with
	a `customer_group` Link field, so a Shopify Store created in the UI arrives holding it
	without anyone choosing it -- and then `Customer.validate_customer_group` throws on every
	single order. A fresh install imported nothing, and the store looked perfectly configured.
	"""

	def tearDown(self):
		frappe.db.set_value("Shopify Store", self.store, "customer_group", None)
		frappe.db.commit()
		frappe.clear_document_cache("Shopify Store", self.store)

	def _group_for(self, configured):
		frappe.db.set_value("Shopify Store", self.store, "customer_group", configured)
		frappe.db.commit()
		frappe.clear_document_cache("Shopify Store", self.store)
		return customer_module._customer_group_for(frappe.get_cached_doc("Shopify Store", self.store))

	def test_the_root_group_is_treated_as_nothing_chosen(self):
		self.assertEqual(self._group_for("All Customer Groups"), "Individual")

	def test_a_real_group_the_merchant_picked_is_honoured(self):
		self.assertEqual(self._group_for("Commercial"), "Commercial")

	def test_an_unset_group_falls_back(self):
		self.assertEqual(self._group_for(None), "Individual")

	def test_the_defaults_are_never_group_nodes(self):
		"""Whatever these return has to be something a Customer can actually hold."""
		self.assertEqual(
			frappe.db.get_value("Customer Group", customer_module.default_customer_group(), "is_group"), 0
		)
		self.assertEqual(frappe.db.get_value("Territory", customer_module.default_territory(), "is_group"), 0)

	def test_the_picker_cannot_offer_a_group_node(self):
		"""Belt and braces: the UI should not let anyone choose one in the first place."""
		field = frappe.get_meta("Shopify Store").get_field("customer_group")
		self.assertIn("is_group", field.link_filters or "")


class TestAddressesForAnExistingCustomer(FrappeTestCase):
	"""Shopify fires customers/create a fraction of a second before orders/create.

	The buyer therefore usually exists by the time the order is handled -- created from a
	customer payload, which carries no address. Addresses were only written on the branch that
	creates the customer, so in production every webhook order silently lost them. Direct-call
	tests never saw it, because no customers/create had run first.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)

	def setUp(self):
		self.gid = "gid://shopify/Customer/9900112233"
		for name in frappe.get_all("Customer", filters={"shopify_customer_gid": self.gid}, pluck="name"):
			for address in frappe.get_all(
				"Dynamic Link",
				filters={"link_doctype": "Customer", "link_name": name, "parenttype": "Address"},
				pluck="parent",
			):
				frappe.delete_doc("Address", address, force=True, ignore_permissions=True)
			frappe.delete_doc("Customer", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	tearDown = setUp

	def _order(self):
		return {
			"id": "gid://shopify/Order/5500",
			"name": "#5500",
			"customer": {"id": self.gid, "firstName": "Grace", "lastName": "Hopper"},
			"shippingAddress": {
				"address1": "1 Compiler Lane",
				"city": "New York",
				"country": "United States",
				"countryCodeV2": "US",
			},
			"billingAddress": {
				"address1": "2 Debug Avenue",
				"city": "Boston",
				"country": "United States",
				"countryCodeV2": "US",
			},
		}

	def _addresses(self, customer):
		return frappe.get_all(
			"Dynamic Link",
			filters={"link_doctype": "Customer", "link_name": customer, "parenttype": "Address"},
			pluck="parent",
		)

	def test_an_order_gives_addresses_to_a_customer_that_already_exists(self):
		from shopify_integration.inbound.customer import resolve_customer

		store_doc = frappe.get_cached_doc("Shopify Store", self.store)

		# what customers/create does: the customer, with no address
		first = resolve_customer(store_doc, {"customer": self._order()["customer"]})
		frappe.db.commit()
		self.assertEqual(self._addresses(first), [])

		# what orders/create does a moment later
		second = resolve_customer(store_doc, self._order())
		frappe.db.commit()

		self.assertEqual(second, first, "the same buyer must not be duplicated")
		self.assertEqual(len(self._addresses(second)), 2, "both addresses should now exist")

	def test_a_repeat_order_does_not_pile_up_duplicate_addresses(self):
		from shopify_integration.inbound.customer import resolve_customer

		store_doc = frappe.get_cached_doc("Shopify Store", self.store)

		customer = resolve_customer(store_doc, self._order())
		resolve_customer(store_doc, self._order())
		resolve_customer(store_doc, self._order())
		frappe.db.commit()

		self.assertEqual(len(self._addresses(customer)), 2)


class TestItemTaxTemplateRace(FrappeTestCase):
	def test_losing_a_race_to_create_a_template_still_finds_it(self):
		"""Two workers draining two 18% orders reach this together and one loses.

		Returning None there drops the whole order onto the blended-rate fallback -- the exact
		behaviour per-line templates exist to prevent -- and the only trace is an Error Log. The
		row the loser wanted exists by then, so it looks again.
		"""
		from decimal import Decimal
		from unittest.mock import MagicMock, patch

		import frappe

		from shopify_integration.utils import taxes

		doc = MagicMock()
		doc.meta.has_field.return_value = False
		doc.insert.side_effect = Exception("Duplicate entry")

		lookups = [None, "Shopify Output Tax IGST 18% - TC"]
		with (
			patch.object(frappe, "new_doc", return_value=doc),
			patch.object(frappe.db, "get_value", side_effect=lookups),
			patch.object(frappe.db, "savepoint"),
			patch.object(frappe.db, "rollback"),
			patch.object(frappe.db, "release_savepoint"),
			patch.object(frappe, "log_error") as logged,
		):
			result = taxes.ensure_item_tax_template("TC", {"Output Tax IGST - TC": Decimal("18")})

		self.assertEqual(result, "Shopify Output Tax IGST 18% - TC")
		self.assertFalse(logged.called, "a lost race is normal, not an error to log")


class TestItemTaxTemplateIsActuallyCreated(FrappeTestCase):
	"""A template for a rate nobody has booked before must actually reach the database.

	This is the test that was missing. Every other test around `ensure_item_tax_template`
	mocks `frappe.db.savepoint`, and that mock hid a real defect for weeks:

	    with frappe.db.savepoint("shopify_item_tax_template"):

	`frappe.db.savepoint(name)` issues SAVEPOINT and returns None. The context manager is a
	separate, module-level `savepoint(catch=...)`. So the `with` raised TypeError, the bare
	`except` swallowed it, and the function returned None for *every* template it was asked
	to create -- silently dropping mixed-rate orders onto blended aggregate rates, which is
	precisely what it exists to prevent.

	It went unnoticed because the test sites already held the common templates, so only a
	rate never seen before exposed it. On a fresh install nothing is held, and every
	mixed-rate order would have been wrong from the first day.
	"""

	def setUp(self):
		self.company = ensure_company()
		self.account = self._tax_account()
		# a rate nobody would have booked, so the lookup cannot mask a failed insert
		self.rate = Decimal("7.77")
		self.title = f"Shopify {self.account.split(' - ')[0]} 7.77%"
		for name in frappe.get_all(
			"Item Tax Template", filters={"title": self.title, "company": self.company}, pluck="name"
		):
			frappe.delete_doc("Item Tax Template", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _tax_account(self) -> str:
		"""A Tax account on this company, created if the site has none.

		Made rather than skipped: a skip here is what let the defect through. The test has to
		run on a plain ERPNext site as well as one carrying india_compliance.
		"""
		existing = frappe.db.get_value(
			"Account", {"company": self.company, "account_type": "Tax", "is_group": 0}, "name"
		)
		if existing:
			return existing

		parent = frappe.db.get_value(
			"Account", {"company": self.company, "is_group": 1, "root_type": "Liability"}, "name"
		)
		if not parent:
			self.skipTest("this company has no liability tree to hang a tax account on")

		doc = frappe.new_doc("Account")
		doc.account_name = "Shopify Test Output Tax"
		doc.company = self.company
		doc.parent_account = parent
		doc.account_type = "Tax"
		doc.root_type = "Liability"
		doc.is_group = 0
		doc.insert(ignore_permissions=True, ignore_if_duplicate=True)
		frappe.db.commit()
		return doc.name

	def tearDown(self):
		for name in frappe.get_all(
			"Item Tax Template", filters={"title": self.title, "company": self.company}, pluck="name"
		):
			frappe.delete_doc("Item Tax Template", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_a_brand_new_rate_gets_a_real_template(self):
		name = taxes_module.ensure_item_tax_template(self.company, {self.account: self.rate})

		self.assertIsNotNone(name, "the template was not created -- the order will blend rates")
		self.assertTrue(frappe.db.exists("Item Tax Template", name))

	def test_asking_twice_returns_the_same_template(self):
		first = taxes_module.ensure_item_tax_template(self.company, {self.account: self.rate})
		second = taxes_module.ensure_item_tax_template(self.company, {self.account: self.rate})

		self.assertEqual(first, second)

	def test_a_zero_rate_template_is_created_too(self):
		"""Freight on an order that charges no tax on freight needs one of these.

		Without it the freight line carries no template, ERPNext falls back to the order's
		tax row, and untaxed shipping is billed at whatever rate the goods carry -- 10.00 on
		a 200.00 delivery, and the Sales Order is refused for not matching the order total.
		"""
		title = f"Shopify {self.account.split(' - ')[0]} 0%"
		for existing in frappe.get_all(
			"Item Tax Template", filters={"title": title, "company": self.company}, pluck="name"
		):
			frappe.delete_doc("Item Tax Template", existing, force=True, ignore_permissions=True)
		frappe.db.commit()

		name = taxes_module.ensure_item_tax_template(
			self.company, {self.account: Decimal("0")}, taxable=False
		)
		self.assertIsNotNone(name, "no zero-rate template -- untaxed lines will pick up a rate")
		self.assertTrue(frappe.db.exists("Item Tax Template", name))

		frappe.delete_doc("Item Tax Template", name, force=True, ignore_permissions=True)
		frappe.db.commit()


class TestQuantitiesAreDrawnDown(FrappeTestCase):
	"""One item on two order lines must not ship, credit or restock twice.

	Shopify puts one variant on two lines routinely -- the same shirt bought twice with
	different engraving, or added to the cart on two separate occasions. The fulfilled
	quantity is summed per item, so reading that total afresh for every matching row shipped
	it once per row. Each row stayed inside its own Sales Order row, so ERPNext raised
	nothing: the stock ledger was simply short, and the order read fully delivered.
	"""

	def _draw_down(self, rows, quantities):
		"""The allocation the delivery-note builder performs, in isolation."""
		remaining = dict(quantities)
		out = []
		for qty, item_code in rows:
			wanted = remaining.get(item_code)
			if not wanted:
				continue
			taken = min(qty, wanted)
			if taken <= 0:
				continue
			remaining[item_code] = wanted - taken
			out.append((item_code, taken))
		return out

	def test_one_unit_across_two_rows_ships_once(self):
		allocated = self._draw_down([(1, "TEE"), (1, "TEE")], {"TEE": 1})
		self.assertEqual(allocated, [("TEE", 1)])
		self.assertEqual(sum(q for _, q in allocated), 1, "one unit fulfilled means one unit out")

	def test_two_units_across_two_rows_fill_both(self):
		allocated = self._draw_down([(1, "TEE"), (1, "TEE")], {"TEE": 2})
		self.assertEqual(sum(q for _, q in allocated), 2)

	def test_a_partial_quantity_stops_at_what_was_fulfilled(self):
		allocated = self._draw_down([(2, "TEE"), (2, "TEE")], {"TEE": 3})
		self.assertEqual(sum(q for _, q in allocated), 3)

	def test_rows_for_other_items_are_untouched(self):
		allocated = self._draw_down([(1, "TEE"), (1, "MUG")], {"TEE": 1})
		self.assertEqual(allocated, [("TEE", 1)])
