"""Price sync against a real site (spec §9.1, phase 5)."""

from __future__ import annotations

from unittest.mock import patch

import frappe

from shopify_integration.outbound import price as price_module
from shopify_integration.sync import engine
from shopify_integration.tests.test_handlers import FakeClient
from shopify_integration.tests.test_integration import with_hsn
from shopify_integration.tests.test_orders import OrderTestCase


class PriceTestCase(OrderTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.price_list = f"Shopify - {cls.store}"[:140]
		if not frappe.db.exists("Price List", cls.price_list):
			doc = frappe.new_doc("Price List")
			doc.price_list_name = cls.price_list
			doc.selling = 1
			doc.enabled = 1
			doc.currency = frappe.get_cached_value("Company", cls.store_doc.company, "default_currency")
			doc.insert(ignore_permissions=True)

		cls.other_list = "Wholesale Test"
		if not frappe.db.exists("Price List", cls.other_list):
			doc = frappe.new_doc("Price List")
			doc.price_list_name = cls.other_list
			doc.selling = 1
			doc.enabled = 1
			doc.currency = frappe.get_cached_value("Company", cls.store_doc.company, "default_currency")
			doc.insert(ignore_permissions=True)

		frappe.db.set_value("Shopify Store", cls.store, "sync_prices", 1)
		frappe.db.set_value("Shopify Store", cls.store, "selling_price_list", None)
		frappe.clear_cache(doctype="Shopify Store")
		frappe.db.commit()

	def setUp(self):
		frappe.db.delete("Shopify Sync Queue")

	def set_price(self, rate, price_list=None):
		price_list = price_list or self.price_list
		existing = frappe.db.get_value(
			"Item Price", {"item_code": "ORD-TEE", "price_list": price_list}, "name"
		)
		doc = frappe.get_doc("Item Price", existing) if existing else frappe.new_doc("Item Price")
		doc.item_code = "ORD-TEE"
		doc.price_list = price_list
		doc.selling = 1
		doc.price_list_rate = rate
		doc.save(ignore_permissions=True)
		return doc


class TestPriceTriggers(PriceTestCase):
	def test_a_price_change_queues_a_push(self):
		with patch.object(engine, "schedule_drain"):
			self.set_price(59.99)

		rows = frappe.get_all(
			"Shopify Sync Queue", filters={"operation": "price"}, fields=["dedupe_key", "ref_docname"]
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].dedupe_key, f"price:{self.store}:ORD-TEE")
		self.assertEqual(rows[0].ref_docname, "ORD-TEE")

	def test_a_price_on_another_list_is_ignored(self):
		"""A wholesale or cost price must never reach a shop's public listing."""
		with patch.object(engine, "schedule_drain"):
			self.set_price(12.00, price_list=self.other_list)

		self.assertEqual(frappe.db.count("Shopify Sync Queue", {"operation": "price"}), 0)

	def test_repeated_edits_coalesce(self):
		with patch.object(engine, "schedule_drain"):
			for rate in (10, 20, 30):
				self.set_price(rate)

		self.assertEqual(frappe.db.count("Shopify Sync Queue", {"operation": "price"}), 1)

	def test_sync_prices_off_does_not_queue(self):
		frappe.db.set_value("Shopify Store", self.store, "sync_prices", 0)
		frappe.clear_cache(doctype="Shopify Store")
		try:
			with patch.object(engine, "schedule_drain"):
				self.set_price(77.00)
			self.assertEqual(frappe.db.count("Shopify Sync Queue", {"operation": "price"}), 0)
		finally:
			frappe.db.set_value("Shopify Store", self.store, "sync_prices", 1)
			frappe.clear_cache(doctype="Shopify Store")

	def test_unlinked_item_does_not_queue(self):
		doc = frappe.new_doc("Item Price")
		doc.item_code = "NOT-ON-SHOPIFY"
		doc.price_list = self.price_list
		doc.selling = 1
		doc.price_list_rate = 5
		if not frappe.db.exists("Item", "NOT-ON-SHOPIFY"):
			item = frappe.new_doc("Item")
			item.item_code = "NOT-ON-SHOPIFY"
			item.item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
			item.stock_uom = "Nos"
			with_hsn(item)
			item.insert(ignore_permissions=True)
		with patch.object(engine, "schedule_drain"):
			doc.insert(ignore_permissions=True)

		self.assertEqual(frappe.db.count("Shopify Sync Queue", {"operation": "price"}), 0)

	def test_the_hook_is_registered(self):
		from shopify_integration import hooks

		self.assertEqual(
			hooks.doc_events["Item Price"]["on_change"],
			"shopify_integration.outbound.price.on_price_change",
		)


