"""Catalogue mapping against a real site (spec §8.7, §16).

Covers the phase-2 entries of the spec's mandatory test list, above all the echo loop:
a product webhook writes an Item, and that Item save must not push back to Shopify.
"""

from __future__ import annotations

import zlib
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.catalogue import mapping
from shopify_integration.sync import engine
from shopify_integration.tests.test_integration import (
	SECRET_A,
	SECRET_B,
	ensure_erpnext_masters,
	make_store,
	with_hsn,
)


def _ident(seed: str) -> int:
	"""Deterministic numeric id from a string, stable across runs.

	Python's hash() is salted per process, so it cannot be used: tests that resume mappings
	across runs would see different GIDs each time.
	"""
	return zlib.crc32(seed.encode()) % 1_000_000


def simple_product(sku="TEE-BASIC", gid=None, handle="tee-basic"):
	"""A product with no real options -- Shopify's placeholder Title/Default Title.

	The GID is derived from the SKU so each test gets its own product. Sharing one GID makes
	resolve_item_code correctly return the first test's item code, which looks like a mapping
	bug but is really a fixture bug.
	"""
	gid = gid or f"gid://shopify/Product/{_ident(sku)}"
	return {
		"id": gid,
		"title": "Basic Tee",
		"handle": handle,
		"description": "A basic tee",
		"status": "ACTIVE",
		"productType": "Shirts",
		"vendor": "Acme Apparel",
		"options": [{"name": "Title", "position": 1, "values": ["Default Title"]}],
		"variants": [
			{
				"id": f"gid://shopify/ProductVariant/{_ident(sku)}1",
				"title": "Default Title",
				"sku": sku,
				"price": "19.99",
				"selectedOptions": [{"name": "Title", "value": "Default Title"}],
				# Derived from the SKU, like the variant GID. A hardcoded id here makes two
				# different products share one inventory item, which cannot happen in Shopify
				# and quietly breaks anything keyed on it.
				"inventoryItem": {
					"id": f"gid://shopify/InventoryItem/{_ident(sku)}",
					# A JSON number, which is what Shopify actually sends. A string here hid a
					# bug that failed every product in a real import.
					"measurement": {"weight": {"value": 250.0, "unit": "GRAMS"}},
				},
			}
		],
	}


def variant_product(gid="gid://shopify/Product/200"):
	"""Two options, three variants -- the template-plus-variants path."""
	return {
		"id": gid,
		"title": "Cotton Tee",
		"handle": "cotton-tee",
		"description": "Soft cotton",
		"status": "ACTIVE",
		"productType": "Shirts",
		"vendor": "Acme Apparel",
		"options": [
			{"name": "Size", "position": 1, "values": ["S", "M"]},
			{"name": "Colour", "position": 2, "values": ["Red"]},
		],
		"variants": [
			{
				"id": "gid://shopify/ProductVariant/201",
				"title": "S / Red",
				"sku": "COT-S-RED",
				"selectedOptions": [{"name": "Size", "value": "S"}, {"name": "Colour", "value": "Red"}],
				"inventoryItem": {"id": "gid://shopify/InventoryItem/2001"},
			},
			{
				"id": "gid://shopify/ProductVariant/202",
				"title": "M / Red",
				"sku": "COT-M-RED",
				"selectedOptions": [{"name": "Size", "value": "M"}, {"name": "Colour", "value": "Red"}],
				"inventoryItem": {"id": "gid://shopify/InventoryItem/2002"},
			},
		],
	}


