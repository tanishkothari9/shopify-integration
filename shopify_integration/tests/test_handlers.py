"""Webhook handler coverage (spec §8, §14).

The builders underneath these are tested hard elsewhere. What is tested *here* is the thin
layer around them: read the stored payload, honour the store's toggle, fetch from Shopify,
call the builder, and record the outcome on the event log.

Thin is not the same as safe. A bug in this layer produces the worst failure mode the spec
names -- an event log that says Success while nothing happened -- so every handler is driven
end to end with Shopify stubbed out.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.inbound import customer as customer_module
from shopify_integration.inbound import inventory as inventory_module
from shopify_integration.inbound import order as order_module
from shopify_integration.inbound import refund as refund_module
from shopify_integration.sync import engine
from shopify_integration.tests.test_orders import (
	OrderTestCase,
	clear_shopify_documents,
	ensure_stock,
)
from shopify_integration.tests.test_refunds import build_refund


class FakeClient:
	"""Stands in for ShopifyClient, returning canned responses keyed by document name.

	Keyed by the operation name inside the query text, so a handler that fetches the wrong
	document fails loudly here rather than quietly returning someone else's data.
	"""

	def __init__(self, responses: dict):
		self.responses = responses
		self.calls: list[dict] = []

	def execute(self, query, variables=None, **kwargs):
		self.calls.append({"query": query, "variables": variables})
		for key, response in self.responses.items():
			if key in query:
				return response
		raise AssertionError(f"FakeClient has no response for query starting: {query[:80]!r}")

	def paginate(self, query, variables, connection_path, **kwargs):
		"""Mirrors ShopifyClient.paginate over the canned response.

		Walking the same edges/pageInfo shape the real client does, so a caller that relies on
		pagination is genuinely exercised rather than handed a plain list.
		"""
		data = self.execute(query, dict(variables or {}, cursor=None))
		node = data
		for part in connection_path.split("."):
			node = (node or {}).get(part)
		for edge in (node or {}).get("edges") or []:
			if edge.get("node") is not None:
				yield edge["node"]


def log_for(store: str, topic: str, payload: dict) -> str:
	"""An event log row as the receiver would have written it."""
	doc = frappe.new_doc("Shopify Event Log")
	doc.store = store
	doc.webhook_id = frappe.generate_hash(length=16)
	doc.topic = topic
	doc.payload = json.dumps(payload)
	doc.status = "Queued"
	doc.insert(ignore_permissions=True)
	# Committed, as the receiver commits it: handlers are enqueued with enqueue_after_commit,
	# so a handler never sees an uncommitted event log. It matters because mark_error rolls back
	# the failed handler's work before recording the error, and an uncommitted fixture would go
	# with it.
	frappe.db.commit()
	return doc.name


class HandlerTestCase(OrderTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.db.set_value("Shopify Store", cls.store, "sync_refunds", 1)
		ensure_stock("ORD-TEE", cls.warehouse, 200)
		clear_shopify_documents(cls.store)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		# These tests commit, so clearing up afterwards is the class's own job -- clearing only
		# on the way in would leave the last run's rows sitting in the real Event Log list.
		clear_shopify_documents(cls.store)
		super().tearDownClass()

	def run_handler(self, handler, module, event_log, responses):
		with patch.object(module.ShopifyClient, "for_store", return_value=FakeClient(responses)):
			return handler(event_log)


class TestOrderHandlers(HandlerTestCase):
	def test_order_create_builds_a_sales_order_and_marks_the_log(self):
		payload = {"id": 11001, "admin_graphql_api_id": "gid://shopify/Order/11001"}
		event = log_for(self.store, "orders/create", payload)
		order = self.order(gid="gid://shopify/Order/11001")

		with patch.object(engine, "schedule_drain"):
			result = self.run_handler(
				order_module.on_order_create, order_module, event, {"query order(": {"order": order}}
			)

		self.assertIn("sales_order", result)
		log = frappe.get_doc("Shopify Event Log", event)
		self.assertEqual(log.status, "Success")
		self.assertEqual(log.ref_doctype, "Sales Order")
		self.assertEqual(log.ref_docname, result["sales_order"])

	def test_order_create_respects_the_sync_orders_toggle(self):
		frappe.db.set_value("Shopify Store", self.store, "sync_orders", 0)
		frappe.clear_cache(doctype="Shopify Store")
		try:
			event = log_for(self.store, "orders/create", {"id": 11002})
			result = order_module.on_order_create(event)

			self.assertIn("skipped", result)
			self.assertEqual(frappe.db.get_value("Shopify Event Log", event, "status"), "Success")
		finally:
			frappe.db.set_value("Shopify Store", self.store, "sync_orders", 1)
			frappe.clear_cache(doctype="Shopify Store")

	def test_a_deleted_order_is_not_an_error(self):
		"""Created and deleted before we got to it. Nothing to do, and nothing wrong."""
		event = log_for(self.store, "orders/create", {"id": 11003})
		with patch.object(engine, "schedule_drain"):
			result = self.run_handler(
				order_module.on_order_create, order_module, event, {"query order(": {"order": None}}
			)

		self.assertIn("skipped", result)
		self.assertEqual(frappe.db.get_value("Shopify Event Log", event, "status"), "Success")

	def test_a_failing_handler_marks_the_log_error_and_reraises(self):
		"""The failure that must never be silent: the log has to show what went wrong."""
		event = log_for(self.store, "orders/create", {"id": 11004})
		broken = self.order(gid="gid://shopify/Order/11004", sku="NEVER-IMPORTED", variant_gid=None)

		with patch.object(engine, "schedule_drain"), self.assertRaises(frappe.ValidationError):
			self.run_handler(
				order_module.on_order_create, order_module, event, {"query order(": {"order": broken}}
			)

		log = frappe.get_doc("Shopify Event Log", event)
		self.assertEqual(log.status, "Error")
		self.assertIn("NEVER-IMPORTED", log.traceback)

	def test_order_paid_builds_an_invoice(self):
		event = log_for(self.store, "orders/paid", {"id": 11005})
		order = self.order(gid="gid://shopify/Order/11005")

		with patch.object(engine, "schedule_drain"):
			result = self.run_handler(
				order_module.on_order_paid, order_module, event, {"query order(": {"order": order}}
			)

		self.assertIsNotNone(result["sales_invoice"])
		self.assertEqual(frappe.db.get_value("Shopify Event Log", event, "status"), "Success")

	def test_order_fulfilled_builds_a_delivery_note(self):
		order_gid = "gid://shopify/Order/11006"
		event = log_for(self.store, "orders/fulfilled", {"id": 11006})
		order = self.order(
			gid=order_gid,
			fulfillments=[
				{
					"id": f"{order_gid}/f/1",
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

		with patch.object(engine, "schedule_drain"):
			result = self.run_handler(
				order_module.on_order_fulfilled, order_module, event, {"query order(": {"order": order}}
			)

		self.assertEqual(len(result["delivery_notes"]), 1)
		self.assertEqual(frappe.db.get_value("Shopify Event Log", event, "status"), "Success")

	def test_order_cancelled_cancels_the_documents(self):
		order_gid = "gid://shopify/Order/11007"
		order = self.order(gid=order_gid)
		with patch.object(engine, "schedule_drain"):
			so_name = order_module.create_sales_order(self.store_doc, order)

		event = log_for(self.store, "orders/cancelled", {"id": 11007})
		with patch.object(engine, "schedule_drain"):
			result = self.run_handler(
				order_module.on_order_cancelled,
				order_module,
				event,
				{"query order(": {"order": order}},
			)

		self.assertTrue(result["cancelled"])
		self.assertEqual(frappe.db.get_value("Sales Order", so_name, "docstatus"), 2)


class TestRefundHandler(HandlerTestCase):
	def test_refund_create_builds_a_credit_note(self):
		order_gid = "gid://shopify/Order/12001"
		order = self.order(gid=order_gid)
		with patch.object(engine, "schedule_drain"):
			order_module.create_sales_invoice(self.store_doc, order)

		event = log_for(self.store, "refunds/create", {"id": 12001})
		refund = build_refund(
			gid="gid://shopify/Refund/12001",
			order_gid=order_gid,
			qty=1,
			variant_gid=self.variant_gid,
		)

		with patch.object(engine, "schedule_drain"):
			result = self.run_handler(
				refund_module.on_refund_create, refund_module, event, {"query refund(": {"refund": refund}}
			)

		self.assertIsNotNone(result["credit_note"])
		log = frappe.get_doc("Shopify Event Log", event)
		self.assertEqual(log.status, "Success")
		self.assertEqual(log.ref_doctype, "Sales Invoice")

	def test_refund_respects_the_sync_refunds_toggle(self):
		frappe.db.set_value("Shopify Store", self.store, "sync_refunds", 0)
		frappe.clear_cache(doctype="Shopify Store")
		try:
			event = log_for(self.store, "refunds/create", {"id": 12002})
			result = refund_module.on_refund_create(event)
			self.assertIn("skipped", result)
		finally:
			frappe.db.set_value("Shopify Store", self.store, "sync_refunds", 1)
			frappe.clear_cache(doctype="Shopify Store")


class TestInventoryHandler(HandlerTestCase):
	def test_inventory_level_update_records_the_outcome(self):
		link = frappe.db.get_value(
			"Shopify Item Link", {"store": self.store, "item_code": "ORD-TEE"}, "inventory_item_gid"
		)
		event = log_for(
			self.store,
			"inventory_levels/update",
			{"inventory_item_id": link.split("/")[-1], "location_id": "555", "available": 3},
		)

		with patch.object(engine, "schedule_drain"):
			result = inventory_module.on_inventory_level_update(event)

		self.assertIsInstance(result, dict)
		self.assertEqual(frappe.db.get_value("Shopify Event Log", event, "status"), "Success")


class TestCustomerHandler(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from shopify_integration.tests.test_integration import SECRET_A, make_store

		cls.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)

	def test_customer_create_makes_a_customer(self):
		event = log_for(
			self.store,
			"customers/create",
			{
				"id": 44001,
				"admin_graphql_api_id": "gid://shopify/Customer/44001",
				"first_name": "Grace",
				"last_name": "Hopper",
				"email": "grace@example.com",
			},
		)
		result = customer_module.on_customer_create(event)

		self.assertIsNotNone(result["customer"])
		self.assertEqual(
			frappe.db.get_value("Customer", result["customer"], "shopify_customer_gid"),
			"gid://shopify/Customer/44001",
		)
		self.assertEqual(frappe.db.get_value("Shopify Event Log", event, "status"), "Success")

	def test_customer_update_does_not_duplicate(self):
		payload = {
			"id": 44002,
			"admin_graphql_api_id": "gid://shopify/Customer/44002",
			"first_name": "Ada",
			"last_name": "Lovelace",
			"email": "ada2@example.com",
		}
		customer_module.on_customer_create(log_for(self.store, "customers/create", payload))

		payload["last_name"] = "Byron"
		customer_module.on_customer_update(log_for(self.store, "customers/update", payload))

		self.assertEqual(
			frappe.db.count("Customer", {"shopify_customer_gid": "gid://shopify/Customer/44002"}), 1
		)

	def test_payload_without_a_customer_id_is_recorded_as_an_error(self):
		event = log_for(self.store, "customers/create", {"first_name": "Nobody"})
		result = customer_module.on_customer_create(event)

		self.assertIn("skipped", result)
		self.assertEqual(frappe.db.get_value("Shopify Event Log", event, "status"), "Error")
