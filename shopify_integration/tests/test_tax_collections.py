"""Products belong in the Shopify collection whose tax override matches ERPNext's own.

Shopify charges tax by collection override; ERPNext decides tax by Item Tax Template, which
on a banded catalogue depends on the price -- a kurti at 2,450 is 5% and the same kurti at
2,550 is 18%. Nothing joined those two facts, so the books and the checkout could disagree.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.outbound.collections import (
	resolve_item_tax_template,
	sync_item_collections,
	target_collection,
	tax_collection_map,
)

FIVE = "Shopify Output Tax CGST 2.5% + Output Tax SGST 2.5% - VCJ"
EIGHTEEN = "Shopify Output Tax CGST 9% + Output Tax SGST 9% - VCJ"
COLL_5 = "gid://shopify/Collection/5"
COLL_18 = "gid://shopify/Collection/18"


class _Client:
	"""Records the membership change, and reports whatever collections we tell it to."""

	def __init__(self, current=()):
		self.current = list(current)
		self.updates = []

	def execute(self, query, variables, cost_hint=0):
		if "productCollections($id" in query or "product(id: $id)" in query:
			return {
				"product": {"collections": {"nodes": [{"id": gid, "title": gid} for gid in self.current]}}
			}
		self.updates.append(variables["product"])
		return {"productUpdate": {"product": {"id": variables["product"]["id"]}, "userErrors": []}}


def store(rows=((FIVE, COLL_5), (EIGHTEEN, COLL_18)), company=None):
	return frappe._dict(
		{
			"name": "Test Store A",
			"company": company or frappe.get_all("Company", limit=1, pluck="name")[0],
			"selling_price_list": "Standard Selling",
			"tax_collection_map": [
				frappe._dict({"item_tax_template": template, "collection_gid": gid}) for template, gid in rows
			],
		}
	)


class TestTheMapIsOptional(FrappeTestCase):
	def test_an_empty_map_means_the_feature_is_off(self):
		"""Nothing here may change behaviour for a store that has not asked for it."""
		self.assertEqual(tax_collection_map(store(rows=())), {})
		self.assertEqual(target_collection(store(rows=()), "anything"), (None, None))

	def test_blank_rows_are_ignored(self):
		half_filled = store(rows=((FIVE, ""), ("", COLL_18)))
		self.assertEqual(tax_collection_map(half_filled), {})

	def test_an_empty_map_does_not_call_shopify(self):
		client = _Client()
		sync_item_collections(client, store(rows=()), "ZZ-TAX-A")
		self.assertEqual(client.updates, [])


class _BandedItem(FrappeTestCase):
	"""An item with a 5% band up to 2,500 and an 18% band above it."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.company = (
			frappe.db.get_value("Shopify Store", "Test Store A", "company")
			or (frappe.get_all("Company", limit=1, pluck="name")[0])
		)
		cls.available = frappe.db.exists("Item Tax Template", FIVE) and frappe.db.exists(
			"Item Tax Template", EIGHTEEN
		)

	def setUp(self):
		if not self.available:
			self.skipTest("this site has no banded GST templates to test against")
		self.made = []

	def tearDown(self):
		for doctype, name in reversed(self.made):
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_missing=True)
		frappe.db.commit()

	def _item(self, code, banded=True, template=None):
		from shopify_integration.tests.test_integration import with_hsn

		frappe.delete_doc("Item", code, force=True, ignore_permissions=True, ignore_missing=True)
		doc = frappe.new_doc("Item")
		doc.item_code = code
		doc.item_name = code
		doc.item_group = frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0]
		doc.stock_uom = "Nos"
		if template:
			doc.variant_of = template
		if banded:
			doc.append("taxes", {"item_tax_template": FIVE, "minimum_net_rate": 0, "maximum_net_rate": 2500})
			doc.append(
				"taxes",
				{"item_tax_template": EIGHTEEN, "minimum_net_rate": 2500.01, "maximum_net_rate": 999999},
			)
		with_hsn(doc)
		doc.insert(ignore_permissions=True)
		self.made.append(("Item", code))
		frappe.db.commit()
		return code

	# The price is injected rather than written, so these stay about the banding.
	def _at(self, price):
		return patch("shopify_integration.outbound.collections.current_price", return_value=price)