class TestCatalogueMapping(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_erpnext_masters()
		cls.store_a = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)
		cls.store_b = make_store("Test Store B", "test-b.myshopify.com", SECRET_B)

	def setUp(self):
		# No commit here. FrappeTestCase rolls each test back, and committing would persist
		# the previous test's documents past that rollback.
		frappe.db.delete("Shopify Sync Queue")

	def test_simple_product_creates_one_item_and_one_link(self):
		result = mapping.write_product_mapping(self.store_a, simple_product())

		self.assertTrue(frappe.db.exists("Item", "TEE-BASIC"))
		item = frappe.get_doc("Item", "TEE-BASIC")
		self.assertEqual(item.item_name, "Basic Tee")
		self.assertFalse(item.get("has_variants"))
		self.assertEqual(len(result["links"]), 1)

	def test_simple_product_carries_weight_with_its_uom(self):
		mapping.write_product_mapping(self.store_a, simple_product(sku="TEE-W"))
		item = frappe.get_doc("Item", "TEE-W")
		self.assertEqual(item.weight_per_unit, 250.0)
		self.assertEqual(item.weight_uom, "Gram")

	def test_vendor_becomes_a_supplier(self):
		mapping.write_product_mapping(self.store_a, simple_product(sku="TEE-V"))
		self.assertTrue(frappe.db.exists("Supplier", "Acme Apparel"))

	def test_product_type_becomes_the_item_group(self):
		mapping.write_product_mapping(self.store_a, simple_product(sku="TEE-G"))
		self.assertEqual(frappe.db.get_value("Item", "TEE-G", "item_group"), "Shirts")

	def test_link_caches_the_inventory_item_gid(self):
		"""Not an optimisation detail: without it every inventory push costs an extra call."""
		mapping.write_product_mapping(self.store_a, simple_product(sku="TEE-INV"))
		link = frappe.db.get_value(
			"Shopify Item Link", {"store": self.store_a, "item_code": "TEE-INV"}, "inventory_item_gid"
		)
		self.assertEqual(link, f"gid://shopify/InventoryItem/{_ident('TEE-INV')}")

	def test_multi_option_product_creates_a_template_and_variants(self):
		result = mapping.write_product_mapping(self.store_a, variant_product())

		template = frappe.get_doc("Item", result["template"])
		self.assertTrue(template.has_variants)
		self.assertEqual({row.attribute for row in template.attributes}, {"Size", "Colour"})

		for code in ("COT-S-RED", "COT-M-RED"):
			variant = frappe.get_doc("Item", code)
			self.assertEqual(variant.variant_of, template.name)
		self.assertEqual(len(result["links"]), 2)

	def test_variant_attributes_are_written(self):
		mapping.write_product_mapping(self.store_a, variant_product())
		variant = frappe.get_doc("Item", "COT-S-RED")
		attrs = {row.attribute: row.attribute_value for row in variant.attributes}
		self.assertEqual(attrs, {"Size": "S", "Colour": "Red"})

	def test_reimport_is_idempotent(self):
		"""Re-running a catalogue import must update links, never duplicate them."""
		mapping.write_product_mapping(self.store_a, simple_product(sku="TEE-IDEM"))
		mapping.write_product_mapping(self.store_a, simple_product(sku="TEE-IDEM"))

		links = frappe.get_all("Shopify Item Link", filters={"store": self.store_a, "item_code": "TEE-IDEM"})
		self.assertEqual(len(links), 1)

	def test_too_many_options_fails_clearly(self):
		"""An item with more attributes than Shopify allows cannot round-trip; say so."""
		product = variant_product(gid="gid://shopify/Product/300")
		product["options"] = [{"name": n, "values": ["x"]} for n in ("Size", "Colour", "Material", "Fit")]
		with self.assertRaises(frappe.ValidationError) as caught:
			mapping.write_product_mapping(self.store_a, product)
		self.assertIn("at most", str(caught.exception))

	def test_archived_product_disables_the_item_rather_than_deleting_it(self):
		product = simple_product(sku="TEE-ARCH")
		product["status"] = "ARCHIVED"
		mapping.write_product_mapping(self.store_a, product)
		self.assertEqual(frappe.db.get_value("Item", "TEE-ARCH", "disabled"), 1)

	def test_variant_without_a_sku_gets_a_stable_generated_code(self):
		product = simple_product(sku="", gid="gid://shopify/Product/400", handle="no-sku")
		result = mapping.write_product_mapping(self.store_a, product)
		self.assertTrue(result["item"].startswith("SHOPIFY-"))

	def test_two_stores_map_the_same_sku_independently(self):
		"""Mandatory case: two stores, same SKU, independent mappings (spec §16)."""
		mapping.write_product_mapping(self.store_a, simple_product(sku="SHARED-SKU"))
		mapping.write_product_mapping(
			self.store_b, simple_product(sku="SHARED-SKU", gid="gid://shopify/Product/999")
		)

		links = frappe.get_all(
			"Shopify Item Link", filters={"item_code": "SHARED-SKU"}, fields=["store", "variant_gid"]
		)
		self.assertEqual(len(links), 2)
		self.assertEqual({link.store for link in links}, {self.store_a, self.store_b})


