"""Reserved stock moves in more ways than submit and cancel.

Closing an order frees its units, re-opening takes them back, and Update Items changes them on
an order already submitted. None of those is a submit or a cancel, so Shopify stayed short --
or long -- until the 03:00 reconciliation noticed. Three orders closed by hand on the test site
freed one saree and two earrings that the website never heard about.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from shopify_integration.tests.test_orders import OrderTestCase


class TestTheHooksAreRegistered(FrappeTestCase):
	"""Both events are needed, and for different reasons -- see hooks.py."""

	def test_close_and_update_items_both_reach_the_handler(self):
		events = frappe.get_hooks("doc_events").get("Sales Order", {})
		handler = "shopify_integration.outbound.inventory.on_reservation_change"

		for event in ("on_submit", "on_cancel", "on_change", "on_update_after_submit"):
			registered = events.get(event) or []
			registered = registered if isinstance(registered, list) else [registered]
			self.assertIn(handler, registered, f"{event} should reach the reservation handler")


class TestReservationPushes(OrderTestCase):
	def setUp(self):
		self.made = []
		# Freshly fetched: the class-level store_doc is shared across tests and is stale the
		# moment a previous tearDown saved it.
		store = frappe.get_doc("Shopify Store", self.store)
		store.sync_inventory = 1
		store.set("location_map", [])
		store.append(
			"location_map",
			{
				"location_gid": "gid://shopify/Location/1",
				"location_name": "Shop",
				"warehouse": self.warehouse,
			},
		)
		store.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_document_cache("Shopify Store", self.store)

	def tearDown(self):
		for name in reversed(self.made):
			try:
				doc = frappe.get_doc("Sales Order", name)
				if doc.docstatus == 1:
					doc.cancel()
			except Exception:
				pass
			frappe.delete_doc("Sales Order", name, force=True, ignore_permissions=True, ignore_missing=True)
		frappe.clear_document_cache("Shopify Store", self.store)
		doc = frappe.get_doc("Shopify Store", self.store)
		doc.sync_inventory = 0
		doc.set("location_map", [])
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_document_cache("Shopify Store", self.store)

	def _queued(self):
		return frappe.db.count("Shopify Sync Queue", {"store": self.store, "operation": "inventory"})

	def _clear_queue(self):
		frappe.db.delete("Shopify Sync Queue", {"store": self.store})
		frappe.db.commit()

	def _reserved(self):
		return flt(
			frappe.db.get_value("Bin", {"item_code": "ORD-TEE", "warehouse": self.warehouse}, "reserved_qty")
		)

	def _fresh(self, name):
		"""Submitting writes more to the order afterwards, so the cached copy is already stale
		and update_status would refuse it on the modified timestamp."""
		frappe.clear_document_cache("Sales Order", name)
		return frappe.get_doc("Sales Order", name)

	def _order(self, qty=2):
		doc = frappe.new_doc("Sales Order")
		doc.customer = self.customer
		company = frappe.db.get_value("Shopify Store", self.store, "company")
		doc.company = company
		doc.currency = frappe.get_cached_value("Company", company, "default_currency")
		doc.conversion_rate = 1
		doc.transaction_date = frappe.utils.nowdate()
		doc.delivery_date = frappe.utils.add_days(frappe.utils.nowdate(), 3)
		doc.append(
			"items",
			{
				"item_code": "ORD-TEE",
				"qty": qty,
				"rate": 50,
				"warehouse": self.warehouse,
				"delivery_date": doc.delivery_date,
			},
		)
		doc.insert(ignore_permissions=True)
		doc.submit()
		self.made.append(doc.name)
		frappe.db.commit()
		return doc.name

	def test_closing_an_order_frees_the_stock_and_tells_shopify(self):
		name = self._order()
		reserved_open = self._reserved()
		self._clear_queue()

		self._fresh(name).update_status("Closed")
		frappe.db.commit()

		self.assertLess(self._reserved(), reserved_open, "closing should free the reservation")
		self.assertTrue(self._queued(), "closing should queue an inventory push")

	def test_reopening_takes_the_stock_back_and_tells_shopify(self):
		name = self._order()
		self._fresh(name).update_status("Closed")
		frappe.db.commit()
		reserved_closed = self._reserved()
		self._clear_queue()

		self._fresh(name).update_status("Draft")
		frappe.db.commit()

		self.assertGreater(self._reserved(), reserved_closed, "re-opening should reserve again")
		self.assertTrue(self._queued(), "re-opening should queue an inventory push")

	def test_lowering_the_quantity_on_a_submitted_order_tells_shopify(self):
		"""Update Items goes through parent.save() on a submitted document, which fires
		on_update_after_submit and not on_change."""
		from erpnext.controllers.accounts_controller import update_child_qty_rate

		name = self._order(qty=2)
		reserved_two = self._reserved()
		self._clear_queue()

		row = self._fresh(name).items[0]
		update_child_qty_rate(
			"Sales Order",
			frappe.as_json([{"docname": row.name, "item_code": "ORD-TEE", "qty": 1, "rate": 50}]),
			name,
		)
		frappe.db.commit()

		self.assertLess(self._reserved(), reserved_two, "one fewer unit should be reserved")
		self.assertTrue(self._queued(), "Update Items should queue an inventory push")

	def test_a_draft_order_does_not_push(self):
		"""on_change fires on every field written to a draft, and a draft reserves nothing."""
		self._clear_queue()
		doc = frappe.new_doc("Sales Order")
		doc.customer = self.customer
		company = frappe.db.get_value("Shopify Store", self.store, "company")
		doc.company = company
		doc.currency = frappe.get_cached_value("Company", company, "default_currency")
		doc.conversion_rate = 1
		doc.transaction_date = frappe.utils.nowdate()
		doc.delivery_date = frappe.utils.add_days(frappe.utils.nowdate(), 3)
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
		self.made.append(doc.name)
		doc.db_set("customer_name", "Touched")
		frappe.db.commit()

		self.assertEqual(self._queued(), 0, "a draft holds no reservation to push")
