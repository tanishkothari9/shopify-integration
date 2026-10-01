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

		scheduled.assert_called_once_with("ZZ Store")

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


class _MediaClient:
	"""A product's media list, and what the app does to it."""

	def __init__(self, media=None, refuse=False):
		self.media = list(media or [])
		self.refuse = refuse
		self.created: list[dict] = []
		self.deleted: list[str] = []
		self._next = 100

	def execute(self, query, variables=None, cost_hint=0):
		variables = variables or {}
		if "media" in variables and "productId" in variables:
			self.created.append(variables)
			if self.refuse:
				return {
					"productCreateMedia": {
						"media": [],
						"mediaUserErrors": [{"message": "Invalid image source"}],
					}
				}
			self._next += 1
			made = f"gid://shopify/MediaImage/{self._next}"
			self.media.append(made)
			return {"productCreateMedia": {"media": [{"id": made}], "mediaUserErrors": []}}
		if "mediaIds" in variables:
			self.deleted.extend(variables["mediaIds"])
			self.media = [m for m in self.media if m not in variables["mediaIds"]]
			return {"productDeleteMedia": {"deletedMediaIds": variables["mediaIds"], "mediaUserErrors": []}}
		return {"product": {"media": {"nodes": [{"id": m} for m in self.media]}}}


