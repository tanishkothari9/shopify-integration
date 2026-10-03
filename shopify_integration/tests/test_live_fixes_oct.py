"""Six faults found on the live store on 1 October, around STOITEM202605498/499."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

LOCATION = "gid://shopify/Location/5551"


def gids(sku: str) -> tuple[str, str, str]:
	"""Shopify ids for one item. Derived from the SKU, because they must differ per item:
	`upsert_link` matches on variant_gid first, so a fake that reuses one id makes every test
	adopt the previous test's link -- watermarks and all."""
	tail = sku.rsplit("-", 1)[-1]
	return (
		f"gid://shopify/Product/{tail}",
		f"gid://shopify/ProductVariant/{tail}",
		f"gid://shopify/InventoryItem/{tail}",
	)


class _CreateClient:
	"""Shopify, as far as creating one simple product is concerned.

	Every call returns the same dict; each caller reads only its own key out of it, so one
	canned reply serves productCreate, the variant update and the re-read alike.
	"""

	def __init__(self, sku: str):
		self.sku = sku
		self.product, self.variant, self.inventory_item = gids(sku)
		self.calls: list[dict] = []

	def execute(self, query, variables=None, cost_hint=0):
		self.calls.append(variables or {})
		variant = {"id": self.variant, "sku": self.sku, "inventoryItem": {"id": self.inventory_item}}
		return {
			"productCreate": {
				"product": {"id": self.product, "variants": {"nodes": [variant]}},
				"userErrors": [],
			},
			"productVariantsBulkUpdate": {"productVariants": [variant], "userErrors": []},
			"product": {"id": self.product, "status": "DRAFT", "variants": {"edges": [{"node": variant}]}},
			"publications": {"nodes": []},
		}


class _InventoryClient:
	"""Records the quantities asked of inventorySetQuantities."""

	def __init__(self):
		self.quantities: list[dict] = []

	def execute(self, query, variables=None, cost_hint=0):
		sent = (variables or {}).get("input", {}).get("quantities")
		if sent:
			self.quantities.extend(sent)
			return {
				"inventorySetQuantities": {"inventoryAdjustmentGroup": {"id": "gid://x/1"}, "userErrors": []}
			}
		return {"inventoryItems": {"nodes": []}}


