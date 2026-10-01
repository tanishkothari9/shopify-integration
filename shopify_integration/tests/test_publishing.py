"""A product created by the app has to end up somewhere a customer can see it.

Creating a product leaves it in the admin with onlineStoreUrl null: every field correct, and
invisible to the storefront. Publishing to the Online Store is a separate call nobody notices
is missing until they look at the shop.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.outbound.product import (
	ONLINE_STORE_APP_HANDLE,
	online_store_publication,
	publish_to_online_store,
)
from shopify_integration.tests.test_integration import with_hsn


class _Client:
	"""Records what was asked of Shopify."""

	def __init__(self, publications=None, fail=False):
		self._publications = (
			publications
			if publications is not None
			else [
				{
					"id": "gid://shopify/Publication/1",
					"catalog": {"title": "Online Store", "apps": {"nodes": [{"handle": "online_store"}]}},
				}
			]
		)
		self.fail = fail
		self.calls = []

	def execute(self, query, variables, cost_hint=0):
		self.calls.append(variables)
		if self.fail:
			raise RuntimeError("Access denied for publications field")
		if "id" in variables:
			return {"publishablePublish": {"publishable": {"id": variables["id"]}, "userErrors": []}}
		return {"publications": {"nodes": self._publications}}


def _no_cache():
	"""Each test looks the publication up fresh."""
	if hasattr(frappe.local, "shopify_online_store_publication"):
		delattr(frappe.local, "shopify_online_store_publication")


class TestFindingTheOnlineStore(FrappeTestCase):
	def setUp(self):
		_no_cache()
		self.addCleanup(_no_cache)

	def test_the_online_store_is_found_by_the_app_handle(self):
		client = _Client()
		self.assertEqual(online_store_publication(client, "ZZ Publish Store"), "gid://shopify/Publication/1")

	def test_a_localised_title_still_matches_on_the_handle(self):
		"""The title is translated; the handle is not. Matching on the title alone would leave
		every non-English shop unpublished."""
		client = _Client(
			[
				{
					"id": "gid://shopify/Publication/9",
					"catalog": {
						"title": "Boutique en ligne",
						"apps": {"nodes": [{"handle": ONLINE_STORE_APP_HANDLE}]},
					},
				}
			]
		)
		self.assertEqual(online_store_publication(client, "ZZ Publish Store"), "gid://shopify/Publication/9")

	def test_other_channels_are_not_mistaken_for_it(self):
		client = _Client(
			[
				{
					"id": "gid://shopify/Publication/2",
					"catalog": {"title": "Point of Sale", "apps": {"nodes": [{"handle": "pos"}]}},
				}
			]
		)
		self.assertIsNone(online_store_publication(client, "ZZ Publish Store"))

	def test_the_lookup_is_remembered_for_the_job(self):
		"""One drain can create fifty products; they must not ask fifty times."""
		client = _Client()
		for _ in range(5):
			online_store_publication(client, "ZZ Publish Store")
		self.assertEqual(len(client.calls), 1, "the publication id should be looked up once")

	def test_a_shop_without_one_is_not_asked_again_either(self):
		client = _Client([])
		self.assertIsNone(online_store_publication(client, "ZZ Publish Store"))
		self.assertIsNone(online_store_publication(client, "ZZ Publish Store"))
		self.assertEqual(len(client.calls), 1)


class TestPublishing(FrappeTestCase):
	def setUp(self):
		_no_cache()
		self.addCleanup(_no_cache)

	def test_a_product_is_published_to_the_online_store(self):
		client = _Client()
		self.assertTrue(publish_to_online_store(client, "ZZ Publish Store", "gid://shopify/Product/5"))
		published = client.calls[-1]
		self.assertEqual(published["id"], "gid://shopify/Product/5")
		self.assertEqual(published["input"], [{"publicationId": "gid://shopify/Publication/1"}])

	def test_a_shop_with_no_online_store_is_not_an_error(self):
		client = _Client([])
		self.assertFalse(publish_to_online_store(client, "ZZ Publish Store", "gid://shopify/Product/5"))

	def test_a_missing_scope_does_not_lose_the_product(self):
		"""A shop installed before the app asked for read_publications refuses this call.
		Failing the whole product sync over a channel assignment would be the worse outcome --
		the product exists either way."""
		client = _Client(fail=True)
		self.assertFalse(publish_to_online_store(client, "ZZ Publish Store", "gid://shopify/Product/5"))


class TestTheDecisionIsMadeOnce(FrappeTestCase):
	"""Publishing is a one-time decision, recorded so no later save revisits or re-reads it.

	Two things hang on that. A merchant who takes a product off the storefront must not find
	it back there after an unrelated ERPNext edit. And an ordinary item save must not cost an
	extra Shopify call just to ask a question already answered.
	"""

	def test_an_undecided_product_is_reported_as_such(self):
		from shopify_integration.outbound.product import _publish_decided

		self._link()
		self.assertFalse(_publish_decided(self.store, "gid://shopify/Product/ZZPUB"))

	def test_marking_it_covers_every_link_on_the_product(self):
		"""A variant product is one Shopify product and many links; the channel belongs to the
		product, so one decision covers all of them."""
		from shopify_integration.outbound.product import _mark_publish_decided, _publish_decided

		self._link(item_code="ZZ-PUB-A", variant="gid://shopify/ProductVariant/A")
		self._link(item_code="ZZ-PUB-B", variant="gid://shopify/ProductVariant/B")

		self.assertFalse(_publish_decided(self.store, "gid://shopify/Product/ZZPUB"))
		_mark_publish_decided(self.store, "gid://shopify/Product/ZZPUB")
		self.assertTrue(
			_publish_decided(self.store, "gid://shopify/Product/ZZPUB"),
			"the decision belongs to the product, which is what a template has",
		)

	def _link(self, item_code="ZZ-PUB-A", variant="gid://shopify/ProductVariant/A"):
		if not hasattr(self, "store"):
			self.store = frappe.get_all("Shopify Store", limit=1, pluck="name")[0]
		if not frappe.db.exists("Item", item_code):
			item = frappe.new_doc("Item")
			item.item_code = item_code
			item.item_name = item_code
			item.item_group = frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0]
			item.stock_uom = "Nos"
			with_hsn(item)
			item.insert(ignore_permissions=True)
			self.addCleanup(frappe.delete_doc, "Item", item_code, force=True, ignore_permissions=True)

		doc = frappe.new_doc("Shopify Item Link")
		doc.store = self.store
		doc.item_code = item_code
		doc.product_gid = "gid://shopify/Product/ZZPUB"
		doc.variant_gid = variant
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		self.addCleanup(frappe.delete_doc, "Shopify Item Link", doc.name, force=True, ignore_permissions=True)
		return doc.name


class TestScopes(FrappeTestCase):
	def test_the_publication_scopes_are_requested(self):
		from shopify_integration.api.oauth import REQUIRED_SCOPES

		self.assertIn("read_publications", REQUIRED_SCOPES)
		self.assertIn("write_publications", REQUIRED_SCOPES)

	def test_the_docs_list_them_too(self):
		"""A merchant who follows the setup guide must end up with the scopes the app needs."""
		import pathlib

		root = pathlib.Path(frappe.get_app_path("shopify_integration")).parent
		for name in ("README.md", "docs/prerequisites.md"):
			text = (root / name).read_text()
			self.assertIn("read_publications", text, f"{name} should list read_publications")
			self.assertIn("write_publications", text, f"{name} should list write_publications")


class TestStockOnPublish(FrappeTestCase):
	"""A product published with three on the shelf must not go live showing zero.

	Inventory is otherwise pushed only when stock *moves*, so a newly listed item stayed at
	zero until someone sold or received one, or the 03:00 reconciliation came round -- listed
	as out of stock on the day it appears.
	"""

	def setUp(self):
		from shopify_integration.tests.test_integration import SECRET_A, make_store

		self.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)
		self.warehouse = frappe.db.get_value(
			"Warehouse",
			{"company": frappe.db.get_value("Shopify Store", self.store, "company"), "is_group": 0},
			"name",
		)
		doc = frappe.get_doc("Shopify Store", self.store)
		doc.sync_inventory = 1
		doc.set("location_map", [])
		doc.append(
			"location_map",
			{
				"location_gid": "gid://shopify/Location/1",
				"location_name": "Shop",
				"warehouse": self.warehouse,
			},
		)
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_document_cache("Shopify Store", self.store)
		frappe.db.delete("Shopify Sync Queue", {"store": self.store})
		frappe.db.commit()

	def tearDown(self):
		frappe.clear_document_cache("Shopify Store", self.store)
		doc = frappe.get_doc("Shopify Store", self.store)
		doc.sync_inventory = 0
		doc.set("location_map", [])
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_document_cache("Shopify Store", self.store)

	def test_linking_an_item_queues_its_current_stock(self):
		from shopify_integration.outbound.product import push_initial_stock

		queued = push_initial_stock(self.store, "ORD-TEE")
		self.assertTrue(queued, "publishing should queue what ERPNext already has")
		self.assertTrue(
			frappe.db.count("Shopify Sync Queue", {"store": self.store, "operation": "inventory"})
		)

	def test_a_store_with_inventory_sync_off_pushes_nothing(self):
		from shopify_integration.outbound.product import push_initial_stock

		frappe.db.set_value("Shopify Store", self.store, "sync_inventory", 0)
		frappe.db.commit()
		frappe.clear_document_cache("Shopify Store", self.store)

		self.assertEqual(push_initial_stock(self.store, "ORD-TEE"), 0)

	def test_every_mapped_warehouse_is_covered(self):
		"""A store can sell one item from more than one location."""
		from shopify_integration.outbound.product import push_initial_stock

		other = frappe.db.get_value(
			"Warehouse",
			{
				"company": frappe.db.get_value("Shopify Store", self.store, "company"),
				"is_group": 0,
				"name": ["!=", self.warehouse],
			},
			"name",
		)
		if not other:
			self.skipTest("this site has only one leaf warehouse")

		doc = frappe.get_doc("Shopify Store", self.store)
		doc.append(
			"location_map",
			{"location_gid": "gid://shopify/Location/2", "location_name": "Second", "warehouse": other},
		)
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_document_cache("Shopify Store", self.store)

		self.assertEqual(push_initial_stock(self.store, "ORD-TEE"), 2)


class TestTheWiring(FrappeTestCase):
	"""Helpers being right is worth nothing if nothing calls them.

	These assert the call sites, not the functions: remove the publish from the create path
	or the stock push from the link, and these are what notice.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from shopify_integration.tests.test_integration import SECRET_A, make_store

		cls.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)

	def _item(self, code):
		if not frappe.db.exists("Item", code):
			item = frappe.new_doc("Item")
			item.item_code = code
			item.item_name = code
			item.item_group = frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0]
			item.stock_uom = "Nos"
			with_hsn(item)
			item.insert(ignore_permissions=True)
		for link in frappe.get_all(
			"Shopify Item Link", filters={"store": self.store, "item_code": code}, pluck="name"
		):
			frappe.delete_doc("Shopify Item Link", link, force=True, ignore_permissions=True)
		return code

	def test_writing_a_link_pushes_what_erpnext_holds(self):
		"""The push sits in `upsert_link` rather than at the call sites, because the call
		sites race: the `products/create` webhook for a product being published arrives
		through the mapping, not through the create path, and whichever writes the link first
		has to be the one that queues the stock."""
		from unittest.mock import patch

		from shopify_integration.outbound import product as product_module

		code = self._item("ZZ-WIRE-A")
		with patch.object(product_module, "push_initial_state") as pushed:
			product_module._link(
				self.store,
				code,
				{
					"id": "gid://shopify/Product/wire-a",
					"variants": [{"id": "gid://shopify/ProductVariant/wire-a"}],
				},
			)

		pushed.assert_called_once_with(self.store, code)

	def test_linking_variants_pushes_for_each_variant(self):
		from unittest.mock import patch

		from shopify_integration.outbound import product as product_module

		self._item("ZZ-WIRE-TPL")
		self._item("ZZ-WIRE-B")
		self._item("ZZ-WIRE-C")
		product = {
			"id": "gid://shopify/Product/wire-bc",
			"variants": [
				{"id": "gid://shopify/ProductVariant/wire-b", "sku": "ZZ-WIRE-B"},
				{"id": "gid://shopify/ProductVariant/wire-c", "sku": "ZZ-WIRE-C"},
			],
		}
		with patch.object(product_module, "push_initial_state") as pushed:
			product_module._link_variants(
				self.store,
				"ZZ-WIRE-TPL",
				product,
				[{"item_code": "ZZ-WIRE-B"}, {"item_code": "ZZ-WIRE-C"}],
			)

		self.assertEqual([call[0][1] for call in pushed.call_args_list], ["ZZ-WIRE-B", "ZZ-WIRE-C"])

	def test_a_product_created_active_is_published(self):
		"""The create path has to hand the product to the Online Store; otherwise every
		product it makes is invisible to customers."""
		import inspect

		from shopify_integration.outbound import product as product_module

		source = inspect.getsource(product_module._create_product_unlocked)
		self.assertIn(
			"publish_to_online_store",
			source,
			"creating a product must put it on the Online Store",
		)

	def test_an_update_publishes_once_and_then_leaves_it_alone(self):
		"""A product switched on is published; the next save neither publishes nor re-reads."""
		import inspect

		from shopify_integration.outbound import product as product_module

		source = inspect.getsource(product_module.push_products)
		self.assertIn("_publish_decided", source, "the update path must check the decision first")
		self.assertIn("publish_to_online_store", source)
		self.assertIn("_mark_publish_decided", source, "and record it so no later save repeats it")