class TestResolvingTheBand(_BandedItem):
	def test_a_price_inside_the_lower_band_resolves_to_five_percent(self):
		code = self._item("ZZ-TAX-KURTI")
		with self._at(2450):
			self.assertEqual(resolve_item_tax_template(store(), code), FIVE)

	def test_a_price_above_the_band_resolves_to_eighteen(self):
		code = self._item("ZZ-TAX-KURTI")
		with self._at(2550):
			self.assertEqual(resolve_item_tax_template(store(), code), EIGHTEEN)

	def test_the_boundary_belongs_to_the_lower_band(self):
		code = self._item("ZZ-TAX-KURTI")
		with self._at(2500):
			self.assertEqual(resolve_item_tax_template(store(), code), FIVE)

	def test_an_item_with_no_tax_rows_resolves_to_nothing_of_its_own(self):
		code = self._item("ZZ-TAX-PLAIN", banded=False)
		with self._at(2450):
			# Whatever its Item Group says, which on a bare test item is nothing.
			self.assertNotEqual(resolve_item_tax_template(store(), code), EIGHTEEN)

	def test_an_item_that_does_not_exist_resolves_to_nothing(self):
		self.assertIsNone(resolve_item_tax_template(store(), "ZZ-NOT-AN-ITEM"))


class TestChoosingTheCollection(_BandedItem):
	def test_the_price_decides_which_collection(self):
		code = self._item("ZZ-TAX-KURTI")
		with self._at(2450):
			self.assertEqual(target_collection(store(), code), (COLL_5, None))
		with self._at(2550):
			self.assertEqual(target_collection(store(), code), (COLL_18, None))

	def test_a_template_that_maps_to_nothing_means_no_collection(self):
		"""The store's default rate needs no override, so this is an answer, not a gap."""
		code = self._item("ZZ-TAX-KURTI")
		only_five = store(rows=((FIVE, COLL_5),))
		with self._at(2550):
			self.assertEqual(target_collection(only_five, code), (None, None))


class TestMovingTheProduct(_BandedItem):
	def _sync(self, client, code, price):
		link = frappe._dict({"product_gid": "gid://shopify/Product/1"})
		with (
			self._at(price),
			patch("shopify_integration.outbound.product.product_link_for", return_value=link),
		):
			return sync_item_collections(client, store(), code)

	def test_a_product_is_added_to_the_matching_collection(self):
		code = self._item("ZZ-TAX-KURTI")
		client = _Client(current=[])
		result = self._sync(client, code, 2450)

		self.assertEqual(result["added"], [COLL_5])
		self.assertEqual(client.updates[0]["collectionsToJoin"], [COLL_5])

	def test_crossing_the_band_moves_it_across(self):
		"""Up and then back down: the move has to work both ways."""
		code = self._item("ZZ-TAX-KURTI")

		up = _Client(current=[COLL_5])
		self._sync(up, code, 2550)
		self.assertEqual(up.updates[0]["collectionsToJoin"], [COLL_18])
		self.assertEqual(up.updates[0]["collectionsToLeave"], [COLL_5])

		down = _Client(current=[COLL_18])
		self._sync(down, code, 2450)
		self.assertEqual(down.updates[0]["collectionsToJoin"], [COLL_5])
		self.assertEqual(down.updates[0]["collectionsToLeave"], [COLL_18])

	def test_a_product_already_in_the_right_place_is_not_touched(self):
		code = self._item("ZZ-TAX-KURTI")
		client = _Client(current=[COLL_5])
		result = self._sync(client, code, 2450)

		self.assertEqual((result["added"], result["removed"]), ([], []))
		self.assertEqual(client.updates, [], "no call at all when nothing needs to change")

	def test_collections_nobody_mapped_are_left_alone(self):
		"""Seasonal collections, manual curation -- none of it is ours to edit."""
		code = self._item("ZZ-TAX-KURTI")
		theirs = "gid://shopify/Collection/summer-sale"
		client = _Client(current=[theirs, COLL_18])
		self._sync(client, code, 2450)

		self.assertEqual(client.updates[0]["collectionsToLeave"], [COLL_18])
		self.assertNotIn(theirs, client.updates[0].get("collectionsToLeave", []))
		self.assertNotIn(theirs, client.updates[0].get("collectionsToJoin", []))

	def test_an_unpublished_item_is_skipped(self):
		code = self._item("ZZ-TAX-KURTI")
		client = _Client()
		with (
			self._at(2450),
			patch("shopify_integration.outbound.product.product_link_for", return_value=None),
		):
			self.assertEqual(sync_item_collections(client, store(), code), {"skipped": "not published"})
		self.assertEqual(client.updates, [])


