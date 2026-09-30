"""Shopify's Vendor is not a supplier field, and must not become a Supplier by default.

Left alone Shopify fills Vendor with the shop's own name. On a live store that created a
Supplier called "Smart Choice" and attached it to every product the shop sells to itself --
a real accounting record with a ledger behind it, invented from a display field. Two
products/update webhooks arriving together also raced, and the loser died on the unique name.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.catalogue.mapping import (
	_apply_supplier,
	_is_the_shop_itself,
	ensure_supplier,
)


class _Item:
	"""Enough Item to see whether a supplier row was added."""

	def __init__(self):
		self.supplier_items = []

	def append(self, field, value):
		if field == "supplier_items":
			self.supplier_items.append(frappe._dict(value))


class _StoreCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from shopify_integration.tests.test_integration import SECRET_A, make_store

		cls.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)

	def _setting(self, on: int):
		frappe.db.set_value("Shopify Store", self.store, "create_suppliers_from_vendor", on)
		frappe.db.commit()
		frappe.clear_document_cache("Shopify Store", self.store)
		self.addCleanup(self._reset)

	def _reset(self):
		frappe.db.set_value("Shopify Store", self.store, "create_suppliers_from_vendor", 0)
		frappe.db.commit()
		frappe.clear_document_cache("Shopify Store", self.store)


class TestTheSettingIsOffByDefault(_StoreCase):
	def test_no_supplier_is_created_and_none_is_attached(self):
		"""The regression. Publishing an item created a Supplier named after the shop."""
		frappe.delete_doc(
			"Supplier", "ZZ Some Vendor", force=True, ignore_permissions=True, ignore_missing=True
		)
		frappe.db.commit()

		item = _Item()
		_apply_supplier(item, {"vendor": "ZZ Some Vendor"}, self.store)

		self.assertEqual(item.supplier_items, [], "nothing should be attached")
		self.assertFalse(frappe.db.exists("Supplier", "ZZ Some Vendor"), "and no Supplier invented")

	def test_the_field_defaults_to_off_on_the_doctype(self):
		"""A merchant who configures nothing must not get suppliers out of a display field."""
		field = frappe.get_meta("Shopify Store").get_field("create_suppliers_from_vendor")
		self.assertIsNotNone(field, "the setting should exist")
		self.assertIn(field.default, (None, "", "0"))


class TestWhenAMerchantOptsIn(_StoreCase):
	def tearDown(self):
		# deleted here rather than addCleanup so a failure still tidies up
		for name in ("ZZ Real Vendor",):
			frappe.delete_doc("Supplier", name, force=True, ignore_permissions=True, ignore_missing=True)
		frappe.db.commit()

	def test_a_genuine_vendor_becomes_a_supplier(self):
		self._setting(1)
		item = _Item()
		_apply_supplier(item, {"vendor": "ZZ Real Vendor"}, self.store)

		self.assertTrue(frappe.db.exists("Supplier", "ZZ Real Vendor"))
		self.assertEqual([row.supplier for row in item.supplier_items], ["ZZ Real Vendor"])

	def test_the_shops_own_name_is_still_refused(self):
		"""A product published from ERPNext comes back carrying the shop as its Vendor. That
		is us, not somebody we buy from, whatever the setting says."""
		self._setting(1)
		# Establish the precondition rather than assume it: an earlier run of the old code
		# may well have left this very Supplier behind, and then the test would report a
		# stale record as a live bug.
		frappe.delete_doc("Supplier", self.store, force=True, ignore_permissions=True, ignore_missing=True)
		frappe.db.commit()

		item = _Item()
		_apply_supplier(item, {"vendor": self.store}, self.store)

		self.assertEqual(item.supplier_items, [])
		self.assertFalse(frappe.db.exists("Supplier", self.store))

	def test_an_empty_vendor_does_nothing(self):
		self._setting(1)
		item = _Item()
		_apply_supplier(item, {"vendor": "   "}, self.store)
		_apply_supplier(item, {}, self.store)
		self.assertEqual(item.supplier_items, [])

	def test_the_same_vendor_is_not_attached_twice(self):
		self._setting(1)
		item = _Item()
		_apply_supplier(item, {"vendor": "ZZ Real Vendor"}, self.store)
		_apply_supplier(item, {"vendor": "ZZ Real Vendor"}, self.store)
		self.assertEqual(len(item.supplier_items), 1)


class TestRecognisingTheShopItself(_StoreCase):
	def test_the_store_record_name_counts(self):
		self.assertTrue(_is_the_shop_itself(self.store, self.store))

	def test_case_and_spacing_do_not_hide_it(self):
		self.assertTrue(_is_the_shop_itself(f"  {self.store.upper()} ", self.store))

	def test_the_shop_domain_and_its_subdomain_count(self):
		domain = frappe.db.get_value("Shopify Store", self.store, "shop_domain")
		self.assertTrue(_is_the_shop_itself(domain, self.store))
		self.assertTrue(_is_the_shop_itself(domain.split(".")[0], self.store))

	def test_a_real_vendor_is_not_mistaken_for_the_shop(self):
		self.assertFalse(_is_the_shop_itself("Banarasi Weavers Pvt Ltd", self.store))


class TestTwoWebhooksAtOnce(FrappeTestCase):
	"""ensure_supplier used to die on the unique name when two arrived together."""

	NAME = "ZZ Racing Vendor"

	def tearDown(self):
		frappe.delete_doc("Supplier", self.NAME, force=True, ignore_permissions=True, ignore_missing=True)
		frappe.db.commit()

	def test_losing_the_race_returns_the_winners_supplier(self):
		from unittest.mock import patch

		real_exists = frappe.db.exists

		def missed_it(doctype, *args, **kwargs):
			"""Report the Supplier as absent, as the loser saw it a moment before the insert."""
			if doctype == "Supplier":
				return None
			return real_exists(doctype, *args, **kwargs)

		# the winner
		self.assertEqual(ensure_supplier(self.NAME), self.NAME)
		frappe.db.commit()

		# the loser: looks, sees nothing, inserts, collides
		with patch.object(frappe.db, "exists", side_effect=missed_it):
			self.assertEqual(
				ensure_supplier(self.NAME),
				self.NAME,
				"the name exists, which is all the caller wanted",
			)

	def test_the_ordinary_path_still_creates_one(self):
		self.assertFalse(frappe.db.exists("Supplier", self.NAME))
		self.assertEqual(ensure_supplier(self.NAME), self.NAME)
		self.assertTrue(frappe.db.exists("Supplier", self.NAME))
