"""Outbound product sync: what ERPNext is allowed to overwrite in Shopify.

The point of this file is one bug. `push_products` sent the ERPNext item_name and
description on every save, so editing an item's *weight* in ERPNext replaced the storefront
title a customer reads and wiped the marketing HTML under it:

    "Banarasi Silk Saree, Festive Edition"  ->  "The Inventory Not Tracked Snowboard"

Nothing failed and nothing was logged. The merchant would find out from the shop.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.outbound import product as product_module
from shopify_integration.tests.test_handlers import FakeClient
from shopify_integration.tests.test_integration import SECRET_A, make_store, with_hsn

PRODUCT_GID = "gid://shopify/Product/9001"


class ProductPushTestCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)

		cls.item_code = "_Test Shopify Copy Item"
		if not frappe.db.exists("Item", cls.item_code):
			item = frappe.new_doc("Item")
			item.item_code = cls.item_code
			item.item_name = "Plain ERPNext Name"
			item.description = "Plain ERPNext description"
			item.item_group = frappe.get_all("Item Group", limit=1, pluck="name")[0]
			item.stock_uom = "Nos"
			item.is_stock_item = 1
			with_hsn(item)
			item.insert(ignore_permissions=True)

		if not frappe.db.exists("Shopify Item Link", {"store": cls.store, "item_code": cls.item_code}):
			link = frappe.new_doc("Shopify Item Link")
			link.store = cls.store
			link.item_code = cls.item_code
			link.product_gid = PRODUCT_GID
			link.variant_gid = "gid://shopify/ProductVariant/9001"
			link.insert(ignore_permissions=True)
		frappe.db.commit()

	def _push(self) -> dict:
		"""Run one product row and return the `product` payload that reached Shopify."""
		client = FakeClient({"productUpdate": {"productUpdate": {"product": {"id": PRODUCT_GID}}}})
		with patch.object(product_module.ShopifyClient, "for_store", return_value=client):
			product_module.push_products(self.store, [{"ref_docname": self.item_code}])
		self.assertEqual(len(client.calls), 1)
		return client.calls[0]["variables"]["product"]

	def _set_titles(self, on: int):
		frappe.db.set_value("Shopify Store", self.store, "sync_item_titles", on)
		frappe.db.commit()
		frappe.clear_document_cache("Shopify Store", self.store)


class TestStorefrontCopyIsLeftAlone(ProductPushTestCase):
	def tearDown(self):
		self._set_titles(0)

	def test_an_ordinary_update_does_not_touch_title_or_description(self):
		"""The regression. Saving an Item pushes what ERPNext owns and nothing else."""
		self._set_titles(0)
		payload = self._push()

		self.assertNotIn("title", payload)
		self.assertNotIn("descriptionHtml", payload)

	def test_it_still_tells_shopify_whether_the_item_is_sellable(self):
		"""Leaving the copy alone must not cost us the one field ERPNext does own."""
		self._set_titles(0)
		self.assertEqual(self._push()["status"], "ACTIVE")

		frappe.db.set_value("Item", self.item_code, "disabled", 1)
		frappe.db.commit()
		try:
			self.assertEqual(self._push()["status"], "ARCHIVED")
		finally:
			frappe.db.set_value("Item", self.item_code, "disabled", 0)
			frappe.db.commit()

	def test_a_merchant_can_opt_in_to_erpnext_owning_the_copy(self):
		"""Some catalogues really are mastered in ERPNext. That is a decision, not a default."""
		self._set_titles(1)
		payload = self._push()

		self.assertEqual(payload["title"], "Plain ERPNext Name")
		self.assertEqual(payload["descriptionHtml"], "Plain ERPNext description")

	def test_the_opt_in_is_off_by_default(self):
		"""A merchant who installs the app and configures nothing keeps their copy."""
		field = frappe.get_meta("Shopify Store").get_field("sync_item_titles")
		self.assertIn(field.default, (None, "", "0"))


class TestItemImageReachesShopify(FrappeTestCase):
	"""Shopify fetches the image itself, so the URL has to be one it can actually reach.

	Three ways that fails quietly, and each would store something wrong as the product photo:
	a private file (Frappe serves those only to a session, so Shopify would save the login
	page), a localhost site (nothing to fetch), and no image at all.
	"""

	def _item(self, image):
		return frappe._dict(
			{"name": "_Test Img", "image": image, "get": lambda k, d=None: image if k == "image" else d}
		)

	def test_an_absolute_url_is_passed_through(self):
		url = "https://cdn.example.com/saree.png"
		self.assertEqual(product_module.item_image_url(self._item(url)), url)

	def test_no_image_is_none(self):
		self.assertIsNone(product_module.item_image_url(self._item("")))
		self.assertIsNone(product_module.item_image_url(self._item(None)))

	def test_a_site_path_becomes_absolute(self):
		with patch.object(product_module, "get_url", return_value="https://shop.example.com"):
			self.assertEqual(
				product_module.item_image_url(self._item("/files/saree.png")),
				"https://shop.example.com/files/saree.png",
			)

	def test_spaces_and_commas_in_the_filename_are_encoded(self):
		"""Real uploads are called things like 'ChatGPT Image Aug 24, 2026, 07_38_56 PM.png'."""
		with patch.object(product_module, "get_url", return_value="https://shop.example.com"):
			url = product_module.item_image_url(self._item("/files/A B, C.png"))
		self.assertNotIn(" ", url)
		self.assertTrue(url.startswith("https://shop.example.com/files/"))

	def test_a_localhost_site_sends_nothing(self):
		"""Shopify cannot fetch from a developer's laptop, and a broken image is worse than none."""
		for base in ("http://localhost:8000", "http://127.0.0.1:8080", "http://mysite.localhost"):
			with patch.object(product_module, "get_url", return_value=base):
				self.assertIsNone(
					product_module.item_image_url(self._item("/files/saree.png")),
					f"{base} was treated as publicly reachable",
				)

	def test_a_private_file_sends_nothing(self):
		"""Frappe serves private files only to a logged-in session; Shopify has none."""
		with (
			patch.object(product_module, "get_url", return_value="https://shop.example.com"),
			patch.object(frappe.db, "exists", return_value=True),
		):
			self.assertIsNone(product_module.item_image_url(self._item("/private/files/x.png")))