class TestEchoSuppression(FrappeTestCase):
	"""The single most common bug in bidirectional integrations (spec §8.7)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_erpnext_masters()
		cls.store_a = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)
		frappe.db.set_value("Shopify Store", cls.store_a, "sync_items", 1)
		frappe.db.commit()

	def setUp(self):
		# No commit here. FrappeTestCase rolls each test back, and committing would persist
		# the previous test's documents past that rollback.
		frappe.db.delete("Shopify Sync Queue")

	def test_mapping_a_product_does_not_enqueue_a_push_back(self):
		"""Shopify -> ERPNext must not become Shopify -> ERPNext -> Shopify."""
		with patch.object(engine, "schedule_drain"):
			mapping.write_product_mapping(self.store_a, simple_product(sku="ECHO-1"))

		self.assertEqual(frappe.db.count("Shopify Sync Queue", {"operation": "product"}), 0)

	def test_variant_product_import_does_not_enqueue_either(self):
		"""Templates, variants, attributes and suppliers are all saved during one import."""
		with patch.object(engine, "schedule_drain"):
			mapping.write_product_mapping(self.store_a, variant_product(gid="gid://shopify/Product/500"))

		self.assertEqual(frappe.db.count("Shopify Sync Queue", {"operation": "product"}), 0)

	def test_a_genuine_erpnext_edit_does_enqueue(self):
		"""The suppression must be narrow: a human editing the item still syncs."""
		with patch.object(engine, "schedule_drain"):
			mapping.write_product_mapping(self.store_a, simple_product(sku="ECHO-2"))
			frappe.db.delete("Shopify Sync Queue")

			item = frappe.get_doc("Item", "ECHO-2")
			item.item_name = "Renamed By A Human"
			item.save(ignore_permissions=True)

		rows = frappe.get_all("Shopify Sync Queue", filters={"operation": "product"}, fields=["dedupe_key"])
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].dedupe_key, f"product:{self.store_a}:ECHO-2")

	def test_unlinked_items_never_enqueue(self):
		"""Most items in a large ERPNext catalogue are not sold on Shopify at all."""
		item = frappe.new_doc("Item")
		item.item_code = "NOT-ON-SHOPIFY"
		item.item_group = mapping.default_item_group()
		item.stock_uom = "Nos"
		item.item_name = "Internal Part"
		with_hsn(item)
		with patch.object(engine, "schedule_drain"):
			item.insert(ignore_permissions=True)

		self.assertEqual(frappe.db.count("Shopify Sync Queue", {"ref_docname": "NOT-ON-SHOPIFY"}), 0)

	def test_template_items_never_enqueue(self):
		"""A template is not sellable and has no Shopify variant of its own."""
		with patch.object(engine, "schedule_drain"):
			result = mapping.write_product_mapping(
				self.store_a, variant_product(gid="gid://shopify/Product/600")
			)
			frappe.db.delete("Shopify Sync Queue")

			template = frappe.get_doc("Item", result["template"])
			template.description = "Edited template"
			template.save(ignore_permissions=True)

		self.assertEqual(frappe.db.count("Shopify Sync Queue", {"ref_docname": result["template"]}), 0)


class TestRealWorldPayloadShapes(FrappeTestCase):
	"""Shapes taken from a live Shopify store, which differ from hand-written fixtures."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_erpnext_masters()
		cls.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)

	def test_numeric_weight_is_accepted(self):
		"""Shopify sends measurement.weight.value as a JSON number. Parsing it with the money
		parser, which refuses floats by design, failed every product in a real import."""
		product = simple_product(sku="WEIGHT-NUM")
		product["variants"][0]["inventoryItem"]["measurement"]["weight"] = {
			"value": 1.5,
			"unit": "KILOGRAMS",
		}
		mapping.write_product_mapping(self.store, product)

		item = frappe.get_doc("Item", "WEIGHT-NUM")
		self.assertEqual(item.weight_per_unit, 1.5)
		self.assertEqual(item.weight_uom, "Kg")

	def test_zero_weight_is_accepted(self):
		"""Most real products have no weight set, and Shopify reports that as 0.0."""
		product = simple_product(sku="WEIGHT-ZERO")
		product["variants"][0]["inventoryItem"]["measurement"]["weight"] = {
			"value": 0.0,
			"unit": "GRAMS",
		}
		mapping.write_product_mapping(self.store, product)
		self.assertEqual(frappe.db.get_value("Item", "WEIGHT-ZERO", "weight_per_unit"), 0.0)

	def test_missing_measurement_is_tolerated(self):
		product = simple_product(sku="WEIGHT-NONE")
		product["variants"][0]["inventoryItem"].pop("measurement", None)
		mapping.write_product_mapping(self.store, product)
		self.assertTrue(frappe.db.exists("Item", "WEIGHT-NONE"))


