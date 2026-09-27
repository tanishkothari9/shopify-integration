"""Saving an already-published variant template must not create a second Shopify product.

A template has no Shopify Item Link of its own -- only its variants do, each recording
template_item. push_products looked one up by item_code, found nothing, concluded the
template had never been published, and created a second product. That duplicate came back
through products/create as an unknown product, was imported as two more ERPNext templates,
and took the original's five variant links with it. Seen on STOITEM202001143 (KURTI CRE NET).
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.catalogue.mapping import _template_already_linked
from shopify_integration.outbound.product import (
	_mark_publish_decided,
	_publish_decided,
	product_link_for,
)

PRODUCT = "gid://shopify/Product/15375241674922"


class _LinkCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from shopify_integration.tests.test_integration import SECRET_A, make_store

		cls.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)

	def setUp(self):
		self.made = []

	def tearDown(self):
		for doctype, name in reversed(self.made):
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_missing=True)
		frappe.db.commit()

	def _item(self, code, template=None):
		"""A plain Item standing in for a template or a variant.

		Nothing under test reads has_variants or variant_of -- the relationship lives entirely
		in the link rows' template_item -- and a real ERPNext template needs an attribute
		table, an Item Attribute and matching values, none of which would make these tests
		prove anything more.
		"""
		if frappe.db.exists("Item", code):
			return code
		from shopify_integration.tests.test_integration import with_hsn

		doc = frappe.new_doc("Item")
		doc.item_code = code
		doc.item_name = code
		doc.item_group = frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0]
		doc.stock_uom = "Nos"
		with_hsn(doc)
		doc.insert(ignore_permissions=True)
		self.made.append(("Item", code))
		return code

	def _variant_link(self, item_code, template, variant_gid, product=PRODUCT):
		doc = frappe.new_doc("Shopify Item Link")
		doc.store = self.store
		doc.item_code = item_code
		doc.sku = item_code
		doc.is_variant = 1
		doc.template_item = template
		doc.product_gid = product
		doc.variant_gid = variant_gid
		doc.insert(ignore_permissions=True)
		self.made.append(("Shopify Item Link", doc.name))
		frappe.db.commit()
		return doc.name


class TestFindingAPublishedTemplate(_LinkCase):
	def test_a_template_is_found_through_its_variants(self):
		"""The regression. This returned nothing, and a second product was created."""
		template = self._item("ZZ-TPL-A")
		self._item("ZZ-TPL-A-S", template=template)
		self._variant_link("ZZ-TPL-A-S", template, "gid://shopify/ProductVariant/1")

		found = product_link_for(self.store, template)
		self.assertIsNotNone(found, "a published template must never look unpublished")
		self.assertEqual(found.product_gid, PRODUCT)

	def test_the_same_product_is_found_whichever_variant_carries_the_link(self):
		template = self._item("ZZ-TPL-B")
		for suffix, variant in (("S", "1"), ("M", "2"), ("L", "3")):
			code = self._item(f"ZZ-TPL-B-{suffix}", template=template)
			self._variant_link(code, template, f"gid://shopify/ProductVariant/{variant}")

		self.assertEqual(product_link_for(self.store, template).product_gid, PRODUCT)

	def test_a_plain_item_is_still_found_by_its_own_link(self):
		code = self._item("ZZ-PLAIN-A")
		doc = frappe.new_doc("Shopify Item Link")
		doc.store = self.store
		doc.item_code = code
		doc.product_gid = PRODUCT
		doc.variant_gid = "gid://shopify/ProductVariant/9"
		doc.insert(ignore_permissions=True)
		self.made.append(("Shopify Item Link", doc.name))
		frappe.db.commit()

		self.assertEqual(product_link_for(self.store, code).product_gid, PRODUCT)

	def test_a_template_nobody_has_published_is_still_unpublished(self):
		template = self._item("ZZ-TPL-C")
		self.assertIsNone(product_link_for(self.store, template))

	def test_a_link_returns_its_own_name_so_the_caller_can_use_it(self):
		"""The field list used to leave `name` out, so every caller asking for it got None."""
		code = self._item("ZZ-PLAIN-B")
		doc = frappe.new_doc("Shopify Item Link")
		doc.store = self.store
		doc.item_code = code
		doc.product_gid = PRODUCT
		doc.variant_gid = "gid://shopify/ProductVariant/8"
		doc.insert(ignore_permissions=True)
		self.made.append(("Shopify Item Link", doc.name))
		frappe.db.commit()

		self.assertEqual(product_link_for(self.store, code).name, doc.name)


class TestThePublishDecisionBelongsToTheProduct(_LinkCase):
	def test_a_template_can_answer_it(self):
		"""Keyed by link it was unanswerable for a template, so a DRAFT template that became
		ACTIVE was never put on the Online Store."""
		template = self._item("ZZ-TPL-D")
		self._item("ZZ-TPL-D-S", template=template)
		self._variant_link("ZZ-TPL-D-S", template, "gid://shopify/ProductVariant/4")

		self.assertFalse(_publish_decided(self.store, PRODUCT))
		_mark_publish_decided(self.store, PRODUCT)
		self.assertTrue(_publish_decided(self.store, PRODUCT))

	def test_no_product_means_no_decision(self):
		self.assertFalse(_publish_decided(self.store, None))
		self.assertFalse(_publish_decided(self.store, ""))


class TestRecognisingOurOwnProduct(_LinkCase):
	"""products/create for a product the app just made must not become new ERPNext items."""

	def test_a_product_is_recognised_by_its_gid(self):
		template = self._item("ZZ-TPL-E")
		self._item("ZZ-TPL-E-S", template=template)
		self._variant_link("ZZ-TPL-E-S", template, "gid://shopify/ProductVariant/5")

		self.assertEqual(_template_already_linked(self.store, {"id": PRODUCT, "variants": []}), template)

	def test_a_product_with_no_links_yet_is_recognised_by_its_skus(self):
		"""The duplicate case. The new product has no links of its own, but its SKUs are item
		codes this store already sells, so the range is ours."""
		template = self._item("ZZ-TPL-F")
		self._item("ZZ-TPL-F-S", template=template)
		self._variant_link("ZZ-TPL-F-S", template, "gid://shopify/ProductVariant/6")

		duplicate = {"id": "gid://shopify/Product/15375267496106", "variants": [{"sku": "ZZ-TPL-F-S"}]}
		self.assertEqual(
			_template_already_linked(self.store, duplicate),
			template,
			"a duplicate must reuse the template, not spawn a twin and steal its variants",
		)

	def test_a_genuinely_new_product_is_not_claimed(self):
		self.assertIsNone(
			_template_already_linked(
				self.store,
				{"id": "gid://shopify/Product/999", "variants": [{"sku": "ZZ-NEVER-SEEN"}]},
			)
		)

	def test_skus_spanning_two_templates_are_not_guessed_at(self):
		first = self._item("ZZ-TPL-G")
		second = self._item("ZZ-TPL-H")
		self._item("ZZ-TPL-G-S", template=first)
		self._item("ZZ-TPL-H-S", template=second)
		self._variant_link("ZZ-TPL-G-S", first, "gid://shopify/ProductVariant/7")
		self._variant_link("ZZ-TPL-H-S", second, "gid://shopify/ProductVariant/8")

		self.assertIsNone(
			_template_already_linked(
				self.store,
				{
					"id": "gid://shopify/Product/998",
					"variants": [{"sku": "ZZ-TPL-G-S"}, {"sku": "ZZ-TPL-H-S"}],
				},
			),
			"two ranges in one product is a mess for a person, not a guess for us",
		)


class TestCreationIsHeldBehindALock(FrappeTestCase):
	def test_it_rechecks_inside_the_lock(self):
		"""Two drains claiming the same item would each find no link and each create."""
		import inspect

		from shopify_integration.outbound import product as product_module

		source = inspect.getsource(product_module._create_product)
		self.assertIn("filelock", source)
		self.assertIn("product_link_for", source, "and look again once it holds the lock")
		self.assertIn("_create_product_unlocked", source)
