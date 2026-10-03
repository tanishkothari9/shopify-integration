"""The publish loop of 3 October, and the five things that let it run.

Publishing one item -- three images, two in stock -- put the app and Shopify into a circle
that ran for eight minutes: 139 `products/update` webhooks, 347 queue rows, 40 timestamp
collisions and 202 media on a product that owns three.

The circle is short. Something outbound mutates the product; Shopify echoes a
`products/update` back; handling it saves the Item; saving the Item fires the doc_events
that queue the next mutation. Every lap earns the next one, and nothing in it was bounded.

These tests hold each link of that circle open on its own, and then close the circle and
check it stops.
"""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.api.client import ShopifyClient
from shopify_integration.catalogue.echo import inbound_write, mark
from shopify_integration.catalogue.mapping import product_digest, stored_digest
from shopify_integration.inbound import product as inbound_product
from shopify_integration.outbound import collections as collections_module
from shopify_integration.outbound.media import owned_media, sync_item_media
from shopify_integration.sync.engine import (
	BREAKER_MAX_ROWS,
	breaker_is_open,
	clear_breaker,
	enqueue_sync,
)
from shopify_integration.tests.test_item_media import MediaCase

PRODUCT_GID = "gid://shopify/Product/loop-1"
VARIANT_GID = "gid://shopify/ProductVariant/loop-1-v1"
INVENTORY_GID = "gid://shopify/InventoryItem/loop-1-i1"


def shopify_product(title: str = "Loop Saree", sku: str = "LOOP-1", **overrides) -> dict:
	"""One Shopify product in the shape `product_by_id` returns it."""
	product = {
		"id": PRODUCT_GID,
		"title": title,
		"description": "A saree that kept publishing itself.",
		"status": "ACTIVE",
		"vendor": "",
		"options": [{"name": "Title", "values": ["Default Title"]}],
		"variants": {
			"pageInfo": {"hasNextPage": False, "endCursor": None},
			"edges": [
				{
					"node": {
						"id": VARIANT_GID,
						"sku": sku,
						"title": "Default Title",
						"price": "4500.00",
						"selectedOptions": [{"name": "Title", "value": "Default Title"}],
						"inventoryItem": {"id": INVENTORY_GID, "measurement": {"weight": None}},
					}
				}
			],
		},
	}
	product.update(overrides)
	return product


class _ProductClient:
	"""Serves one product and counts how often it is asked for."""

	def __init__(self, product: dict):
		self.product = product
		self.reads = 0

	def execute(self, query, variables=None, cost_hint=0):
		self.reads += 1
		# A copy, because the handler flattens `variants` from a connection to a list in
		# place -- and each webhook refetches from Shopify, so each one gets it whole.
		self.reads_product = deepcopy(self.product)
		return {"product": self.reads_product}

	def paginate(self, *args, **kwargs):
		return iter(())


class FeedbackLoopCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from shopify_integration.tests.test_integration import SECRET_A, make_store

		cls.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)
		frappe.db.commit()

	def setUp(self):
		self.addCleanup(frappe.db.commit)
		self._forget_product()

	def _forget_product(self):
		for name in frappe.get_all(
			"Shopify Item Link", filters={"store": self.store, "product_gid": PRODUCT_GID}, pluck="name"
		):
			frappe.delete_doc("Shopify Item Link", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	# -- driving a webhook ---------------------------------------------------------------

	def _deliver(self, product: dict) -> dict:
		"""Hand the app a products/update, exactly as the receiver would."""
		from shopify_integration.tests.test_handlers import log_for

		event = log_for(self.store, "products/update", {"admin_graphql_api_id": PRODUCT_GID})
		self.addCleanup(
			frappe.delete_doc, "Shopify Event Log", event, force=True, ignore_permissions=True
		)
		with patch.object(
			inbound_product.ShopifyClient, "for_store", staticmethod(lambda store: _ProductClient(product))
		):
			return inbound_product.on_product_update(event)

	def _queue_counts(self) -> dict:
		rows = frappe.db.sql(
			"""SELECT operation, COUNT(*) AS n FROM `tabShopify Sync Queue`
			   WHERE store = %s GROUP BY operation""",
			self.store,
			as_dict=True,
		)
		return {row.operation: row.n for row in rows}

	def _clear_queue(self):
		frappe.db.sql("DELETE FROM `tabShopify Sync Queue` WHERE store = %s", self.store)
		frappe.db.commit()

	# -- the echo ------------------------------------------------------------------------

	def test_an_unchanged_products_update_is_dropped_before_anything_is_written(self):
		product = shopify_product()
		first = self._deliver(product)
		item = first["item"]
		self.addCleanup(frappe.delete_doc, "Item", item, force=True, ignore_permissions=True)

		self.assertEqual(stored_digest(self.store, PRODUCT_GID), product_digest(product))

		modified = frappe.db.get_value("Item", item, "modified")
		self._clear_queue()

		# Shopify echoing our own mutations back. Six of them, as it did on the day.
		for _ in range(6):
			self.assertEqual(self._deliver(product), {"skipped": "no mapped field changed"})

		self.assertEqual(
			frappe.db.get_value("Item", item, "modified"),
			modified,
			"an echoed webhook saved the Item, which is what fires the doc_events",
		)
		self.assertEqual(self._queue_counts(), {}, "an echoed webhook queued outbound work")

	def test_a_real_change_still_lands(self):
		product = shopify_product()
		item = self._deliver(product)["item"]
		self.addCleanup(frappe.delete_doc, "Item", item, force=True, ignore_permissions=True)

		changed = shopify_product(title="Loop Saree, renamed in Shopify")
		result = self._deliver(changed)

		self.assertNotIn("skipped", result)
		self.assertEqual(
			frappe.db.get_value("Item", item, "item_name"), "Loop Saree, renamed in Shopify"
		)
		self.assertEqual(stored_digest(self.store, PRODUCT_GID), product_digest(changed))

	def test_a_price_change_alone_is_not_a_change_on_this_side(self):
		"""Our own price push comes back as a products/update. It must not read as news."""
		product = shopify_product()
		item = self._deliver(product)["item"]
		self.addCleanup(frappe.delete_doc, "Item", item, force=True, ignore_permissions=True)

		echoed = shopify_product()
		echoed["variants"]["edges"][0]["node"]["price"] = "4999.00"
		self.assertEqual(self._deliver(echoed), {"skipped": "no mapped field changed"})

	def test_the_circle_closes(self):
		"""Thirty laps of the loop, and it has to stop queueing work."""
		product = shopify_product()
		item = self._deliver(product)["item"]
		self.addCleanup(frappe.delete_doc, "Item", item, force=True, ignore_permissions=True)
		self._clear_queue()

		for _ in range(30):
			self._deliver(product)

		counts = self._queue_counts()
		self.assertEqual(
			counts,
			{},
			f"thirty echoed webhooks produced {counts}; on 3 October this was 347 rows",
		)


class InitialStateCase(FrappeTestCase):
	"""`push_initial_state` must fire once for a link, not once per webhook."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from shopify_integration.tests.test_integration import SECRET_A, make_store

		cls.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)
		frappe.db.set_value("Shopify Store", cls.store, "sync_item_images", 1)
		frappe.db.commit()

	def _linked_item(self) -> tuple[str, str]:
		from shopify_integration.tests.test_integration import with_hsn

		code = f"ZZ-LOOP-{frappe.generate_hash(length=6)}"
		item = frappe.new_doc("Item")
		item.item_code = code
		item.item_name = code
		item.item_group = frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0]
		item.stock_uom = "Nos"
		with_hsn(item)
		item.flags.ignore_mandatory = True
		item.insert(ignore_permissions=True)
		item.db_set("image", "/files/loop.png", update_modified=False)

		link = frappe.new_doc("Shopify Item Link")
		link.store = self.store
		link.item_code = code
		link.product_gid = f"gid://shopify/Product/{code}"
		link.variant_gid = f"gid://shopify/ProductVariant/{code}"
		link.insert(ignore_permissions=True)
		frappe.db.commit()

		self.addCleanup(frappe.delete_doc, "Shopify Item Link", link.name, force=True, ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Item", code, force=True, ignore_permissions=True)
		self.addCleanup(frappe.db.commit)
		return code, link.name

	def test_initial_state_is_pushed_once_however_many_webhooks_arrive(self):
		from shopify_integration.outbound.product import push_initial_state

		code, link = self._linked_item()

		with patch(
			"shopify_integration.outbound.product.enqueue_media"
		) as media, patch(
			"shopify_integration.outbound.product._erpnext_holds_images", return_value=True
		):
			for _ in range(12):
				push_initial_state(self.store, code)

		self.assertEqual(
			media.call_count,
			1,
			"every upsert_link re-queued the opening push; that is 103 media rows in eight minutes",
		)
		self.assertEqual(frappe.db.get_value("Shopify Item Link", link, "initial_state_pushed"), 1)

	def test_the_marker_is_set_even_though_the_watermarks_are_not(self):
		"""The watermarks are written on drain. The marker cannot wait for that."""
		from shopify_integration.outbound.product import push_initial_state

		code, link = self._linked_item()
		with patch("shopify_integration.outbound.product.enqueue_media"), patch(
			"shopify_integration.outbound.product._erpnext_holds_images", return_value=True
		):
			push_initial_state(self.store, code)

		row = frappe.db.get_value(
			"Shopify Item Link",
			link,
			["initial_state_pushed", "inventory_synced_on", "price_synced_on"],
			as_dict=True,
		)
		self.assertEqual(row.initial_state_pushed, 1)
		self.assertIsNone(row.inventory_synced_on)
		self.assertIsNone(row.price_synced_on)


class EchoGateCase(FrappeTestCase):
	"""Every outbound doc_event must refuse a document an inbound handler wrote."""

	def test_collection_sync_is_not_queued_for_an_inbound_item_save(self):
		doc = frappe.new_doc("Item")
		doc.item_code = "ZZ-ECHO-GATE"
		doc.name = doc.item_code
		mark(doc)

		with patch.object(collections_module, "_enqueue_for_stores") as enqueue:
			collections_module.on_item_change(doc)
		enqueue.assert_not_called()

	def test_collection_sync_is_not_queued_inside_an_inbound_write(self):
		doc = frappe.new_doc("Item")
		doc.item_code = "ZZ-ECHO-GATE-2"
		doc.name = doc.item_code

		with patch.object(collections_module, "_enqueue_for_stores") as enqueue:
			with inbound_write():
				collections_module.on_item_change(doc)
		enqueue.assert_not_called()

	def test_collection_sync_still_runs_for_a_real_edit(self):
		doc = frappe.new_doc("Item")
		doc.item_code = "ZZ-ECHO-GATE-3"
		doc.name = doc.item_code

		with patch.object(collections_module, "_enqueue_for_stores") as enqueue:
			collections_module.on_item_change(doc)
		enqueue.assert_called_once_with("ZZ-ECHO-GATE-3")

	def test_a_price_written_inbound_does_not_queue_a_collection_move(self):
		doc = frappe.new_doc("Item Price")
		doc.item_code = "ZZ-ECHO-GATE-4"
		doc.name = doc.item_code
		mark(doc)

		with patch.object(collections_module, "_enqueue_for_stores") as enqueue:
			collections_module.on_price_change(doc)
		enqueue.assert_not_called()


class BreakerCase(FrappeTestCase):
	"""The last line of defence: whatever else leaks, a loop must not run for eight minutes."""

	STORE = "Test Store A"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from shopify_integration.tests.test_integration import SECRET_A, make_store

		cls.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)
		frappe.db.commit()

	def setUp(self):
		self.item = f"ZZ-BREAK-{frappe.generate_hash(length=6)}"
		for operation in ("media", "price", "inventory"):
			clear_breaker(self.store, operation, self.item)
			self.addCleanup(clear_breaker, self.store, operation, self.item)
		self.addCleanup(
			frappe.db.sql, "DELETE FROM `tabShopify Sync Queue` WHERE ref_docname = %s", self.item
		)
		self.addCleanup(frappe.db.commit)

	def _enqueue(self, operation: str, nth: int):
		return enqueue_sync(
			self.store,
			operation,
			dedupe_key=f"{operation}:{self.store}:{self.item}:{nth}",
			ref_doctype="Item",
			ref_docname=self.item,
		)

	def test_a_runaway_operation_is_stopped_and_the_rest_keeps_going(self):
		# Ten rows is a product somebody is working on. The eleventh is what makes it a
		# loop, and it is the one that trips the breaker -- so the twelfth is the first
		# refused. The row that trips it is still written; stopping starts after it.
		for nth in range(BREAKER_MAX_ROWS + 1):
			self.assertIsNotNone(self._enqueue("media", nth), f"row {nth} should have been queued")

		self.assertTrue(breaker_is_open(self.store, "media", self.item))
		self.assertIsNone(self._enqueue("media", 99), "the breaker should have stopped this one")

		self.assertIsNotNone(
			self._enqueue("price", 0),
			"the breaker is per operation: a media loop must not stop this item's prices",
		)
		self.assertFalse(breaker_is_open(self.store, "price", self.item))

	def test_another_item_is_untouched(self):
		for nth in range(BREAKER_MAX_ROWS + 1):
			self._enqueue("media", nth)
		self.assertTrue(breaker_is_open(self.store, "media", self.item))

		other = f"ZZ-BREAK-{frappe.generate_hash(length=6)}"
		self.addCleanup(clear_breaker, self.store, "media", other)
		self.addCleanup(
			frappe.db.sql, "DELETE FROM `tabShopify Sync Queue` WHERE ref_docname = %s", other
		)
		self.assertIsNotNone(
			enqueue_sync(
				self.store,
				"media",
				dedupe_key=f"media:{self.store}:{other}",
				ref_doctype="Item",
				ref_docname=other,
			)
		)

	def test_it_can_be_cleared_by_hand(self):
		for nth in range(BREAKER_MAX_ROWS + 1):
			self._enqueue("media", nth)
		self.assertTrue(breaker_is_open(self.store, "media", self.item))

		clear_breaker(self.store, "media", self.item)
		self.assertFalse(breaker_is_open(self.store, "media", self.item))
		self.assertIsNotNone(self._enqueue("media", 500))

	def test_stock_is_never_stopped(self):
		"""The one operation the breaker must not touch.

		A breaker spends correctness to contain a fault. For a title that is a fair trade;
		for stock it is the trade the whole app exists to refuse, because the harm of
		stopping is Shopify selling units ERPNext knows are gone. Eleven sales of one SKU
		in five minutes is a flash sale, not a loop.
		"""
		for nth in range(BREAKER_MAX_ROWS * 3):
			self.assertIsNotNone(
				enqueue_sync(
					self.store,
					"inventory",
					dedupe_key=f"inventory:{self.store}:{self.item}:{nth}",
					ref_doctype="Item",
					ref_docname=self.item,
				),
				f"stock push {nth} was refused; that is how a store oversells",
			)
		self.assertFalse(breaker_is_open(self.store, "inventory", self.item))

	def test_a_coalesced_enqueue_is_not_counted(self):
		"""Rows, not attempts.

		An enqueue that folds into an existing Pending row is the queue doing its job --
		one row standing in for a burst of edits. Counting those would trip the breaker on
		a product somebody is simply working on.
		"""
		key = f"media:{self.store}:{self.item}:one"
		first = enqueue_sync(
			self.store, "media", dedupe_key=key, ref_doctype="Item", ref_docname=self.item
		)
		self.assertIsNotNone(first)

		for _ in range(BREAKER_MAX_ROWS * 3):
			self.assertIsNone(
				enqueue_sync(
					self.store, "media", dedupe_key=key, ref_doctype="Item", ref_docname=self.item
				),
				"a second row was written for one dedupe key",
			)

		self.assertFalse(
			breaker_is_open(self.store, "media", self.item),
			"thirty coalesced enqueues tripped the breaker; only written rows should count",
		)

	def test_the_flag_is_visible_within_the_same_job(self):
		"""Frappe's `get_value` memoises a miss into `frappe.local.cache` for the whole job.

		The breaker is set and read inside one drain, so a flag read through that memo is
		read as "closed" for ever after the first check -- and the breaker never fires.
		"""
		self.assertFalse(breaker_is_open(self.store, "media", self.item))
		for nth in range(BREAKER_MAX_ROWS + 1):
			self._enqueue("media", nth)
		self.assertTrue(
			breaker_is_open(self.store, "media", self.item),
			"the open flag was set but could not be read back in the same job",
		)


class _LaggingClient:
	"""Shopify, with its media connection running behind the create that filled it.

	`productCreateMedia` hands back the ids straight away; the product's `media` connection
	does not list them for a while afterwards. That gap is what the loop fell into.
	"""

	def __init__(self):
		self.media: list[dict] = []
		self.created: list[dict] = []
		self.deleted: list[str] = []
		self.hide = False

	def execute(self, query, variables=None, cost_hint=0):
		variables = variables or {}

		if "media" in variables and "productId" in variables:
			self.created.extend(variables["media"])
			made = []
			for entry in variables["media"]:
				node = {
					"id": f"gid://shopify/MediaImage/{entry['originalSource'].rsplit('/', 1)[-1]}",
					"status": "UPLOADED",
					"mediaErrors": [],
				}
				self.media.append(node)
				made.append(node)
			return {"productCreateMedia": {"media": made, "mediaUserErrors": []}}

		if "mediaIds" in variables:
			self.deleted.extend(variables["mediaIds"])
			self.media = [m for m in self.media if m["id"] not in variables["mediaIds"]]
			return {
				"productDeleteMedia": {
					"deletedMediaIds": variables["mediaIds"],
					"mediaUserErrors": [],
				}
			}

		if "moves" in variables:
			return {"productReorderMedia": {"job": None, "mediaUserErrors": []}}

		nodes = [] if self.hide else self.media
		return {"product": {"id": "p", "media": {"nodes": nodes}}}

	def paginate(self, *args, **kwargs):
		return iter(())


class MediaReuploadCase(MediaCase):
	"""202 media on a product that owns three.

	Inherits MediaCase for the item, attachment and link fixtures, and for the `public_url`
	patch -- the site is a localhost one, so a real public URL would be refused.
	"""

	def _item_with_three_images(self) -> tuple[str, str]:
		code = self._item()
		for name in ("one.png", "two.png", "three.png"):
			self._attach(code, name)
		link = self._link(code, product="gid://shopify/Product/lag-1")
		return code, link.name

	def test_a_file_already_uploaded_is_never_uploaded_again_while_shopify_lags(self):
		code, link = self._item_with_three_images()
		client = _LaggingClient()

		sync_item_media(client, self.store_doc, code)
		self.assertEqual(len(client.created), 3)
		self.assertEqual(len(owned_media(link)), 3)

		# Shopify now stops listing them -- exactly the window the loop fell into, six times.
		client.hide = True
		for _ in range(6):
			sync_item_media(client, self.store_doc, code)

		self.assertEqual(
			len(client.created),
			3,
			f"the same three files were uploaded {len(client.created)} times; on the live "
			"store this reached 202 media on one product",
		)
		self.assertEqual(len(owned_media(link)), 3, "ownership was lost while Shopify lagged")

	def test_an_image_the_merchant_deleted_does_come_back_once_the_absence_is_settled(self):
		"""The other half. A lag must not be read as a deletion -- nor the reverse."""
		from shopify_integration.outbound import media as media_module

		code, _link = self._item_with_three_images()
		client = _LaggingClient()
		sync_item_media(client, self.store_doc, code)
		self.assertEqual(len(client.created), 3)

		# The merchant deletes all three in the Shopify admin, and time passes.
		client.media = []
		with patch.object(media_module, "MEDIA_SETTLE_SECONDS", -1):
			sync_item_media(client, self.store_doc, code)

		self.assertEqual(
			len(client.created), 6, "an image deleted in Shopify was never restored from ERPNext"
		)

	def test_an_absence_that_has_not_settled_is_left_alone(self):
		"""Same absence, no time passed: nothing is re-uploaded and nothing is forgotten."""
		code, link = self._item_with_three_images()
		client = _LaggingClient()
		sync_item_media(client, self.store_doc, code)

		client.media = []
		sync_item_media(client, self.store_doc, code)

		self.assertEqual(len(client.created), 3)
		self.assertEqual(len(owned_media(link)), 3)


class _DuplicateProductClient:
	"""A product carrying the wreckage of the loop: copies, originals and the merchant's."""

	def __init__(self, nodes: list[dict]):
		self.nodes = list(nodes)
		self.deleted: list[str] = []

	def execute(self, query, variables=None, cost_hint=0):
		variables = variables or {}
		if "mediaIds" in variables:
			self.deleted.extend(variables["mediaIds"])
			self.nodes = [n for n in self.nodes if n["id"] not in variables["mediaIds"]]
			return {
				"productDeleteMedia": {
					"deletedMediaIds": variables["mediaIds"],
					"mediaUserErrors": [],
				}
			}
		return {"product": {"id": "p", "media": {"nodes": self.nodes}}}


class PruneDuplicateMediaCase(MediaCase):
	"""The one-off repair for the product that ended up with 202 media."""

	CDN = "https://cdn.shopify.com/s/files/1/0/1/files"

	def _node(self, gid: str, filename: str) -> dict:
		return {"id": gid, "status": "READY", "image": {"url": f"{self.CDN}/{filename}?v=1"}}

	def _wreckage(self):
		"""Three owned images, three copies of them, and one photograph of the merchant's."""
		code = self._item()
		owned = {
			"/files/saree-red.png": "gid://shopify/MediaImage/own-1",
			"/files/saree-blue.png": "gid://shopify/MediaImage/own-2",
			"/files/saree-gold.png": "gid://shopify/MediaImage/own-3",
		}
		link = self._link(code, product="gid://shopify/Product/wreck-1", owned=owned)
		nodes = [
			self._node("gid://shopify/MediaImage/own-1", "saree-red.png"),
			self._node("gid://shopify/MediaImage/own-2", "saree-blue.png"),
			self._node("gid://shopify/MediaImage/own-3", "saree-gold.png"),
			self._node("gid://shopify/MediaImage/dupe-1", "saree-red_2.png"),
			self._node("gid://shopify/MediaImage/dupe-2", "saree-blue_a1b2c3d4.png"),
			self._node("gid://shopify/MediaImage/dupe-3", "saree-gold_17.png"),
			self._node("gid://shopify/MediaImage/theirs", "studio-shot.png"),
		]
		return code, link, _DuplicateProductClient(nodes)

	def test_it_reports_without_deleting_until_asked(self):
		from shopify_integration.outbound.media import prune_duplicate_media

		code, _link, client = self._wreckage()
		with patch.object(ShopifyClient, "for_store", staticmethod(lambda store: client)):
			report = prune_duplicate_media(self.store, code)

		self.assertFalse(report["applied"])
		self.assertEqual(client.deleted, [], "a dry run deleted something")
		self.assertEqual({d["id"] for d in report["duplicates"]}, {
			"gid://shopify/MediaImage/dupe-1",
			"gid://shopify/MediaImage/dupe-2",
			"gid://shopify/MediaImage/dupe-3",
		})

	def test_it_removes_only_the_copies(self):
		from shopify_integration.outbound.media import prune_duplicate_media

		code, _link, client = self._wreckage()
		with patch.object(ShopifyClient, "for_store", staticmethod(lambda store: client)):
			report = prune_duplicate_media(self.store, code, apply=True)

		self.assertTrue(report["applied"])
		self.assertEqual(report["deleted"], 3)
		self.assertEqual(
			[n["id"] for n in client.nodes],
			[
				"gid://shopify/MediaImage/own-1",
				"gid://shopify/MediaImage/own-2",
				"gid://shopify/MediaImage/own-3",
				"gid://shopify/MediaImage/theirs",
			],
			"the repair took something that was not a copy",
		)

	def test_a_merchant_photograph_is_never_a_candidate(self):
		"""Not even when its name merely starts the same. `-closeup` is not `_2`."""
		from shopify_integration.outbound.media import prune_duplicate_media

		code = self._item()
		self._link(
			code,
			product="gid://shopify/Product/wreck-2",
			owned={"/files/saree-red.png": "gid://shopify/MediaImage/own-1"},
		)
		client = _DuplicateProductClient(
			[
				self._node("gid://shopify/MediaImage/own-1", "saree-red.png"),
				self._node("gid://shopify/MediaImage/theirs", "saree-red-closeup.png"),
			]
		)
		with patch.object(ShopifyClient, "for_store", staticmethod(lambda store: client)):
			report = prune_duplicate_media(self.store, code, apply=True)

		self.assertEqual(report["duplicates"], [])
		self.assertEqual(client.deleted, [])