class TestPricePush(PriceTestCase):
	def test_push_sends_the_current_price_grouped_by_product(self):
		with patch.object(engine, "schedule_drain"):
			self.set_price(59.99)

		client = FakeClient(
			{
				"productVariantsBulkUpdate": {
					"productVariantsBulkUpdate": {"productVariants": [], "userErrors": []}
				}
			}
		)
		rows = [{"ref_docname": "ORD-TEE", "dedupe_key": f"price:{self.store}:ORD-TEE"}]

		with patch.object(price_module.ShopifyClient, "for_store", return_value=client):
			price_module.push_prices(self.store, rows)

		self.assertEqual(len(client.calls), 1)
		variables = client.calls[0]["variables"]
		self.assertTrue(variables["productId"].startswith("gid://shopify/Product/"))
		self.assertEqual(variables["variants"][0]["price"], "59.99")

	def test_push_reads_the_price_at_drain_time_not_from_the_row(self):
		"""A row that waited behind a backlog must push what the item costs now."""
		with patch.object(engine, "schedule_drain"):
			self.set_price(10.00)
			self.set_price(88.50)

		client = FakeClient({"productVariantsBulkUpdate": {"productVariantsBulkUpdate": {"userErrors": []}}})
		with patch.object(price_module.ShopifyClient, "for_store", return_value=client):
			price_module.push_prices(
				self.store, [{"ref_docname": "ORD-TEE", "dedupe_key": f"price:{self.store}:ORD-TEE"}]
			)

		self.assertEqual(client.calls[0]["variables"]["variants"][0]["price"], "88.50")

	def test_an_item_with_no_price_is_skipped_rather_than_zeroed(self):
		"""Sending 0 would zero the product on Shopify, which is far worse than doing nothing."""
		existing = frappe.db.get_value(
			"Item Price", {"item_code": "ORD-TEE", "price_list": self.price_list}, "name"
		)
		if existing:
			frappe.delete_doc("Item Price", existing, force=True, ignore_permissions=True)

		client = FakeClient({"productVariantsBulkUpdate": {"productVariantsBulkUpdate": {"userErrors": []}}})
		with patch.object(price_module.ShopifyClient, "for_store", return_value=client):
			price_module.push_prices(
				self.store, [{"ref_docname": "ORD-TEE", "dedupe_key": f"price:{self.store}:ORD-TEE"}]
			)

		self.assertEqual(client.calls, [], "no price means no call at all")

	def test_compare_at_price_is_never_sent(self):
		"""It is the merchant's promotional 'was' price; overwriting it clears a promotion."""
		with patch.object(engine, "schedule_drain"):
			self.set_price(25.00)

		client = FakeClient({"productVariantsBulkUpdate": {"productVariantsBulkUpdate": {"userErrors": []}}})
		with patch.object(price_module.ShopifyClient, "for_store", return_value=client):
			price_module.push_prices(
				self.store, [{"ref_docname": "ORD-TEE", "dedupe_key": f"price:{self.store}:ORD-TEE"}]
			)

		self.assertNotIn("compareAtPrice", client.calls[0]["variables"]["variants"][0])

	def test_an_unmapped_item_is_skipped_rather_than_raised(self):
		"""It used to raise, and that was worse than it looked.

		`push_prices` resolves every claimed row before sending anything, so one unmapped item
		failed the whole claimed batch -- every other row in it went to Failed carrying an
		unrelated item's message. Items lose their mapping routinely: a `products/delete`
		webhook unlinks one while a price row is still pending.
		"""
		client = FakeClient({})
		with patch.object(price_module.ShopifyClient, "for_store", return_value=client):
			price_module.push_prices(
				self.store, [{"ref_docname": "NOT-ON-SHOPIFY", "dedupe_key": "price:x:NOT-ON-SHOPIFY"}]
			)

		self.assertEqual(client.calls, [], "an unmapped item has nothing to push")


class TestOneBadItemDoesNotSinkTheBatch(PriceTestCase):
	"""The queue's promise: a poison row never blocks the rows behind it.

	`push_prices` groups every claimed row before sending anything, so raising on one unmapped
	item used to fail the whole group — up to 49 correct price changes parked as Failed with
	another item's message, needing a manual requeue. An item loses its mapping routinely: a
	`products/delete` webhook unlinks it while a price row is still pending.
	"""

	def test_an_unmapped_item_is_skipped_and_the_rest_still_push(self):
		from shopify_integration.outbound import price as price_module

		sent = []

		class Client:
			def execute(self, query, variables, cost_hint=0):
				sent.append(variables)
				return {"productVariantsBulkUpdate": {"productVariants": [], "userErrors": []}}

		with patch.object(engine, "schedule_drain"):
			self.set_price(44.50)

		rows = [
			{"ref_docname": "ORD-TEE", "dedupe_key": f"price:{self.store}:ORD-TEE"},
			{"ref_docname": "NEVER-MAPPED-ITEM", "dedupe_key": f"price:{self.store}:NEVER-MAPPED-ITEM"},
		]

		with patch.object(price_module.ShopifyClient, "for_store", return_value=Client()):
			price_module.push_prices(self.store, rows)

		self.assertTrue(
			sent,
			"the mapped item's price must still reach Shopify even though the other item is "
			"unmapped -- that is the whole point of skipping rather than raising",
		)


