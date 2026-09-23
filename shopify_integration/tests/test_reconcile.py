"""Reconciliation and the dashboard (spec §12, §14)."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.shopify_integration.doctype.shopify_store import shopify_store as store_module
from shopify_integration.sync import engine, reconcile
from shopify_integration.tests.test_handlers import FakeClient
from shopify_integration.tests.test_inventory import LOCATION, InventoryTestCase

LEVELS_QUERY = "query inventoryLevels"
ORDERS_QUERY = "query ordersSince"


def levels_response(inventory_item_gid, available, location=LOCATION):
	return {
		"nodes": [
			{
				"id": inventory_item_gid,
				"inventoryLevels": {
					"nodes": [
						{
							"location": {"id": location},
							"quantities": [{"name": "available", "quantity": available}],
						}
					]
				},
			}
		]
	}


def orders_response(nodes):
	return {
		"orders": {
			"edges": [{"node": node} for node in nodes],
			"pageInfo": {"hasNextPage": False, "endCursor": None},
		}
	}


class ReconcileTestCase(InventoryTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.inventory_gid = frappe.db.get_value(
			"Shopify Item Link", {"store": cls.store, "item_code": "INV-TEE"}, "inventory_item_gid"
		)
		frappe.db.set_value("Shopify Store", cls.store, "reconcile_enabled", 1)
		frappe.db.commit()

	def set_bin(self, actual, reserved=0):
		name = frappe.db.get_value("Bin", {"item_code": "INV-TEE", "warehouse": self.warehouse}, "name")
		frappe.db.set_value("Bin", name, {"actual_qty": actual, "reserved_qty": reserved})


class TestInventoryReconciliation(ReconcileTestCase):
	def run_with(self, shopify_available):
		client = FakeClient({LEVELS_QUERY: levels_response(self.inventory_gid, shopify_available)})
		with (
			patch.object(reconcile.ShopifyClient, "for_store", return_value=client),
			patch.object(engine, "schedule_drain"),
		):
			return reconcile.reconcile_inventory(self.store_doc)

	def test_matching_levels_produce_no_corrections(self):
		self.set_bin(40)
		result = self.run_with(40)

		self.assertGreaterEqual(result["checked"], 1)
		self.assertEqual(result["drifted"], 0)
		self.assertEqual(result["corrected"], 0)

	def test_drift_is_found_and_a_correction_queued(self):
		self.set_bin(40)
		result = self.run_with(7)

		self.assertEqual(result["drifted"], 1)
		self.assertEqual(result["corrected"], 1)
		self.assertEqual(frappe.db.count("Shopify Sync Queue", {"operation": "inventory"}), 1)

	def test_the_correction_runs_towards_shopify_only(self):
		"""ERPNext stock is never rewritten from Shopify -- that loop has no fixed point."""
		self.set_bin(40)
		self.run_with(7)

		from shopify_integration.outbound.inventory import available_quantity

		self.assertEqual(available_quantity("INV-TEE", self.warehouse), 40)

	def test_the_summary_names_what_drifted(self):
		self.set_bin(40)
		result = self.run_with(7)

		self.assertEqual(result["examples"][0]["item_code"], "INV-TEE")
		self.assertEqual(result["examples"][0]["shopify"], 7)
		self.assertEqual(result["examples"][0]["erpnext"], 40)

	def test_sync_inventory_off_skips_the_check(self):
		frappe.db.set_value("Shopify Store", self.store, "sync_inventory", 0)
		frappe.clear_cache(doctype="Shopify Store")
		try:
			result = reconcile.reconcile_inventory(frappe.get_cached_doc("Shopify Store", self.store))
			self.assertIn("skipped", result)
		finally:
			frappe.db.set_value("Shopify Store", self.store, "sync_inventory", 1)
			frappe.clear_cache(doctype="Shopify Store")


class TestOrderReconciliation(ReconcileTestCase):
	def setUp(self):
		super().setUp() if hasattr(super(), "setUp") else None
		# _replay_order commits, so its event logs outlive FrappeTestCase's rollback and would
		# make a second run of these tests see its own previous replays as already done.
		for name in frappe.get_all(
			"Shopify Event Log", filters={"webhook_id": ["like", "reconcile::%"]}, pluck="name"
		):
			frappe.delete_doc("Shopify Event Log", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	# And afterwards, so the last replay does not linger in the Event Log list.
	tearDown = setUp

	def run_with(self, nodes):
		client = FakeClient({ORDERS_QUERY: orders_response(nodes)})
		with (
			patch.object(reconcile.ShopifyClient, "for_store", return_value=client),
			patch.object(frappe, "enqueue"),
		):
			return reconcile.reconcile_orders(self.store_doc)

	def test_a_missing_order_is_replayed(self):
		"""The safety net for dropped webhooks and downtime."""
		result = self.run_with(
			[{"id": "gid://shopify/Order/70001", "name": "#7001", "createdAt": "2026-09-10T10:00:00Z"}]
		)

		self.assertEqual(result["missing"], 1)
		self.assertEqual(result["replayed"], 1)
		self.assertTrue(
			frappe.db.exists("Shopify Event Log", {"webhook_id": "reconcile::gid://shopify/Order/70001"})
		)

	def test_replay_goes_through_the_normal_webhook_path(self):
		"""So a replayed order is logged, retryable and debuggable like any other event."""
		self.run_with(
			[{"id": "gid://shopify/Order/70002", "name": "#7002", "createdAt": "2026-09-10T10:00:00Z"}]
		)
		log = frappe.get_doc("Shopify Event Log", {"webhook_id": "reconcile::gid://shopify/Order/70002"})
		self.assertEqual(log.topic, "orders/create")
		self.assertEqual(log.status, "Queued")

	def test_replaying_twice_does_not_queue_twice(self):
		node = {"id": "gid://shopify/Order/70003", "name": "#7003", "createdAt": "2026-09-10T10:00:00Z"}
		first = self.run_with([node])
		second = self.run_with([node])

		self.assertEqual(first["replayed"], 1)
		self.assertEqual(second["replayed"], 0, "the event log id is the dedupe")

	def test_a_cancelled_order_is_not_replayed(self):
		result = self.run_with(
			[
				{
					"id": "gid://shopify/Order/70004",
					"name": "#7004",
					"createdAt": "2026-09-10T10:00:00Z",
					"cancelledAt": "2026-09-11T10:00:00Z",
				}
			]
		)
		self.assertEqual(result["replayed"], 0)

	def test_an_order_already_in_erpnext_is_not_replayed(self):
		from shopify_integration.tests.test_orders import _ensure_customer

		order_gid = "gid://shopify/Order/70005"
		company_currency = frappe.get_cached_value("Company", self.store_doc.company, "default_currency")

		# This SO is committed by the insert below and outlives the rollback, so a second run
		# would collide with it on the unique Shopify order index.
		for stale in frappe.get_all("Sales Order", filters={"shopify_order_gid": order_gid}, pluck="name"):
			doc = frappe.get_doc("Sales Order", stale)
			if doc.docstatus == 1:
				doc.cancel()
			frappe.delete_doc("Sales Order", stale, force=True, ignore_permissions=True)

		so = frappe.new_doc("Sales Order")
		so.customer = _ensure_customer()
		so.company = self.store_doc.company
		# Set the currency explicitly to the company's. Picking an arbitrary customer can
		# land on one whose own currency differs, and ERPNext then wants an exchange rate
		# this test has no reason to care about.
		so.currency = company_currency
		so.conversion_rate = 1
		so.shopify_store = self.store
		so.shopify_order_gid = order_gid
		so.transaction_date = frappe.utils.nowdate()
		so.delivery_date = frappe.utils.nowdate()
		so.append("items", {"item_code": "INV-TEE", "qty": 1, "rate": 10, "warehouse": self.warehouse})
		so.flags.ignore_mandatory = True
		so.insert(ignore_permissions=True)

		result = self.run_with([{"id": order_gid, "name": "#7005", "createdAt": "2026-09-10T10:00:00Z"}])
		self.assertEqual(result["missing"], 0)


class TestDashboard(ReconcileTestCase):
	def test_status_reports_queue_and_event_counts(self):
		with patch.object(engine, "schedule_drain"):
			engine.enqueue_sync(self.store, "inventory", f"inventory:{self.store}:INV-TEE:{LOCATION}")

		status = store_module.store_status(self.store)

		self.assertIn("queue", status)
		self.assertGreaterEqual(status["queue"]["pending"], 1)
		self.assertIn("events", status)

	def test_status_survives_an_unreachable_throttle(self):
		"""The dashboard must render even when Redis is down."""
		with patch(
			"shopify_integration.api.throttle.AdaptiveThrottle.read_state",
			side_effect=RuntimeError("redis down"),
		):
			status = store_module.store_status(self.store)
		self.assertIsNone(status["api_headroom"])

	def test_reconcile_store_records_its_summary_on_the_store(self):
		client = FakeClient(
			{
				LEVELS_QUERY: levels_response(self.inventory_gid, 40),
				ORDERS_QUERY: orders_response([]),
			}
		)
		self.set_bin(40)
		with (
			patch.object(reconcile.ShopifyClient, "for_store", return_value=client),
			patch.object(engine, "schedule_drain"),
		):
			reconcile.reconcile_store(self.store)

		doc = frappe.get_doc("Shopify Store", self.store)
		self.assertIsNotNone(doc.last_reconciled_on)
		self.assertIn("inventory", doc.last_reconciliation_summary)

	def test_reconcile_all_stores_honours_the_toggle(self):
		frappe.db.set_value("Shopify Store", self.store, "reconcile_enabled", 0)
		frappe.clear_cache(doctype="Shopify Store")
		try:
			result = reconcile.reconcile_all_stores()
			self.assertNotIn(self.store, result)
		finally:
			frappe.db.set_value("Shopify Store", self.store, "reconcile_enabled", 1)
			frappe.clear_cache(doctype="Shopify Store")


class TestTheReconciliationWindow(FrappeTestCase):
	"""The window handed to Shopify has to be real UTC.

	`last_reconciled_on` is naive local time. Stamping a `Z` on it tells Shopify it is already
	UTC, and east of UTC that pushes each window's start *forward*: an IST site reconciling at
	03:00 next asks for everything after 08:30 IST, and the 5h30m in between is never checked
	by any run. Since this is the net that catches dropped webhooks, an order lost there is
	lost permanently.
	"""

	def test_a_local_timestamp_is_converted_not_relabelled(self):
		from datetime import datetime
		from zoneinfo import ZoneInfo

		from shopify_integration.sync.reconcile import _as_utc

		local = datetime(2026, 9, 18, 3, 0, 0)
		with (
			patch("frappe.utils.get_system_timezone", return_value="Asia/Kolkata"),
			patch("shopify_integration.sync.reconcile.get_system_timezone", return_value="Asia/Kolkata"),
		):
			self.assertEqual(_as_utc(local), "2026-09-17T21:30:00Z")

		# and the same instant, already tagged UTC, is left alone
		self.assertEqual(_as_utc(local.replace(tzinfo=ZoneInfo("UTC"))), "2026-09-18T03:00:00Z")

	def test_an_already_aware_timestamp_is_left_where_it_is(self):
		from datetime import datetime, timezone

		from shopify_integration.sync.reconcile import _as_utc

		aware = datetime(2026, 9, 18, 3, 0, 0, tzinfo=timezone.utc)
		self.assertEqual(_as_utc(aware), "2026-09-18T03:00:00Z")

	def test_a_utc_site_is_unaffected(self):
		from datetime import datetime

		from shopify_integration.sync.reconcile import _as_utc

		local = datetime(2026, 9, 18, 3, 0, 0)
		with patch("shopify_integration.sync.reconcile.get_system_timezone", return_value="UTC"):
			self.assertEqual(_as_utc(local), "2026-09-18T03:00:00Z")
