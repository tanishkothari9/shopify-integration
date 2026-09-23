"""Fulfilment: ERPNext -> Shopify (Delivery Note ships it, Shipment tracks it)."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.outbound import fulfillment
from shopify_integration.sync import engine
from shopify_integration.tests.test_integration import SECRET_A, make_store
from shopify_integration.tests.test_orders import OrderTestCase


class TestResolvingTheShopifyOrder(FrappeTestCase):
	"""Which Shopify order a Delivery Note belongs to.

	The first version of this read `shopify_order_gid` off the note and stopped there, and it
	fulfilled nothing whatsoever. The field is `no_copy`, so a note raised the ordinary way --
	from the Sales Order, in the UI, which is how every real shipment is made -- does not carry
	it. Only notes this app creates itself do.
	"""

	def test_the_notes_own_fields_win_when_it_has_them(self):
		note = frappe._dict(
			{
				"shopify_store": "Store A",
				"shopify_order_gid": "gid://shopify/Order/1",
				"items": [],
			}
		)
		self.assertEqual(fulfillment.shopify_order_for(note), ("Store A", "gid://shopify/Order/1"))

	def test_a_note_without_them_is_resolved_through_its_sales_order(self):
		store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)
		order_gid = "gid://shopify/Order/88001"
		sales_order = frappe.get_all("Sales Order", filters={"docstatus": ["<", 2]}, limit=1, pluck="name")
		if not sales_order:
			self.skipTest("no Sales Order on this site to hang the test on")

		frappe.db.set_value(
			"Sales Order",
			sales_order[0],
			{"shopify_store": store, "shopify_order_gid": order_gid},
			update_modified=False,
		)
		note = frappe._dict(
			{
				"shopify_store": None,
				"shopify_order_gid": None,
				"items": [frappe._dict({"against_sales_order": sales_order[0]})],
			}
		)

		self.assertEqual(fulfillment.shopify_order_for(note), (store, order_gid))

	def test_an_ordinary_delivery_note_belongs_to_no_shopify_order(self):
		note = frappe._dict(
			{
				"shopify_store": None,
				"shopify_order_gid": None,
				"items": [frappe._dict({"against_sales_order": None})],
			}
		)
		self.assertEqual(fulfillment.shopify_order_for(note), (None, None))


class TestTracking(FrappeTestCase):
	"""Carrier and AWB come from the Shipment covering the note, and only once it is submitted."""

	def test_no_shipment_means_no_tracking(self):
		self.assertIsNone(fulfillment.tracking_for("DN-DOES-NOT-EXIST"))

	def test_a_draft_shipment_is_not_tracking_yet(self):
		"""A Shipment in draft is a plan, not a dispatch. Sending its number would tell the
		customer their parcel is moving before anyone has handed it over."""
		with (
			patch.object(frappe.db, "table_exists", return_value=True),
			patch.object(frappe, "get_all", return_value=["SHIPMENT-DRAFT"]),
			patch.object(
				frappe.db,
				"get_value",
				return_value=frappe._dict(
					{"docstatus": 0, "carrier": "Blue Dart", "awb_number": "BD1", "tracking_url": None}
				),
			),
		):
			self.assertIsNone(fulfillment.tracking_for("DN-1"))

	def test_a_submitted_shipment_yields_carrier_and_number(self):
		with (
			patch.object(frappe.db, "table_exists", return_value=True),
			patch.object(frappe, "get_all", return_value=["SHIPMENT-1"]),
			patch.object(
				frappe.db,
				"get_value",
				return_value=frappe._dict(
					{
						"docstatus": 1,
						"carrier": "Blue Dart",
						"awb_number": "BD12345",
						"tracking_url": "https://bluedart.com/track/BD12345",
					}
				),
			),
		):
			self.assertEqual(
				fulfillment.tracking_for("DN-1"),
				{
					"number": "BD12345",
					"company": "Blue Dart",
					"url": "https://bluedart.com/track/BD12345",
				},
			)

	def test_a_shipment_with_nothing_filled_in_is_not_tracking(self):
		"""None rather than an empty dict, so the caller can tell "no tracking yet" from
		"tracking that happens to be blank" -- sending the latter would wipe a number the
		merchant had typed into Shopify by hand."""
		with (
			patch.object(frappe.db, "table_exists", return_value=True),
			patch.object(frappe, "get_all", return_value=["SHIPMENT-2"]),
			patch.object(
				frappe.db,
				"get_value",
				return_value=frappe._dict(
					{"docstatus": 1, "carrier": "", "awb_number": "  ", "tracking_url": None}
				),
			),
		):
			self.assertIsNone(fulfillment.tracking_for("DN-2"))

	def test_a_site_without_the_shipment_doctype_is_fine(self):
		"""ERPNext ships Shipment, but a site can disable it. Its absence means no tracking,
		not a failure."""
		with patch.object(frappe.db, "table_exists", return_value=False):
			self.assertIsNone(fulfillment.tracking_for("DN-3"))


class TestFulfilmentTrigger(OrderTestCase):
	"""What does and does not reach the queue."""

	def setUp(self):
		frappe.db.delete("Shopify Sync Queue", {"store": self.store})
		frappe.db.commit()

	def tearDown(self):
		frappe.db.set_value("Shopify Store", self.store, "sync_fulfillments", 0)
		frappe.db.commit()
		frappe.clear_document_cache("Shopify Store", self.store)

	def _note(self):
		return frappe._dict(
			{
				"name": "DN-TRIGGER-1",
				"flags": frappe._dict(),
				"shopify_store": self.store,
				"shopify_order_gid": "gid://shopify/Order/9100",
				"items": [],
			}
		)

	def _queued(self):
		return frappe.db.count("Shopify Sync Queue", {"store": self.store, "operation": "fulfillment"})

	def test_nothing_is_pushed_while_the_setting_is_off(self):
		"""Off by default, because turning it on emails customers."""
		frappe.db.set_value("Shopify Store", self.store, "sync_fulfillments", 0)
		frappe.db.commit()
		frappe.clear_document_cache("Shopify Store", self.store)

		with patch.object(engine, "schedule_drain"):
			fulfillment.on_delivery_note_submit(self._note())

		self.assertEqual(self._queued(), 0)

	def test_it_is_pushed_once_the_setting_is_on(self):
		frappe.db.set_value("Shopify Store", self.store, "sync_fulfillments", 1)
		frappe.db.commit()
		frappe.clear_document_cache("Shopify Store", self.store)

		with patch.object(engine, "schedule_drain"):
			fulfillment.on_delivery_note_submit(self._note())

		self.assertEqual(self._queued(), 1)

	def test_a_note_shopify_itself_created_does_not_bounce_back(self):
		"""Shopify tells us an order was fulfilled, we write the Delivery Note, and without
		this we would turn round and tell Shopify it was fulfilled."""
		frappe.db.set_value("Shopify Store", self.store, "sync_fulfillments", 1)
		frappe.db.commit()
		frappe.clear_document_cache("Shopify Store", self.store)

		note = self._note()
		note.flags.from_shopify = True
		with patch.object(engine, "schedule_drain"):
			fulfillment.on_delivery_note_submit(note)

		self.assertEqual(self._queued(), 0)

	def test_a_delivery_note_with_no_shopify_order_is_ignored(self):
		frappe.db.set_value("Shopify Store", self.store, "sync_fulfillments", 1)
		frappe.db.commit()
		frappe.clear_document_cache("Shopify Store", self.store)

		note = self._note()
		note.shopify_store = None
		note.shopify_order_gid = None
		with patch.object(engine, "schedule_drain"):
			fulfillment.on_delivery_note_submit(note)

		self.assertEqual(self._queued(), 0)


class TestQuantityMatching(FrappeTestCase):
	"""Never ask Shopify to fulfil more than it says is still owed."""

	def test_a_partial_shipment_fulfils_only_what_shipped(self):
		client = _FakeClient(
			{
				"order": {
					"fulfillmentOrders": {
						"nodes": [
							{
								"id": "gid://shopify/FulfillmentOrder/1",
								"status": "OPEN",
								"lineItems": {
									"nodes": [
										{
											"id": "gid://shopify/FulfillmentOrderLineItem/1",
											"remainingQuantity": 5,
											"lineItem": {"variant": {"id": "gid://shopify/ProductVariant/7"}},
										}
									]
								},
							}
						]
					}
				}
			}
		)
		grouped = fulfillment._line_items_to_fulfil(
			client, "gid://shopify/Order/1", {"gid://shopify/ProductVariant/7": 2}
		)
		self.assertEqual(grouped[0]["fulfillmentOrderLineItems"][0]["quantity"], 2)

	def test_it_never_exceeds_what_shopify_says_remains(self):
		"""A note for three against an order Shopify has already seen two of would be refused
		outright, losing the one unit that was genuinely new."""
		client = _FakeClient(
			{
				"order": {
					"fulfillmentOrders": {
						"nodes": [
							{
								"id": "gid://shopify/FulfillmentOrder/1",
								"status": "OPEN",
								"lineItems": {
									"nodes": [
										{
											"id": "gid://shopify/FulfillmentOrderLineItem/1",
											"remainingQuantity": 1,
											"lineItem": {"variant": {"id": "gid://shopify/ProductVariant/7"}},
										}
									]
								},
							}
						]
					}
				}
			}
		)
		grouped = fulfillment._line_items_to_fulfil(
			client, "gid://shopify/Order/1", {"gid://shopify/ProductVariant/7": 3}
		)
		self.assertEqual(grouped[0]["fulfillmentOrderLineItems"][0]["quantity"], 1)

	def test_a_closed_fulfilment_order_is_left_alone(self):
		client = _FakeClient(
			{
				"order": {
					"fulfillmentOrders": {
						"nodes": [
							{
								"id": "gid://shopify/FulfillmentOrder/1",
								"status": "CLOSED",
								"lineItems": {
									"nodes": [
										{
											"id": "gid://shopify/FulfillmentOrderLineItem/1",
											"remainingQuantity": 5,
											"lineItem": {"variant": {"id": "gid://shopify/ProductVariant/7"}},
										}
									]
								},
							}
						]
					}
				}
			}
		)
		self.assertEqual(
			fulfillment._line_items_to_fulfil(
				client, "gid://shopify/Order/1", {"gid://shopify/ProductVariant/7": 2}
			),
			[],
		)


class _FakeClient:
	def __init__(self, response):
		self.response = response

	def execute(self, query, variables, cost_hint=0):
		return self.response
