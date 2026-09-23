"""Real-time inventory against a real site (spec §10, §16).

The phase-4 acceptance criteria drive these: a stock movement reaches the queue immediately,
and a 20-line invoice produces one queue row per item rather than twenty API calls.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.catalogue import mapping
from shopify_integration.inbound import inventory as drift
from shopify_integration.outbound import inventory as inv
from shopify_integration.sync import engine
from shopify_integration.tests.test_catalogue import simple_product
from shopify_integration.tests.test_integration import SECRET_A, make_store
from shopify_integration.tests.test_orders import ensure_stock

LOCATION = "gid://shopify/Location/555"


class InventoryTestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)
		cls.store_doc = frappe.get_doc("Shopify Store", cls.store)
		company = cls.store_doc.company

		cls.warehouse = frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")
		cls.other_warehouse = frappe.db.get_value(
			"Warehouse", {"company": company, "is_group": 0, "name": ["!=", cls.warehouse]}, "name"
		)

		cls.store_doc.default_warehouse = cls.warehouse
		cls.store_doc.sync_inventory = 1
		cls.store_doc.set("location_map", [])
		cls.store_doc.append("location_map", {"warehouse": cls.warehouse, "location_gid": LOCATION})
		cls.store_doc.flags.ignore_mandatory = True
		cls.store_doc.save(ignore_permissions=True)

		mapping.write_product_mapping(cls.store, simple_product(sku="INV-TEE"))
		ensure_stock("INV-TEE", cls.warehouse, 100)
		frappe.db.commit()

	def setUp(self):
		frappe.db.delete("Shopify Sync Queue")


class TestQuantityFormula(InventoryTestCase):
	"""available = actual_qty - reserved_qty, floored (spec §10.1)."""

	def set_bin(self, actual, reserved):
		name = frappe.db.get_value("Bin", {"item_code": "INV-TEE", "warehouse": self.warehouse}, "name")
		frappe.db.set_value("Bin", name, {"actual_qty": actual, "reserved_qty": reserved})

	def test_available_is_actual_minus_reserved(self):
		self.set_bin(100, 30)
		self.assertEqual(inv.available_quantity("INV-TEE", self.warehouse), 70)

	def test_reserved_quantity_is_not_optional(self):
		"""The load-bearing case: a web order has raised reserved_qty but no stock has moved
		yet. Ignoring reserved_qty here re-exposes units that are already sold."""
		self.set_bin(100, 0)
		before = inv.available_quantity("INV-TEE", self.warehouse)

		self.set_bin(100, 10)
		after = inv.available_quantity("INV-TEE", self.warehouse)

		self.assertEqual(before, 100)
		self.assertEqual(after, 90, "a reservation must reduce what Shopify may sell")

	def test_fractional_quantities_floor_the_difference_not_each_term(self):
		"""10.2 on hand, 2.9 reserved is 7.3 truly available.

		Truncating each term separately gives 10 - 2 = 8, over-reporting by a unit, and
		over-reporting is the direction that oversells."""
		self.set_bin(10.2, 2.9)
		self.assertEqual(inv.available_quantity("INV-TEE", self.warehouse), 7)

	def test_negative_availability_is_reported_not_clamped(self):
		"""Clamping to zero would make drift detection see a permanent disagreement it would
		try to correct forever."""
		self.set_bin(5, 12)
		self.assertEqual(inv.available_quantity("INV-TEE", self.warehouse), -7)

	def test_unknown_item_or_warehouse_is_zero(self):
		self.assertEqual(inv.available_quantity("NO-SUCH-ITEM", self.warehouse), 0)

	def test_location_total_sums_the_warehouses_feeding_it(self):
		self.set_bin(40, 0)
		self.assertEqual(inv.available_for_location(self.store_doc, "INV-TEE", LOCATION), 40)

	def test_unmapped_location_contributes_nothing(self):
		self.assertEqual(
			inv.available_for_location(self.store_doc, "INV-TEE", "gid://shopify/Location/999"), 0
		)


class TestTriggers(InventoryTestCase):
	"""What causes a push to be queued (spec §10.2)."""

	def test_stock_movement_queues_a_push(self):
		with patch.object(engine, "schedule_drain"):
			queued = inv.enqueue_for_item("INV-TEE", self.warehouse, "Stock Ledger Entry", "SLE-1")

		self.assertEqual(queued, 1)
		rows = frappe.get_all("Shopify Sync Queue", filters={"operation": "inventory"}, fields=["dedupe_key"])
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].dedupe_key, f"inventory:{self.store}:INV-TEE:{LOCATION}")

	def test_repeated_movements_coalesce_into_one_row(self):
		"""The acceptance criterion: a 20-line invoice is one row per item, not twenty calls."""
		with patch.object(engine, "schedule_drain"):
			for i in range(20):
				inv.enqueue_for_item("INV-TEE", self.warehouse, "Stock Ledger Entry", f"SLE-{i}")

		self.assertEqual(frappe.db.count("Shopify Sync Queue", {"operation": "inventory"}), 1)

	def test_unmapped_item_costs_nothing(self):
		"""Most movements in a large catalogue touch items Shopify never heard of."""
		with patch.object(engine, "schedule_drain"):
			queued = inv.enqueue_for_item("NOT-ON-SHOPIFY-AT-ALL", self.warehouse, "SLE", "x")

		self.assertEqual(queued, 0)
		self.assertEqual(frappe.db.count("Shopify Sync Queue"), 0)

	def test_unmapped_warehouse_does_not_queue(self):
		if not self.other_warehouse:
			self.skipTest("site has only one warehouse")
		with patch.object(engine, "schedule_drain"):
			queued = inv.enqueue_for_item("INV-TEE", self.other_warehouse, "SLE", "x")
		self.assertEqual(queued, 0)

	def test_sync_inventory_off_does_not_queue(self):
		frappe.db.set_value("Shopify Store", self.store, "sync_inventory", 0)
		frappe.clear_cache(doctype="Shopify Store")
		try:
			with patch.object(engine, "schedule_drain"):
				queued = inv.enqueue_for_item("INV-TEE", self.warehouse, "SLE", "x")
			self.assertEqual(queued, 0)
		finally:
			frappe.db.set_value("Shopify Store", self.store, "sync_inventory", 1)
			frappe.clear_cache(doctype="Shopify Store")

	def test_sales_order_submission_queues_a_push(self):
		"""reserved_qty moved, and no stock ledger entry reflects that."""
		doc = frappe._dict(
			doctype="Sales Order",
			name="SO-TEST",
			items=[frappe._dict(item_code="INV-TEE", warehouse=self.warehouse)],
			set_warehouse=self.warehouse,
			get=lambda key, default=None: (
				[frappe._dict(item_code="INV-TEE", warehouse=self.warehouse)] if key == "items" else default
			),
		)
		with patch.object(engine, "schedule_drain"):
			inv.on_reservation_change(doc)

		self.assertEqual(frappe.db.count("Shopify Sync Queue", {"operation": "inventory"}), 1)


class TestDriftDetection(InventoryTestCase):
	"""inventory_levels/update is for detecting drift only (spec §10.4)."""

	def payload(self, available):
		link = frappe.db.get_value(
			"Shopify Item Link", {"store": self.store, "item_code": "INV-TEE"}, "inventory_item_gid"
		)
		return {
			"inventory_item_id": link.split("/")[-1],
			"location_id": LOCATION.split("/")[-1],
			"available": available,
		}

	def set_bin(self, actual, reserved=0):
		name = frappe.db.get_value("Bin", {"item_code": "INV-TEE", "warehouse": self.warehouse}, "name")
		frappe.db.set_value("Bin", name, {"actual_qty": actual, "reserved_qty": reserved})

	def test_matching_levels_report_no_drift(self):
		self.set_bin(50)
		result = drift.detect_drift(self.store, self.payload(50))
		self.assertFalse(result["drift"])

	def test_mismatch_queues_a_correction_towards_shopify(self):
		self.set_bin(50)
		with patch.object(engine, "schedule_drain"):
			result = drift.detect_drift(self.store, self.payload(42))

		self.assertTrue(result["drift"])
		self.assertEqual(result["erpnext"], 50)
		self.assertEqual(result["shopify"], 42)
		self.assertEqual(frappe.db.count("Shopify Sync Queue", {"operation": "inventory"}), 1)

	def test_erpnext_stock_is_never_written(self):
		"""Writing ERPNext stock from Shopify creates a loop with no stable fixed point."""
		self.set_bin(50)
		with patch.object(engine, "schedule_drain"):
			drift.detect_drift(self.store, self.payload(42))

		self.assertEqual(inv.available_quantity("INV-TEE", self.warehouse), 50)

	def test_drift_is_not_counted_while_our_own_push_is_pending(self):
		"""The notification is usually the echo of our own last write. Counting it would
		inflate the metric that tells operators when something is genuinely wrong."""
		self.set_bin(50)
		with patch.object(engine, "schedule_drain"):
			engine.enqueue_sync(self.store, "inventory", f"inventory:{self.store}:INV-TEE:{LOCATION}")
			result = drift.detect_drift(self.store, self.payload(42))

		self.assertFalse(result["drift"])
		self.assertIn("already pending", result["reason"])

	def test_unmapped_inventory_item_is_skipped(self):
		result = drift.detect_drift(
			self.store, {"inventory_item_id": "99999999", "location_id": "555", "available": 1}
		)
		self.assertIn("skipped", result)

	def test_numeric_ids_are_converted_to_gids(self):
		"""Webhook payloads carry legacy numeric ids; the rest of the app speaks GIDs."""
		self.assertEqual(drift._gid("12345", "InventoryItem"), "gid://shopify/InventoryItem/12345")
		self.assertEqual(drift._gid("gid://shopify/Location/9", "Location"), "gid://shopify/Location/9")
		self.assertIsNone(drift._gid(None, "Location"))


class TestHooksFireEndToEnd(InventoryTestCase):
	"""Proof the doc_events are actually wired, not just that the functions work.

	Calling enqueue_for_item() directly tests the logic but would keep passing if hooks.py
	stopped registering the hook at all -- which is the failure that would silently disable
	the whole feature in production.
	"""

	def test_a_real_stock_movement_queues_a_push(self):
		"""The acceptance criterion: a stock movement reaches the queue by itself."""
		with patch.object(engine, "schedule_drain"):
			entry = frappe.new_doc("Stock Entry")
			entry.stock_entry_type = "Material Receipt"
			entry.purpose = "Material Receipt"
			entry.company = self.store_doc.company
			entry.append(
				"items",
				{"item_code": "INV-TEE", "qty": 5, "t_warehouse": self.warehouse, "basic_rate": 10},
			)
			entry.insert(ignore_permissions=True)
			entry.submit()

		rows = frappe.get_all(
			"Shopify Sync Queue",
			filters={"operation": "inventory"},
			fields=["dedupe_key", "ref_doctype"],
		)
		self.assertEqual(len(rows), 1, "submitting a Stock Entry must queue exactly one push")
		self.assertEqual(rows[0].dedupe_key, f"inventory:{self.store}:INV-TEE:{LOCATION}")
		self.assertEqual(rows[0].ref_doctype, "Stock Ledger Entry")

	def test_the_stock_movement_hook_is_registered(self):
		"""hooks.py must point Stock Ledger Entry at the handler. ERPNext creates SLEs with
		sle.submit(), so this fires -- unlike a Bin hook, which never would."""
		from shopify_integration import hooks

		self.assertEqual(
			hooks.doc_events["Stock Ledger Entry"]["on_submit"],
			"shopify_integration.outbound.inventory.on_stock_movement",
		)

	def test_bin_is_not_hooked(self):
		"""ERPNext v15 writes Bin with db_update(), which fires no doc events. A Bin hook
		would silently never run, and the feature would appear broken for no visible reason."""
		from shopify_integration import hooks

		self.assertNotIn("Bin", hooks.doc_events)


class TestCompareAndSetConflictIsRetryable(InventoryTestCase):
	"""Losing an optimistic-concurrency race is not a failure, it is a reason to try again.

	Every other `userErrors[]` refusal is permanent, and treating this one the same way meant a
	row died on its first conflict with `attempts=0`. Three workers draining the same items
	turned twelve rows into dead work needing a manual requeue, and only the nightly
	reconciliation put Shopify right.
	"""

	def test_the_conflict_exception_asks_the_queue_to_retry(self):
		from shopify_integration.exceptions import ShopifyInventoryConflict, ShopifyUserError

		self.assertTrue(ShopifyInventoryConflict("moved").retryable)
		self.assertFalse(
			ShopifyUserError("refused").retryable,
			"every other user error stays permanent -- retrying an unchanged write cannot help",
		)

	def test_it_is_still_a_user_error(self):
		"""So anything catching ShopifyUserError keeps catching it."""
		from shopify_integration.exceptions import ShopifyInventoryConflict, ShopifyUserError

		self.assertIsInstance(ShopifyInventoryConflict("moved"), ShopifyUserError)

	def test_a_persistent_mismatch_is_raised_as_a_conflict(self):
		from shopify_integration.exceptions import ShopifyInventoryConflict, ShopifyUserError
		from shopify_integration.outbound import inventory as inventory_module

		mismatch = ShopifyUserError(
			"inventorySetQuantities rejected the write: "
			"input.quantities.0.compareQuantity: The compareQuantity argument no longer matches"
		)

		class AlwaysConflicts:
			def execute(self, query, variables, cost_hint=0):
				if "inventoryLevels" in str(query) or "nodes" in str(variables):
					return {"nodes": []}
				raise mismatch

		batch = [
			{
				"link": "LINK-1",
				"item_code": "TEE",
				"inventory_item_gid": "gid://shopify/InventoryItem/1",
				"location_gid": "gid://shopify/Location/1",
				"quantity": 5,
			}
		]

		with self.assertRaises(ShopifyInventoryConflict):
			inventory_module._push_batch(AlwaysConflicts(), self.store_doc, batch, allow_retry=True)

	def test_an_unrelated_user_error_is_still_permanent(self):
		from shopify_integration.exceptions import ShopifyInventoryConflict, ShopifyUserError
		from shopify_integration.outbound import inventory as inventory_module

		class AlwaysRefuses:
			def execute(self, query, variables, cost_hint=0):
				if "inventoryLevels" in str(query) or "nodes" in str(variables):
					return {"nodes": []}
				raise ShopifyUserError("Inventory item does not exist")

		batch = [
			{
				"link": "LINK-1",
				"item_code": "TEE",
				"inventory_item_gid": "gid://shopify/InventoryItem/1",
				"location_gid": "gid://shopify/Location/1",
				"quantity": 5,
			}
		]

		with self.assertRaises(ShopifyUserError) as caught:
			inventory_module._push_batch(AlwaysRefuses(), self.store_doc, batch, allow_retry=True)

		self.assertNotIsInstance(caught.exception, ShopifyInventoryConflict)