class TestAChangedImageReachesShopify(FrappeTestCase):
	"""The old rule was "attach only if the product has no media at all", to avoid trampling
	the merchant's photography. Right instinct, wrong rule: after the first push the product
	always has media -- its own -- so a corrected image in ERPNext never reached Shopify
	again. Now the app records what it put there and replaces only that.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from shopify_integration.tests.test_integration import SECRET_A, make_store

		cls.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)
		frappe.db.set_value("Shopify Store", cls.store, "sync_item_images", 1)
		frappe.db.commit()

	def _item_and_link(self, code, image, media=None, synced=None):
		from shopify_integration.tests.test_integration import with_hsn

		for existing in frappe.get_all("Shopify Item Link", filters={"item_code": code}, pluck="name"):
			frappe.delete_doc("Shopify Item Link", existing, force=True, ignore_permissions=True)
		if not frappe.db.exists("Item", code):
			item = frappe.new_doc("Item")
			item.item_code = code
			item.item_name = code
			item.item_group = frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0]
			item.stock_uom = "Nos"
			with_hsn(item)
			item.insert(ignore_permissions=True)
		item = frappe.get_doc("Item", code)
		item.db_set("image", image, update_modified=False)

		link = frappe.new_doc("Shopify Item Link")
		link.store = self.store
		link.item_code = code
		link.sku = code
		link.product_gid = f"gid://shopify/Product/img-{code}"
		link.variant_gid = f"gid://shopify/ProductVariant/img-{code}"
		link.app_media_gids = "\n".join(media or [])
		link.image_synced_url = synced
		link.insert(ignore_permissions=True)
		frappe.db.commit()
		self.addCleanup(frappe.db.commit)
		self.addCleanup(
			frappe.delete_doc, "Shopify Item Link", link.name, force=True, ignore_permissions=True
		)
		return frappe.get_doc("Item", code), link

	def test_a_new_image_replaces_the_one_the_app_put_there(self):
		"""The regression itself."""
		from shopify_integration.outbound.product import _sync_image

		old = "gid://shopify/MediaImage/1"
		item, link = self._item_and_link(
			"ZZ-IMG-A",
			"https://cdn.example.com/new.jpg",
			media=[old],
			synced="https://cdn.example.com/old.jpg",
		)
		client = _MediaClient([old])

		_sync_image(client, link.product_gid, item, self.store)

		self.assertEqual(len(client.created), 1, "the new image must be sent")
		self.assertEqual(client.deleted, [old], "and the one it replaces taken down")
		after = frappe.db.get_value(
			"Shopify Item Link", link.name, ["app_media_gids", "image_synced_url"], as_dict=True
		)
		self.assertEqual(after.image_synced_url, "https://cdn.example.com/new.jpg")
		self.assertNotIn(old, after.app_media_gids.splitlines())

	def test_the_merchants_own_photography_is_never_touched(self):
		"""Media the app did not record is the merchant's, in the order they arranged it."""
		from shopify_integration.outbound.product import _sync_image

		ours, theirs = "gid://shopify/MediaImage/1", "gid://shopify/MediaImage/77"
		item, link = self._item_and_link(
			"ZZ-IMG-B",
			"https://cdn.example.com/new.jpg",
			media=[ours],
			synced="https://cdn.example.com/old.jpg",
		)
		client = _MediaClient([ours, theirs])

		_sync_image(client, link.product_gid, item, self.store)

		self.assertEqual(client.deleted, [ours])
		self.assertIn(theirs, client.media, "the merchant's photo must survive")

	def test_media_from_before_the_app_recorded_anything_is_left_alone(self):
		"""An existing shop's products have media this app never put there. Deleting it
		because the link happens to be blank would destroy real photography."""
		from shopify_integration.outbound.product import _sync_image

		item, link = self._item_and_link("ZZ-IMG-C", "https://cdn.example.com/new.jpg")
		client = _MediaClient(["gid://shopify/MediaImage/900"])

		_sync_image(client, link.product_gid, item, self.store)

		self.assertEqual(client.created, [], "nothing should be added over the merchant's")
		self.assertEqual(client.deleted, [])
		self.assertEqual(
			frappe.db.get_value("Shopify Item Link", link.name, "image_synced_url"),
			"https://cdn.example.com/new.jpg",
			"but it must be recorded, so this does not cost a call on every save",
		)

	def test_an_unchanged_image_costs_nothing(self):
		"""Every product save would otherwise ask Shopify for its media list."""
		from shopify_integration.outbound.product import _sync_image

		url = "https://cdn.example.com/same.jpg"
		item, link = self._item_and_link("ZZ-IMG-D", url, media=["gid://shopify/MediaImage/1"], synced=url)
		client = _MediaClient(["gid://shopify/MediaImage/1"])

		_sync_image(client, link.product_gid, item, self.store)

		self.assertEqual(client.created, [])
		self.assertEqual(client.deleted, [])

	def test_a_refused_image_leaves_the_old_one_up(self):
		"""Deleting first and failing to upload would leave the product with no photo at all."""
		from shopify_integration.outbound.product import _sync_image

		old = "gid://shopify/MediaImage/1"
		item, link = self._item_and_link(
			"ZZ-IMG-E",
			"https://cdn.example.com/bad.jpg",
			media=[old],
			synced="https://cdn.example.com/old.jpg",
		)
		client = _MediaClient([old], refuse=True)

		_sync_image(client, link.product_gid, item, self.store)

		self.assertEqual(client.deleted, [], "the old image must stay until a new one exists")
		self.assertEqual(
			frappe.db.get_value("Shopify Item Link", link.name, "image_synced_url"),
			"https://cdn.example.com/old.jpg",
			"and the failure must not be recorded as a success, or it is never retried",
		)

	def test_a_store_with_image_sync_off_is_not_touched(self):
		from shopify_integration.outbound.product import _sync_image

		item, link = self._item_and_link("ZZ-IMG-F", "https://cdn.example.com/new.jpg")
		frappe.db.set_value("Shopify Store", self.store, "sync_item_images", 0)
		self.addCleanup(frappe.db.set_value, "Shopify Store", self.store, "sync_item_images", 1)
		frappe.clear_document_cache("Shopify Store", self.store)
		client = _MediaClient([])

		_sync_image(client, link.product_gid, item, self.store)

		self.assertEqual(client.created, [])