class TestVariantsInDifferentBands(FrappeTestCase):
	"""A Shopify product has one collection membership for all its variants.

	The variant relationship is stubbed rather than built: a real ERPNext template needs an
	attribute table, an Item Attribute and matching values on every variant, none of which
	would make these prove anything more about which band a product lands in.
	"""

	TEMPLATE = "ZZ-TAX-TPL"

	def _variants(self, codes):
		return patch("shopify_integration.outbound.collections._variant_codes", return_value=list(codes))

	def _bands(self, mapping):
		return patch(
			"shopify_integration.outbound.collections.resolve_item_tax_template",
			side_effect=lambda store_doc, item_code: mapping.get(item_code),
		)

	def test_a_template_whose_variants_agree_uses_that_band(self):
		with (
			self._variants(["S", "M", "L"]),
			self._bands({"S": FIVE, "M": FIVE, "L": FIVE}),
		):
			self.assertEqual(target_collection(store(), self.TEMPLATE), (COLL_5, None))

	def test_variants_in_different_bands_are_reported_not_guessed(self):
		"""Shopify charges one rate per product, so picking one would overcharge or
		undercharge somebody. Nothing is changed and the clash is named."""
		with self._variants(["S", "L"]), self._bands({"S": FIVE, "L": EIGHTEEN}):
			collection, warning = target_collection(store(), self.TEMPLATE)

		self.assertIsNone(collection, "no guess")
		self.assertIn("different tax bands", warning)
		self.assertIn("S ->", warning)
		self.assertIn("L ->", warning)
		self.assertIn("Split the product", warning)

	def test_a_variant_with_no_template_does_not_count_as_a_clash(self):
		"""One unpriced variant must not stop the rest being placed correctly."""
		with self._variants(["S", "M"]), self._bands({"S": FIVE, "M": None}):
			self.assertEqual(target_collection(store(), self.TEMPLATE), (COLL_5, None))

	def test_a_mixed_template_is_not_moved(self):
		client = _Client(current=[COLL_5])
		link = frappe._dict({"product_gid": "gid://shopify/Product/1"})
		with (
			self._variants(["S", "L"]),
			self._bands({"S": FIVE, "L": EIGHTEEN}),
			patch("shopify_integration.outbound.product.product_link_for", return_value=link),
		):
			result = sync_item_collections(client, store(), self.TEMPLATE)

		self.assertIn("warning", result)
		self.assertEqual(client.updates, [], "a product nobody can price correctly is left alone")


class TestItIsWiredUp(FrappeTestCase):
	def test_the_queue_knows_the_operation(self):
		from shopify_integration.sync.engine import OPERATION_HANDLERS

		self.assertEqual(
			OPERATION_HANDLERS["collection"],
			"shopify_integration.outbound.collections.push_collections",
		)

	def test_every_trigger_reaches_it(self):
		events = frappe.get_hooks("doc_events")

		def handlers(doctype, event):
			found = (events.get(doctype) or {}).get(event) or []
			return found if isinstance(found, list) else [found]

		self.assertIn(
			"shopify_integration.outbound.collections.on_item_change", handlers("Item", "on_update")
		)
		self.assertIn(
			"shopify_integration.outbound.collections.on_price_change",
			handlers("Item Price", "on_change"),
		)
		self.assertIn(
			"shopify_integration.outbound.collections.on_item_group_change",
			handlers("Item Group", "on_update"),
		)

	def test_creating_a_product_queues_a_collection_check(self):
		import inspect

		from shopify_integration.outbound import product as product_module

		self.assertIn("enqueue_collection", inspect.getsource(product_module._create_product_unlocked))

	def test_reconciliation_covers_it(self):
		import inspect

		from shopify_integration.sync import reconcile

		self.assertIn("reconcile_collections", inspect.getsource(reconcile.reconcile_store))