class TestItemGroupRoot(FrappeTestCase):
	"""The tree root is found, never recreated.

	Whether a site's root Item Group has a NULL parent or an empty-string one depends on how
	the site was made. A filter of ``["in", ("", None)]`` looks like it covers both, but in SQL
	``IN ('', NULL)`` never matches a NULL -- so on a NULL-parent site the root was invisible
	here and a second "All Item Groups" was inserted, failing on the primary key and taking
	every product import down with it.
	"""

	def test_the_root_is_found_when_its_parent_is_null(self):
		root = mapping.root_item_group()
		frappe.db.sql("UPDATE `tabItem Group` SET parent_item_group = NULL WHERE name = %s", root)
		try:
			self.assertEqual(mapping.root_item_group(), root)
		finally:
			frappe.db.sql("UPDATE `tabItem Group` SET parent_item_group = '' WHERE name = %s", root)

	def test_the_root_is_found_when_its_parent_is_empty(self):
		root = mapping.root_item_group()
		frappe.db.sql("UPDATE `tabItem Group` SET parent_item_group = '' WHERE name = %s", root)
		self.assertEqual(mapping.root_item_group(), root)

	def test_it_does_not_create_a_second_root(self):
		before = frappe.db.count("Item Group", {"is_group": 1})
		mapping.root_item_group()
		mapping.root_item_group()
		self.assertEqual(frappe.db.count("Item Group", {"is_group": 1}), before)