class TestTheWebhookRacingTheOutboundCreate(FrappeTestCase):
	"""Publishing a product makes Shopify fire products/create straight back, and it arrives
	while the job that caused it is still running.

	On 1 October that webhook wrote STOITEM202605498's link at 21:47:02.96, 130ms before the
	publishing job finished. The webhook writes links through the catalogue mapping, which
	never queued stock; the job then found a link where it expected none and took the update
	branch, which queues neither stock nor price. Nothing was left to correct it: the item
	had no inventory row in the queue at any point, and its link's inventory_synced_on stayed
	NULL. It went live at zero.
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

	def _item(self, qty):
		from shopify_integration.tests.test_integration import with_hsn

		code = f"ZZ-RACE-{frappe.generate_hash(length=6)}"
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

	def _webhook_product(self, sku):
		"""What Shopify echoes back for the product just created."""
		product, variant, inventory_item = gids(sku)
		return {
			"id": product,
			"title": sku,
			"status": "ACTIVE",
			"options": [],
			"variants": [
				{"id": variant, "sku": sku, "price": "0.00", "inventoryItem": {"id": inventory_item}}
			],
		}

	def test_the_webhook_winning_the_link_leaves_nothing_to_send_the_stock(self):
		"""The live shape, in order.

		The webhook writes the link through the catalogue mapping. The outbound product row
		drains afterwards, finds a link where it expected none, and takes the update branch
		-- which sends a title and a status and nothing else. Neither half queued the stock,
		and STOITEM202605498 went live at zero.
		"""
		from shopify_integration.api.client import ShopifyClient
		from shopify_integration.catalogue.mapping import write_product_mapping
		from shopify_integration.outbound.product import push_products
		from shopify_integration.sync import engine

		item = self._item(7)

		with patch.object(engine, "schedule_drain"), patch("shopify_integration.sync.engine.schedule_drain"):
			write_product_mapping(self.store, self._webhook_product(item.name))
			frappe.db.commit()

			client = _CreateClient(item.name)
			with patch.object(ShopifyClient, "for_store", return_value=client):
				push_products(self.store, [{"ref_docname": item.name}])
		frappe.db.commit()

		links = frappe.get_all("Shopify Item Link", filters={"store": self.store, "item_code": item.name})
		self.assertEqual(len(links), 1, "the two paths must converge on one link, not two")

		queued = frappe.get_all(
			"Shopify Sync Queue",
			filters={"store": self.store, "operation": "inventory"},
			pluck="dedupe_key",
		)
		self.assertTrue(
			[k for k in queued if item.name in k],
			"whichever path wrote the link first had to queue the stock -- this is the row "
			"STOITEM202605498 never had, at any point",
		)

	def test_a_webhook_landing_mid_publish_still_leaves_stock_queued(self):
		"""The other order: the echo arrives while the create is still running, so the job
		adopts a link it did not write."""
		from shopify_integration.catalogue.mapping import write_product_mapping
		from shopify_integration.outbound.product import _create_product_unlocked
		from shopify_integration.sync import engine

		item = self._item(7)
		landed = []

		class _RacingClient(_CreateClient):
			def execute(inner, query, variables=None, cost_hint=0):
				result = super().execute(query, variables, cost_hint)
				if not landed:
					landed.append(write_product_mapping(self.store, self._webhook_product(item.name)))
				return result

		with patch.object(engine, "schedule_drain"), patch("shopify_integration.sync.engine.schedule_drain"):
			_create_product_unlocked(_RacingClient(item.name), self.store, item.name)
		frappe.db.commit()

		self.assertTrue(landed, "the webhook has to have landed, or this tests nothing")
		self.assertEqual(
			len(frappe.get_all("Shopify Item Link", filters={"store": self.store, "item_code": item.name})),
			1,
		)
		queued = frappe.get_all(
			"Shopify Sync Queue", filters={"store": self.store, "operation": "inventory"}, pluck="dedupe_key"
		)
		self.assertTrue([k for k in queued if item.name in k])

	def test_the_queued_row_drains_to_shopify(self):
		"""And the row is worth having: it carries the real figure and marks the link synced."""
		from shopify_integration.api.client import ShopifyClient
		from shopify_integration.catalogue.mapping import write_product_mapping
		from shopify_integration.outbound.inventory import push_inventory
		from shopify_integration.sync import engine

		item = self._item(7)
		with patch.object(engine, "schedule_drain"), patch("shopify_integration.sync.engine.schedule_drain"):
			write_product_mapping(self.store, self._webhook_product(item.name))
		frappe.db.commit()

		rows = frappe.get_all(
			"Shopify Sync Queue",
			filters={"store": self.store, "operation": "inventory"},
			fields=["name", "dedupe_key", "payload"],
		)
		self.assertTrue(rows)

		client = _InventoryClient()
		with patch.object(ShopifyClient, "for_store", return_value=client):
			push_inventory(self.store, rows)
		frappe.db.commit()

		self.assertEqual([q["quantity"] for q in client.quantities], [7])
		self.assertIsNotNone(
			frappe.db.get_value(
				"Shopify Item Link", {"store": self.store, "item_code": item.name}, "inventory_synced_on"
			),
			"a drained push marks the link, which is what stops this firing for ever",
		)

	def test_an_imported_product_does_not_have_its_shopify_stock_zeroed(self):
		"""The hazard in pushing from the inbound path. A catalogue imported *from* Shopify
		creates ERPNext Items with no stock, and sending that zero would wipe the quantity
		the merchant actually has on the shelf."""
		from shopify_integration.catalogue.mapping import write_product_mapping
		from shopify_integration.sync import engine

		sku = f"ZZ-IMPORTED-{frappe.generate_hash(length=6)}"
		with patch.object(engine, "schedule_drain"), patch("shopify_integration.sync.engine.schedule_drain"):
			write_product_mapping(self.store, self._webhook_product(sku))
		frappe.db.commit()

		queued = frappe.get_all(
			"Shopify Sync Queue", filters={"store": self.store, "operation": "inventory"}, pluck="dedupe_key"
		)
		self.assertEqual([k for k in queued if sku in k], [], "ERPNext knows nothing about this item's stock")

	def test_queuing_is_idempotent(self):
		"""Both paths can run, and a product push repeats on every save. One row."""
		from shopify_integration.catalogue.mapping import write_product_mapping
		from shopify_integration.outbound.product import push_initial_state
		from shopify_integration.sync import engine

		item = self._item(7)
		with patch.object(engine, "schedule_drain"), patch("shopify_integration.sync.engine.schedule_drain"):
			write_product_mapping(self.store, self._webhook_product(item.name))
			for _ in range(4):
				push_initial_state(self.store, item.name)
		frappe.db.commit()

		rows = frappe.get_all(
			"Shopify Sync Queue",
			filters={"store": self.store, "operation": "inventory", "state": "Pending"},
			pluck="dedupe_key",
		)
		self.assertEqual(len([k for k in rows if item.name in k]), 1)

	def test_a_synced_link_stops_asking(self):
		"""Past the watermark it is not initial state any more -- the ordinary stock hooks own
		it from here, and re-sending on every link write would be a push per save."""
		from shopify_integration.catalogue.mapping import write_product_mapping
		from shopify_integration.outbound.product import push_initial_state
		from shopify_integration.sync import engine

		item = self._item(7)
		with patch.object(engine, "schedule_drain"), patch("shopify_integration.sync.engine.schedule_drain"):
			write_product_mapping(self.store, self._webhook_product(item.name))
			frappe.db.set_value(
				"Shopify Item Link",
				{"store": self.store, "item_code": item.name},
				"inventory_synced_on",
				frappe.utils.now_datetime(),
			)
			frappe.db.delete("Shopify Sync Queue", {"store": self.store})
			push_initial_state(self.store, item.name)
		frappe.db.commit()

		self.assertEqual(
			frappe.get_all(
				"Shopify Sync Queue", filters={"store": self.store, "operation": "inventory"}, pluck="name"
			),
			[],
		)