class TestSavingAnItemGroupStaysCheap(FrappeTestCase):
	"""The hook runs inside somebody's save, and a real group is enormous.

	SAREE holds 39,384 items on the live site and the root holds 77,667. Walking them in the
	save at two or three queries each was something like a hundred thousand queries in one
	request, which does not slow the save down so much as end it.
	"""

	def setUp(self):
		self.made = []
		self.group = self._group("ZZ Tax Group")

	def tearDown(self):
		for doctype, name in reversed(self.made):
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_missing=True)
		frappe.db.commit()

	def _group(self, label):
		name = f"{label} {frappe.generate_hash(length=5)}"
		doc = frappe.new_doc("Item Group")
		doc.item_group_name = name
		doc.parent_item_group = frappe.db.get_value("Item Group", {"is_group": 1}, "name")
		doc.is_group = 0
		doc.insert(ignore_permissions=True)
		self.made.append(("Item Group", doc.name))
		frappe.db.commit()
		return doc.name

	def _items(self, count):
		from shopify_integration.tests.test_integration import with_hsn

		for index in range(count):
			code = f"ZZ-GRP-{index}-{frappe.generate_hash(length=4)}"
			doc = frappe.new_doc("Item")
			doc.item_code = code
			doc.item_name = code
			doc.item_group = self.group
			doc.stock_uom = "Nos"
			with_hsn(doc)
			doc.insert(ignore_permissions=True)
			self.made.append(("Item", code))
		frappe.db.commit()

	def _queries_to_save(self):
		"""How many statements one save of the group costs, counting only our hook's."""
		from shopify_integration.outbound import collections as module

		counted = []
		real_sql = frappe.db.sql

		def counting(*args, **kwargs):
			counted.append(1)
			return real_sql(*args, **kwargs)

		doc = frappe.get_doc("Item Group", self.group)
		with (
			patch.object(frappe.db, "sql", side_effect=counting),
			patch.object(module, "_taxes_changed", return_value=True),
			patch.object(module.frappe, "enqueue") as enqueued,
		):
			module.on_item_group_change(doc)

		return len(counted), enqueued.called

	def test_the_cost_does_not_grow_with_the_group(self):
		"""The property that matters: the same work whether the group holds nothing or a
		catalogue. Forty stands in for forty thousand -- what is being asserted is that the
		number does not move, and a number that does not move at 40 does not move at 40,000."""
		with patch("shopify_integration.outbound.collections._any_store_maps_collections", return_value=True):
			empty, _ = self._queries_to_save()
			self._items(40)
			full, enqueued = self._queries_to_save()

		self.assertEqual(
			full, empty, f"saving cost {empty} queries empty and {full} with 40 items in the group"
		)
		self.assertTrue(enqueued, "and the real work goes to a worker")

	def test_nothing_happens_at_all_when_no_store_maps_collections(self):
		"""The common case on every site that does not use this: one query and out."""
		from shopify_integration.outbound import collections as module

		self._items(5)
		doc = frappe.get_doc("Item Group", self.group)
		with (
			patch.object(module, "_any_store_maps_collections", return_value=False),
			patch.object(module.frappe, "enqueue") as enqueued,
			patch.object(module, "_taxes_changed") as changed,
		):
			module.on_item_group_change(doc)

		self.assertFalse(enqueued.called)
		self.assertFalse(changed.called, "it should not even look at what changed")

	def test_a_save_that_did_not_touch_the_taxes_does_nothing(self):
		"""Groups are saved for renames, re-parenting and descriptions. None of those can
		move an item between tax bands."""
		from shopify_integration.outbound import collections as module

		doc = frappe.get_doc("Item Group", self.group)
		with (
			patch.object(module, "_any_store_maps_collections", return_value=True),
			patch.object(module, "_taxes_changed", return_value=False),
			patch.object(module.frappe, "enqueue") as enqueued,
		):
			module.on_item_group_change(doc)

		self.assertFalse(enqueued.called)


class TestSpottingATaxChange(FrappeTestCase):
	from shopify_integration.outbound.collections import _taxes_changed as _changed

	def _group(self, taxes):
		doc = frappe._dict(
			taxes=[frappe._dict(row) for row in taxes],
			get=lambda key, default=None: doc_taxes if key == "taxes" else default,
		)
		doc_taxes = doc.taxes
		doc.get_doc_before_save = lambda: None
		return doc

	def test_a_new_group_with_rules_counts_as_changed(self):
		from shopify_integration.outbound.collections import _taxes_changed

		doc = self._group([{"item_tax_template": FIVE, "minimum_net_rate": 0, "maximum_net_rate": 2500}])
		self.assertTrue(_taxes_changed(doc))

	def test_a_new_group_with_no_rules_is_not_a_change(self):
		from shopify_integration.outbound.collections import _taxes_changed

		self.assertFalse(_taxes_changed(self._group([])))

	def test_an_identical_table_is_not_a_change(self):
		from shopify_integration.outbound.collections import _taxes_changed

		rows = [{"item_tax_template": FIVE, "minimum_net_rate": 0, "maximum_net_rate": 2500}]
		doc = self._group(rows)
		before = self._group(rows)
		doc.get_doc_before_save = lambda: before
		self.assertFalse(_taxes_changed(doc))

	def test_a_moved_band_is_a_change(self):
		from shopify_integration.outbound.collections import _taxes_changed

		doc = self._group([{"item_tax_template": FIVE, "minimum_net_rate": 0, "maximum_net_rate": 3000}])
		before = self._group([{"item_tax_template": FIVE, "minimum_net_rate": 0, "maximum_net_rate": 2500}])
		doc.get_doc_before_save = lambda: before
		self.assertTrue(_taxes_changed(doc))

	def test_a_different_template_is_a_change(self):
		from shopify_integration.outbound.collections import _taxes_changed

		doc = self._group([{"item_tax_template": EIGHTEEN, "minimum_net_rate": 0, "maximum_net_rate": 2500}])
		before = self._group([{"item_tax_template": FIVE, "minimum_net_rate": 0, "maximum_net_rate": 2500}])
		doc.get_doc_before_save = lambda: before
		self.assertTrue(_taxes_changed(doc))