class TestANewProductSendsWhatErpnextAlreadyHas(FrappeTestCase):
	"""Stock and price that existed before the product did.

	Opening Stock is the case that shows it: the Stock Ledger Entry is submitted in the same
	save as the Item, long before any Shopify product exists, so the stock hook finds no link
	and queues nothing. Unless creating the product sends what ERPNext already holds, the
	listing goes live at zero -- out of stock on the day it appears -- and stays there until
	someone sells one or the nightly reconciliation comes round.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from shopify_integration.tests.test_integration import SECRET_A, make_store

		cls.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)
		store_doc = frappe.get_doc("Shopify Store", cls.store)
		cls.company = store_doc.company
		cls.warehouse = frappe.db.get_value("Warehouse", {"company": cls.company, "is_group": 0}, "name")
		store_doc.default_warehouse = cls.warehouse
		store_doc.sync_inventory = 1
		store_doc.sync_prices = 1
		store_doc.set("location_map", [])
		store_doc.append("location_map", {"warehouse": cls.warehouse, "location_gid": LOCATION})
		store_doc.flags.ignore_mandatory = True
		store_doc.save(ignore_permissions=True)
		frappe.db.commit()

	def setUp(self):
		frappe.db.delete("Shopify Sync Queue", {"store": self.store})
		frappe.db.commit()

	def _item_with_opening_stock(self, code: str, qty: float):
		from shopify_integration.tests.test_integration import with_hsn

		for doctype, name in (("Shopify Item Link", {"item_code": code}), ("Item", code)):
			if isinstance(name, dict):
				for existing in frappe.get_all(doctype, filters=name, pluck="name"):
					frappe.delete_doc(doctype, existing, force=True, ignore_permissions=True)
			elif frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)

		item = frappe.new_doc("Item")
		item.item_code = code
		item.item_name = code
		item.item_group = frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0]
		item.stock_uom = "Nos"
		item.is_stock_item = 1
		item.opening_stock = qty
		item.valuation_rate = 100
		item.append("item_defaults", {"company": self.company, "default_warehouse": self.warehouse})
		with_hsn(item)
		item.insert(ignore_permissions=True)
		frappe.db.commit()
		# These helpers commit, so the usual per-test rollback cannot undo them. A stale link
		# is not inert either: upsert_link matches on it.
		self.addCleanup(frappe.db.commit)
		self.addCleanup(self._forget, code)
		return item

	def _forget(self, item_code):
		for link in frappe.get_all("Shopify Item Link", filters={"item_code": item_code}, pluck="name"):
			frappe.delete_doc("Shopify Item Link", link, force=True, ignore_permissions=True)
		frappe.db.delete("Shopify Sync Queue", {"ref_docname": item_code})

	def test_opening_stock_reaches_shopify(self):
		"""The whole path: seven on the shelf before publishing, seven in the mutation."""
		from shopify_integration.api.client import ShopifyClient
		from shopify_integration.outbound.inventory import push_inventory
		from shopify_integration.outbound.product import _create_product_unlocked
		from shopify_integration.sync import engine

		item = self._item_with_opening_stock(f"ZZ-OPENING-{frappe.generate_hash(length=6)}", 7)
		self.assertEqual(
			frappe.db.get_value("Bin", {"item_code": item.name, "warehouse": self.warehouse}, "actual_qty"),
			7,
			"the fixture itself must put seven on the shelf, or this proves nothing",
		)

		with patch.object(engine, "schedule_drain"), patch("shopify_integration.sync.engine.schedule_drain"):
			product_gid = _create_product_unlocked(_CreateClient(item.name), self.store, item.name)
		frappe.db.commit()

		self.assertEqual(product_gid, gids(item.name)[0])
		rows = frappe.get_all(
			"Shopify Sync Queue",
			filters={"store": self.store, "operation": "inventory"},
			fields=["dedupe_key", "payload"],
		)
		self.assertTrue(rows, "publishing an item that already has stock must queue that stock")

		client = _InventoryClient()
		with patch.object(ShopifyClient, "for_store", return_value=client):
			push_inventory(self.store, rows)

		self.assertEqual(
			[q["quantity"] for q in client.quantities],
			[7],
			"Shopify was told how many are actually on the shelf",
		)
		self.assertEqual(client.quantities[0]["inventoryItemId"], gids(item.name)[2])
		self.assertEqual(client.quantities[0]["locationId"], LOCATION)

	def test_the_price_is_sent_too(self):
		"""A price typed before the item was ever published is not in what productCreate was
		given -- the variant carried whatever the price list held when the row was built."""
		from shopify_integration.outbound.product import _create_product_unlocked
		from shopify_integration.sync import engine

		item = self._item_with_opening_stock(f"ZZ-OPENPRICE-{frappe.generate_hash(length=6)}", 3)
		# The rate typed on the Item form, which is where most merchants put it.
		frappe.db.set_value("Item", item.name, "standard_rate", 1200)
		frappe.db.delete("Shopify Sync Queue", {"store": self.store})
		frappe.db.commit()
		with patch.object(engine, "schedule_drain"), patch("shopify_integration.sync.engine.schedule_drain"):
			_create_product_unlocked(_CreateClient(item.name), self.store, item.name)
		frappe.db.commit()

		queued = frappe.get_all(
			"Shopify Sync Queue",
			filters={"store": self.store, "operation": "price", "ref_docname": item.name},
			pluck="name",
		)
		self.assertTrue(queued, "the price ERPNext holds must be sent after the product exists")


class TestADrainDoesNotStrandWork(FrappeTestCase):
	"""A row written while a drain was already running had its signal deduplicated away.

	STOITEM202605499's inventory row sat Pending from 21:47:45 until the safety net at 22:00.
	Correct in the end, thirteen minutes late.
	"""

	def test_a_finished_drain_schedules_another_while_work_remains(self):
		from shopify_integration.sync import engine

		with (
			patch.object(engine, "_drain_locked", return_value={"claimed": 1, "done": 1}),
			patch.object(engine, "has_pending", return_value=True),
			patch.object(engine, "schedule_drain") as scheduled,
		):
			engine.drain_store("ZZ Store")

		# follow_on, because the drain making the request holds the plain job id and Frappe
		# refuses to queue a duplicate of a STARTED job -- which silently dropped every
		# self-reschedule until it was fixed.
		scheduled.assert_called_once_with("ZZ Store", follow_on=True)

	def test_an_empty_queue_schedules_nothing(self):
		from shopify_integration.sync import engine

		with (
			patch.object(engine, "_drain_locked", return_value={"claimed": 0, "done": 0}),
			patch.object(engine, "has_pending", return_value=False),
			patch.object(engine, "schedule_drain") as scheduled,
		):
			engine.drain_store("ZZ Store")

		self.assertFalse(scheduled.called, "an idle store must not spin")

	def test_the_safety_net_runs_every_minute(self):
		"""A drain reschedules itself now, so this is a true net rather than the main path --
		and the longest a dropped signal can strand a push is how long a customer waits to
		see stock they could have bought."""
		cron = frappe.get_hooks("scheduler_events").get("cron") or {}
		every_minute = cron.get("* * * * *") or []
		self.assertIn("shopify_integration.sync.engine.drain_all_stores", every_minute)


class TestTheQueuePayloadIsJson(FrappeTestCase):
	"""push_collections read the payload as a dict. It is the JSON string as_json wrote, so
	every collection row died on `'str' object has no attribute 'get'` -- the operation had
	never once succeeded."""

	def test_a_json_payload_is_parsed(self):
		from shopify_integration.outbound.collections import _item_from

		self.assertEqual(_item_from({"payload": frappe.as_json({"item_code": "KURTI-S"})}), "KURTI-S")

	def test_a_dict_payload_still_works(self):
		from shopify_integration.outbound.collections import _item_from

		self.assertEqual(_item_from({"payload": {"item_code": "KURTI-S"}}), "KURTI-S")

	def test_it_falls_back_to_the_reference(self):
		from shopify_integration.outbound.collections import _item_from

		self.assertEqual(_item_from({"payload": None, "ref_docname": "KURTI-M"}), "KURTI-M")

	def test_rubbish_in_the_payload_does_not_raise(self):
		from shopify_integration.outbound.collections import _item_from

		self.assertEqual(_item_from({"payload": "not json", "ref_docname": "KURTI-L"}), "KURTI-L")
		self.assertIsNone(_item_from({"payload": "[]"}))

	def test_a_real_queued_row_reaches_the_handler(self):
		"""Through the queue, not past it: the shape the handler actually receives is the
		thing that was wrong."""
		from shopify_integration.outbound import collections as module
		from shopify_integration.sync.engine import enqueue_sync

		store = frappe.get_all("Shopify Store", limit=1, pluck="name")[0]
		frappe.db.delete("Shopify Sync Queue", {"store": store, "operation": "collection"})
		frappe.db.commit()

		with (
			patch.object(module, "schedule_drain", create=True),
			patch("shopify_integration.sync.engine.schedule_drain"),
		):
			enqueue_sync(
				store,
				"collection",
				dedupe_key=f"collection:{store}:ZZ-ROW",
				ref_doctype="Item",
				ref_docname="ZZ-ROW",
				payload={"item_code": "ZZ-ROW"},
			)
		frappe.db.commit()

		rows = frappe.get_all(
			"Shopify Sync Queue",
			filters={"store": store, "operation": "collection"},
			fields=["name", "payload", "ref_docname"],
		)
		self.addCleanup(frappe.db.delete, "Shopify Sync Queue", {"store": store, "operation": "collection"})
		self.assertTrue(rows)
		self.assertEqual(module._item_from(rows[0]), "ZZ-ROW")


class TestTheCreateUpdateRace(FrappeTestCase):
	"""Publishing makes Shopify fire products/create and products/update straight back, and
	they land while the outbound job is still saving the same Item. Two Event Logs in Error
	inside forty-three seconds on the live store."""

	def test_a_timestamp_mismatch_is_retried_not_logged_as_an_error(self):
		from shopify_integration.inbound import product as module

		attempts = []

		def flaky(store, product):
			attempts.append(1)
			if len(attempts) < 3:
				raise frappe.TimestampMismatchError("Document has been modified")
			return {"item": "ZZ-ITEM"}

		with (
			patch.object(module, "write_product_mapping", side_effect=flaky),
			patch.object(module.frappe.db, "rollback"),
		):
			result = module._write_with_retry("ZZ Store", {"id": "gid://shopify/Product/1"})

		self.assertEqual(result, {"item": "ZZ-ITEM"})
		self.assertEqual(len(attempts), 3, "it should have re-read and tried again")

	def test_it_gives_up_eventually(self):
		"""A mismatch that survives every attempt is something else wearing this error's
		clothes, and must not be swallowed."""
		from shopify_integration.inbound import product as module

		with (
			patch.object(module, "write_product_mapping", side_effect=frappe.TimestampMismatchError("nope")),
			patch.object(module.frappe.db, "rollback"),
			self.assertRaises(frappe.TimestampMismatchError),
		):
			module._write_with_retry("ZZ Store", {"id": "gid://shopify/Product/1"})

	def test_an_unrelated_failure_is_not_retried(self):
		from shopify_integration.inbound import product as module

		attempts = []

		def broken(store, product):
			attempts.append(1)
			raise ValueError("something else")

		with (
			patch.object(module, "write_product_mapping", side_effect=broken),
			self.assertRaises(ValueError),
		):
			module._write_with_retry("ZZ Store", {"id": "gid://shopify/Product/1"})

		self.assertEqual(len(attempts), 1)


class TestLosingTheDrainLockIsQuiet(FrappeTestCase):
	"""Frappe's filelock writes an Error Log before it raises, so catching the exception did
	not stop the noise. A second worker finding a store already draining is the design
	working, and on a busy shop it filled the log hundreds of times a day."""

	def test_it_does_not_write_an_error_log(self):
		from frappe.utils.file_lock import LockTimeoutError

		from shopify_integration.sync.engine import quiet_filelock

		name = f"zz_quiet_probe_{frappe.generate_hash(length=6)}"
		with quiet_filelock(name, timeout=1):
			with patch.object(frappe, "log_error") as logged:
				with self.assertRaises(LockTimeoutError):
					with quiet_filelock(name, timeout=1):
						pass
			self.assertFalse(logged.called, "losing the race is not an error worth logging")

	def test_it_still_serialises(self):
		from frappe.utils.file_lock import LockTimeoutError

		from shopify_integration.sync.engine import quiet_filelock

		name = f"zz_quiet_probe_{frappe.generate_hash(length=6)}"
		with quiet_filelock(name, timeout=1):
			with self.assertRaises(LockTimeoutError):
				with quiet_filelock(name, timeout=1):
					pass