class TestPublishingDoesNotDuplicateTheTemplate(FrappeTestCase):
	"""The echo of our own `products/create` must reuse the template, not make a twin.

	Publishing a variant template from ERPNext makes Shopify fire `products/create` straight
	back. The inbound writer named the ERPNext template after the Shopify *handle*, so
	`KURTA-SET` gained a twin called `chikankari-kurta`, the six variant links were rewired to
	the twin, and the run then died on the unique (store, item_code) constraint.

	The quiet part is what mattered: `_stores_for` finds a published template through
	`template_item`, so once the links pointed at the handle no new size could attach. The
	range looked published and silently stopped accepting variants.
	"""

	def setUp(self):
		self.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)
		self.template = "_Test Erp Template"
		self.variant = "_Test Erp Template-S"
		self.gid = "gid://shopify/Product/77001"
		frappe.db.delete("Shopify Item Link", {"store": self.store})
		frappe.db.commit()
		# variants first, then the templates they point at
		for code in (
			self.variant,
			"_Test Brand New-S",
			self.template,
			"erp-template-handle",
			"brand-new-handle",
		):
			if frappe.db.exists("Item", code):
				frappe.delete_doc("Item", code, force=True, ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		frappe.db.delete("Shopify Item Link", {"store": self.store})
		frappe.db.commit()
		# variants first, then the templates they point at
		for code in (
			self.variant,
			"_Test Brand New-S",
			self.template,
			"erp-template-handle",
			"brand-new-handle",
		):
			if frappe.db.exists("Item", code):
				frappe.delete_doc("Item", code, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _erpnext_template_and_variant(self):
		grp = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		t = frappe.new_doc("Item")
		t.item_code = self.template
		t.item_name = "Erp Template"
		t.item_group = grp
		t.stock_uom = "Nos"
		t.has_variants = 1
		t.append("attributes", {"attribute": "Size"})
		t.append("attributes", {"attribute": "Colour"})
		with_hsn(t)
		t.insert(ignore_permissions=True)

		v = frappe.new_doc("Item")
		v.item_code = self.variant
		v.item_name = "Erp Template S"
		v.item_group = grp
		v.stock_uom = "Nos"
		v.variant_of = self.template
		v.append("attributes", {"attribute": "Size", "attribute_value": "Small"})
		v.append("attributes", {"attribute": "Colour", "attribute_value": "White"})
		with_hsn(v)
		with patch.object(engine, "schedule_drain"):
			v.insert(ignore_permissions=True)

		link = frappe.new_doc("Shopify Item Link")
		link.store = self.store
		link.item_code = self.variant
		link.template_item = self.template
		link.is_variant = 1
		link.product_gid = self.gid
		link.variant_gid = "gid://shopify/ProductVariant/77001"
		link.insert(ignore_permissions=True)
		frappe.db.commit()

	def _echo_from_shopify(self):
		"""What Shopify sends back after we publish -- note the handle differs from the code."""
		return {
			"id": self.gid,
			"handle": "erp-template-handle",
			"title": "Erp Template",
			"status": "ACTIVE",
			"productType": "",
			"options": [
				{"name": "Size", "position": 1, "values": ["Small"]},
				{"name": "Colour", "position": 2, "values": ["White"]},
			],
			"variants": [
				{
					"id": "gid://shopify/ProductVariant/77001",
					"title": "Small / White",
					"sku": self.variant,
					"selectedOptions": [
						{"name": "Size", "value": "Small"},
						{"name": "Colour", "value": "White"},
					],
					"inventoryItem": {"id": "gid://shopify/InventoryItem/77001"},
				}
			],
		}

	def test_the_echo_reuses_the_erpnext_template(self):
		self._erpnext_template_and_variant()
		mapping.write_product_mapping(self.store, self._echo_from_shopify())
		frappe.db.commit()

		self.assertFalse(
			frappe.db.exists("Item", "erp-template-handle"),
			"the Shopify handle became a second template -- the original is now orphaned",
		)
		self.assertEqual(
			frappe.db.get_value(
				"Shopify Item Link", {"store": self.store, "item_code": self.variant}, "template_item"
			),
			self.template,
			"the variant link was rewired away from the ERPNext template, so new sizes cannot attach",
		)

	def test_a_product_we_did_not_publish_still_uses_its_handle(self):
		"""Only an already-linked product reuses a template. A genuinely new one is named normally."""
		product = self._echo_from_shopify()
		product["id"] = "gid://shopify/Product/77999"
		product["handle"] = "brand-new-handle"
		product["variants"][0]["id"] = "gid://shopify/ProductVariant/77999"
		product["variants"][0]["sku"] = "_Test Brand New-S"
		product["variants"][0]["inventoryItem"]["id"] = "gid://shopify/InventoryItem/77999"
		try:
			mapping.write_product_mapping(self.store, product)
			frappe.db.commit()
			self.assertTrue(frappe.db.exists("Item", "brand-new-handle"))
		finally:
			frappe.db.delete("Shopify Item Link", {"store": self.store})
			frappe.db.commit()
			# the variant must go before the template it points at
			for code in ("_Test Brand New-S", "brand-new-handle"):
				if frappe.db.exists("Item", code):
					frappe.delete_doc("Item", code, force=True, ignore_permissions=True)
			frappe.db.commit()


class TestATemplateSavedBeforeItsVariants(FrappeTestCase):
	"""Saving the template first and adding sizes afterwards must still publish.

	That is the order anyone works in: make the template, save, then add the sizes. A worker
	draining between the two steps found a template with no variants, correctly declined it --
	"nothing to sell" -- and marked the queue row Done. The publish decision was then spent.

	Nothing rescued it. Variants carry no `publish_to_shopify` of their own, and `_stores_for`
	reaches a store through the template only once the template already has a link. Before the
	first publish there is none, so the product never appeared and nothing was logged as wrong.
	"""

	def setUp(self):
		self.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)
		frappe.db.set_value(
			"Shopify Store", self.store, {"sync_items": 1, "publish_new_items": 1}, update_modified=False
		)
		frappe.db.commit()
		frappe.clear_document_cache("Shopify Store", self.store)
		self.template = "_Test Tpl Before Variants"
		self.variant = f"{self.template}-S"
		for code in (self.variant, self.template):
			if frappe.db.exists("Item", code):
				frappe.delete_doc("Item", code, force=True, ignore_permissions=True)
		frappe.db.delete("Shopify Sync Queue", {"store": self.store})
		frappe.db.commit()

	def tearDown(self):
		for code in (self.variant, self.template):
			if frappe.db.exists("Item", code):
				frappe.delete_doc("Item", code, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _template(self):
		item = frappe.new_doc("Item")
		item.item_code = self.template
		item.item_name = self.template
		item.item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		item.stock_uom = "Nos"
		item.has_variants = 1
		item.publish_to_shopify = 1
		item.append("attributes", {"attribute": "Size"})
		with_hsn(item)
		item.insert(ignore_permissions=True)
		return item

	def _queued_for(self, item_code):
		return frappe.db.count(
			"Shopify Sync Queue",
			{"store": self.store, "operation": "product", "ref_docname": item_code},
		)

	def test_adding_a_variant_re_queues_the_template(self):
		self._template()
		frappe.db.delete("Shopify Sync Queue", {"store": self.store})  # the worker already drained it
		frappe.db.commit()
		self.assertEqual(self._queued_for(self.template), 0)

		variant = frappe.new_doc("Item")
		variant.item_code = self.variant
		variant.item_name = self.variant
		variant.item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		variant.stock_uom = "Nos"
		variant.variant_of = self.template
		variant.append("attributes", {"attribute": "Size", "attribute_value": "Small"})
		with_hsn(variant)
		with patch.object(engine, "schedule_drain"):
			variant.insert(ignore_permissions=True)

		self.assertEqual(
			self._queued_for(self.template),
			1,
			"adding a size did not re-queue the template -- the product will never publish",
		)

	def test_the_template_is_not_re_queued_once_it_is_published(self):
		"""Only a template still waiting needs rescuing. A published one is already linked."""
		self._template()
		# the first size, already published -- this is what "already linked" means
		first = frappe.new_doc("Item")
		first.item_code = self.variant
		first.item_name = self.variant
		first.item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		first.stock_uom = "Nos"
		first.variant_of = self.template
		first.append("attributes", {"attribute": "Size", "attribute_value": "Small"})
		with_hsn(first)
		with patch.object(engine, "schedule_drain"):
			first.insert(ignore_permissions=True)

		link = frappe.new_doc("Shopify Item Link")
		link.store = self.store
		link.item_code = self.variant
		link.template_item = self.template
		link.is_variant = 1
		link.product_gid = "gid://shopify/Product/55501"
		link.variant_gid = "gid://shopify/ProductVariant/55501"
		link.insert(ignore_permissions=True)
		frappe.db.delete("Shopify Sync Queue", {"store": self.store})
		frappe.db.commit()

		second = frappe.new_doc("Item")
		second.item_code = f"{self.template}-M"
		second.item_name = f"{self.template}-M"
		second.item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		second.stock_uom = "Nos"
		second.variant_of = self.template
		second.append("attributes", {"attribute": "Size", "attribute_value": "Medium"})
		with_hsn(second)
		try:
			with patch.object(engine, "schedule_drain"):
				second.insert(ignore_permissions=True)
			# the new size goes out on its own; the template is not pushed again
			self.assertEqual(self._queued_for(self.template), 0)
			self.assertEqual(self._queued_for(second.item_code), 1)
		finally:
			frappe.delete_doc("Item", second.item_code, force=True, ignore_permissions=True)
			frappe.delete_doc("Shopify Item Link", link.name, force=True, ignore_permissions=True)
			frappe.db.commit()


class TestPublishingFromERPNext(FrappeTestCase):
	"""Creating a Shopify product from an ERPNext item.

	Two switches, both off by default. An ERPNext catalogue is mostly raw materials, packaging
	and internal parts; a single careless default would put all of it in a live shop, and
	unpublishing thousands of products by hand is not a recoverable mistake.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)

	def setUp(self):
		frappe.db.set_value("Shopify Store", self.store, "publish_new_items", 0)
		frappe.db.commit()
		frappe.clear_document_cache("Shopify Store", self.store)

	tearDown = setUp

	def _item(self, publish, has_variants=0):
		code = f"PUB-{frappe.generate_hash(length=6)}"
		item = frappe.new_doc("Item")
		item.item_code = code
		item.item_name = "Publishable Thing"
		item.item_group = mapping.default_item_group()
		item.stock_uom = "Nos"
		item.is_stock_item = 0 if has_variants else 1
		item.has_variants = has_variants
		if has_variants:
			# ERPNext insists a template declares what varies
			item.append(
				"attributes",
				{"attribute": mapping.ensure_item_attribute({"name": "Size", "values": ["M"]})},
			)
		item.publish_to_shopify = publish
		with_hsn(item)
		with patch.object(engine, "schedule_drain"):
			item.insert(ignore_permissions=True)
		frappe.db.commit()
		return item

	def _publishes(self, item_code):
		from shopify_integration.outbound import product as product_module

		return product_module._should_publish(self.store, item_code)

	def test_nothing_publishes_while_the_store_switch_is_off(self):
		item = self._item(publish=1)
		self.assertFalse(
			self._publishes(item.name),
			"the item asked to be published, but no store has agreed to accept new items",
		)

	def test_nothing_publishes_while_the_item_switch_is_off(self):
		frappe.db.set_value("Shopify Store", self.store, "publish_new_items", 1)
		frappe.db.commit()
		item = self._item(publish=0)
		self.assertFalse(
			self._publishes(item.name),
			"the store accepts new items, but this one was never offered",
		)

	def test_it_publishes_only_when_both_are_on(self):
		frappe.db.set_value("Shopify Store", self.store, "publish_new_items", 1)
		frappe.db.commit()
		item = self._item(publish=1)
		self.assertTrue(self._publishes(item.name))

	def test_an_opted_in_item_reaches_the_queue(self):
		from shopify_integration.outbound import product as product_module

		frappe.db.set_value("Shopify Store", self.store, {"publish_new_items": 1, "sync_items": 1})
		frappe.db.commit()
		frappe.clear_document_cache("Shopify Store", self.store)

		item = self._item(publish=1)
		frappe.db.delete("Shopify Sync Queue", {"ref_docname": item.name})
		frappe.db.commit()

		with patch.object(engine, "schedule_drain"):
			product_module.on_item_change(item)

		self.assertEqual(
			frappe.db.count(
				"Shopify Sync Queue",
				{"store": self.store, "operation": "product", "ref_docname": item.name},
			),
			1,
			"an unlinked item that asked to be published has to reach the queue, or nothing "
			"will ever create it in Shopify",
		)

	def test_an_item_with_variants_is_not_published_half_formed(self):
		"""It needs Shopify options built to match, which this version does not create."""
		from shopify_integration.outbound import product as product_module

		frappe.db.set_value("Shopify Store", self.store, "publish_new_items", 1)
		frappe.db.commit()
		item = self._item(publish=1, has_variants=1)

		calls = []

		class Client:
			def execute(self, query, variables, cost_hint=0):
				calls.append(variables)
				return {}

		self.assertIsNone(product_module._create_product(Client(), self.store, item.name))
		self.assertEqual(calls, [], "nothing should have been sent to Shopify")


class TestPublishingVariants(FrappeTestCase):
	"""A template item becomes one Shopify product with options and a variant per child."""

	def _rows(self, *pairs):
		return [frappe._dict({"attribute": a, "attribute_value": v}) for a, v in pairs]

	def test_options_come_from_the_variants_that_exist(self):
		"""Not from the Item Attribute's own list. An attribute may hold every size a business
		has ever stocked; a shop selling two of them should offer two."""
		from shopify_integration.outbound import product as product_module

		children = [
			{"item_code": "K-S-IND", "values": self._rows(("Size", "S"), ("Colour", "Indigo"))},
			{"item_code": "K-M-IND", "values": self._rows(("Size", "M"), ("Colour", "Indigo"))},
		]
		options = product_module._options_from(children)

		self.assertEqual([o["name"] for o in options], ["Size", "Colour"])
		self.assertEqual([v["name"] for v in options[0]["values"]], ["S", "M"])
		self.assertEqual(
			[v["name"] for v in options[1]["values"]],
			["Indigo"],
			"a colour used by both variants is one option value, not two",
		)

	def test_option_order_follows_the_attribute_order(self):
		from shopify_integration.outbound import product as product_module

		children = [{"item_code": "K", "values": self._rows(("Colour", "Red"), ("Size", "S"))}]
		options = product_module._options_from(children)

		self.assertEqual([o["name"] for o in options], ["Colour", "Size"])
		self.assertEqual([o["position"] for o in options], [1, 2])

	def test_blank_attribute_values_are_left_out(self):
		from shopify_integration.outbound import product as product_module

		children = [{"item_code": "K", "values": self._rows(("Size", "S"), ("", "x"), ("Colour", ""))}]
		options = product_module._options_from(children)

		self.assertEqual([o["name"] for o in options], ["Size"])

	def test_a_template_with_no_variants_publishes_nothing(self):
		"""There is nothing to sell, and Shopify would reject a product with options and no
		variants for them."""
		from shopify_integration.outbound import product as product_module

		calls = []

		class Client:
			def execute(self, query, variables, cost_hint=0):
				calls.append(variables)
				return {}

		store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)
		code = f"EMPTY-TPL-{frappe.generate_hash(length=6)}"
		item = frappe.new_doc("Item")
		item.item_code = code
		item.item_name = "Template with no children"
		item.item_group = mapping.default_item_group()
		item.stock_uom = "Nos"
		item.is_stock_item = 0
		item.has_variants = 1
		item.append(
			"attributes",
			{"attribute": mapping.ensure_item_attribute({"name": "Size", "values": ["M"]})},
		)
		with_hsn(item)
		with patch.object(engine, "schedule_drain"):
			item.insert(ignore_permissions=True)
		frappe.db.commit()

		self.assertIsNone(product_module._create_product(Client(), store, code))
		self.assertEqual(calls, [], "nothing should have been sent to Shopify")

	def test_a_new_variant_inherits_the_templates_decision(self):
		"""Nobody ticks a box per size. The template already said this product belongs in the
		shop, and a size added to it is part of that product."""
		from shopify_integration.outbound import product as product_module

		store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)
		frappe.db.set_value("Shopify Store", store, "sync_items", 1)
		frappe.db.commit()

		# Unique per run: (store, variant_gid) is a unique index, and this row is committed, so
		# a fixed id passes once and collides on every run after.
		unique = frappe.generate_hash(length=8)
		template = f"TPL-{unique}"
		sibling = f"{template}-S"
		link = frappe.new_doc("Shopify Item Link")
		link.store = store
		link.item_code = sibling
		link.template_item = template
		link.is_variant = 1
		link.product_gid = f"gid://shopify/Product/{unique}"
		link.variant_gid = f"gid://shopify/ProductVariant/{unique}"
		# The items themselves are beside the point here -- what is under test is whether a
		# published template is found through one of its children.
		link.flags.ignore_links = True
		link.insert(ignore_permissions=True, ignore_links=True)
		frappe.db.commit()
		self.addCleanup(lambda: _drop_link(link.name))

		fresh = frappe._dict({"name": f"{template}-M", "variant_of": template, "publish_to_shopify": 0})

		self.assertIn(
			store,
			product_module._stores_for(fresh),
			"a new variant of a published range has to reach the queue, or it sits unlinked for "
			"ever -- the template is already published, so nothing takes the create path again",
		)


def _drop_link(name: str) -> None:
	"""Committed fixtures outlive FrappeTestCase's rollback and have to be cleared by hand."""
	frappe.db.delete("Shopify Item Link", {"name": name})
	frappe.db.commit()