class TestTheItemCarriesAPriceToo(PriceTestCase):
	"""ERPNext files a rate typed on the Item form under the price list in Selling Settings.

	That is rarely the list a Shopify store points at. So a merchant sets a saree at 1,200,
	publishes it, and it goes live at **0.00** — the price is there, just on another list, and
	nothing anywhere says so. Found on a real item.

	Standard Selling Rate is therefore a fallback source, and saving an Item has to queue a
	price push, or the rate stays readable and never sent.
	"""

	def setUp(self):
		super().setUp() if hasattr(super(), "setUp") else None
		self.item = "_Test Std Rate Item"
		if not frappe.db.exists("Item", self.item):
			doc = frappe.new_doc("Item")
			doc.item_code = self.item
			doc.item_name = self.item
			doc.item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
			doc.stock_uom = "Nos"
			with_hsn(doc)
			doc.insert(ignore_permissions=True)
		frappe.db.delete("Item Price", {"item_code": self.item})
		frappe.db.set_value("Item", self.item, "standard_rate", 0, update_modified=False)
		frappe.db.commit()

	def tearDown(self):
		frappe.db.delete("Item Price", {"item_code": self.item})
		if frappe.db.exists("Item", self.item):
			frappe.delete_doc("Item", self.item, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _store_doc(self):
		return frappe.get_cached_doc("Shopify Store", self.store)

		# --- reading the price

	def test_the_stores_own_list_wins(self):
		frappe.db.set_value("Item", self.item, "standard_rate", 999, update_modified=False)
		ip = frappe.new_doc("Item Price")
		ip.item_code = self.item
		ip.price_list = self.price_list
		ip.price_list_rate = 1500
		ip.selling = 1
		ip.insert(ignore_permissions=True)
		frappe.db.commit()

		self.assertEqual(float(price_module.current_price(self._store_doc(), self.item)), 1500.0)

	def test_the_standard_rate_is_used_when_the_list_has_nothing(self):
		frappe.db.set_value("Item", self.item, "standard_rate", 1200, update_modified=False)
		frappe.db.commit()

		self.assertEqual(float(price_module.current_price(self._store_doc(), self.item)), 1200.0)

	def test_a_price_on_another_list_does_not_leak(self):
		"""A wholesale rate is not a shop price. Only this store's list, or the Item's own."""
		ip = frappe.new_doc("Item Price")
		ip.item_code = self.item
		ip.price_list = self.other_list
		ip.price_list_rate = 77
		ip.selling = 1
		ip.insert(ignore_permissions=True)
		frappe.db.commit()

		self.assertIsNone(price_module.current_price(self._store_doc(), self.item))

	def test_no_price_anywhere_is_none_not_zero(self):
		"""None, so a product can be held back rather than listed free."""
		self.assertIsNone(price_module.current_price(self._store_doc(), self.item))

	def test_a_zero_standard_rate_is_not_a_price(self):
		frappe.db.set_value("Item", self.item, "standard_rate", 0, update_modified=False)
		frappe.db.commit()

		self.assertIsNone(price_module.current_price(self._store_doc(), self.item))

		# --- sending it

	def test_saving_the_item_queues_a_price_push(self):
		"""The rate lives on the Item, so an Item save has to be able to send it."""
		link = frappe.new_doc("Shopify Item Link")
		link.store = self.store
		link.item_code = self.item
		link.product_gid = "gid://shopify/Product/9401"
		link.variant_gid = "gid://shopify/ProductVariant/9401"
		link.insert(ignore_permissions=True)
		frappe.db.delete("Shopify Sync Queue", {"store": self.store, "operation": "price"})
		frappe.db.commit()

		doc = frappe.get_doc("Item", self.item)
		doc.standard_rate = 1200
		with patch.object(engine, "schedule_drain"):
			doc.save(ignore_permissions=True)

		self.assertEqual(
			frappe.db.count(
				"Shopify Sync Queue",
				{"store": self.store, "operation": "price", "ref_docname": self.item},
			),
			1,
			"the Item's own rate can never reach Shopify if saving it queues nothing",
		)
		frappe.delete_doc("Shopify Item Link", link.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_an_unlinked_item_queues_nothing(self):
		doc = frappe.get_doc("Item", self.item)
		doc.standard_rate = 500
		with patch.object(engine, "schedule_drain"):
			doc.save(ignore_permissions=True)

		self.assertEqual(
			frappe.db.count(
				"Shopify Sync Queue",
				{"store": self.store, "operation": "price", "ref_docname": self.item},
			),
			0,
		)
