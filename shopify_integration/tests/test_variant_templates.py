"""A published range must stay one Shopify product and one ERPNext template.

Publishing SHOPIFY TEST A3 -- three sizes under one template -- produced, within a minute,
a second ERPNext template with no variants, a second Shopify product carrying two of the
three sizes at zero stock, a rewritten Colour and Size attribute, and no photograph.

All of it came from one structural choice: the template had no link of its own, so its
Shopify product could only ever be inferred backwards through its children. Publishing takes
several API calls and the children are linked last, so the echo of our own `products/create`
arrived when nothing at all identified the product as ours.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.api.client import ShopifyClient
from shopify_integration.catalogue.mapping import (
	_template_already_linked,
	ensure_item_attribute,
	write_product_mapping,
)
from shopify_integration.outbound.product import (
	link_template,
	product_link_for,
	repair_split_template,
)

PRODUCT = "gid://shopify/Product/tpl-keep"
OTHER_PRODUCT = "gid://shopify/Product/tpl-duplicate"
COLOUR = "ZZ Shade"
SIZE = "ZZ Length"


class TemplateCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from shopify_integration.tests.test_integration import SECRET_A, make_store

		cls.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)
		frappe.db.commit()

	def setUp(self):
		self.made = []
		self.addCleanup(self._clean)

	def _clean(self):
		for doctype, name in reversed(self.made):
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_missing=True)
		frappe.db.commit()

	# -- fixtures ------------------------------------------------------------------

	def _attribute(self, name: str, values: list[str]) -> str:
		"""Make sure the attribute exists *and* carries these values.

		Topping it up rather than returning early: an attribute left behind by an earlier
		test holds only that test's values, and a variant built on a missing one dies with
		"Attribute Value XL is not valid".
		"""
		if frappe.db.exists("Item Attribute", name):
			doc = frappe.get_doc("Item Attribute", name)
		else:
			doc = frappe.new_doc("Item Attribute")
			doc.attribute_name = name
			self.made.append(("Item Attribute", name))

		present = {row.attribute_value for row in (doc.item_attribute_values or [])}
		taken = {row.abbr for row in (doc.item_attribute_values or [])}
		changed = False
		for value in values:
			if value in present:
				continue
			abbr = f"{value[:2]}{len(taken)}"
			while abbr in taken:
				abbr += "x"
			taken.add(abbr)
			doc.append("item_attribute_values", {"attribute_value": value, "abbr": abbr})
			changed = True

		if changed or doc.is_new():
			doc.save(ignore_permissions=True)
			frappe.db.commit()
		return name

	def _range(self, sizes=("L", "XL", "XXL")) -> tuple[str, list[str]]:
		"""A real ERPNext template with real variants -- `variant_of` is what is under test."""
		from shopify_integration.tests.test_integration import with_hsn

		self._attribute(COLOUR, ["Beige"])
		self._attribute(SIZE, list(sizes))

		code = f"ZZ-TPL-{frappe.generate_hash(length=5).upper()}"
		group = frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0]

		template = frappe.new_doc("Item")
		template.item_code, template.item_name = code, code
		template.item_group, template.stock_uom = group, "Nos"
		template.has_variants = 1
		template.append("attributes", {"attribute": COLOUR})
		template.append("attributes", {"attribute": SIZE})
		with_hsn(template)
		template.flags.ignore_mandatory = True
		template.insert(ignore_permissions=True)
		self.made.append(("Item", code))

		children = []
		for size in sizes:
			child_code = f"{code}-BEI-{size}"
			child = frappe.new_doc("Item")
			child.item_code, child.item_name = child_code, child_code
			child.variant_of, child.item_group, child.stock_uom = code, group, "Nos"
			child.append("attributes", {"attribute": COLOUR, "attribute_value": "Beige"})
			child.append("attributes", {"attribute": SIZE, "attribute_value": size})
			with_hsn(child)
			child.flags.ignore_mandatory = True
			child.insert(ignore_permissions=True)
			self.made.append(("Item", child_code))
			children.append(child_code)

		frappe.db.commit()
		return code, children

	def _payload(self, children: list[str], product=PRODUCT) -> dict:
		"""The product as Shopify announces it back to us, flattened as the handler does."""
		return {
			"id": product,
			"handle": "shopify-test-a3",
			"title": "SHOPIFY TEST A3",
			"description": "",
			"status": "ACTIVE",
			"vendor": "",
			"options": [
				{"name": COLOUR, "values": ["Beige"]},
				{"name": SIZE, "values": [c.rsplit("-", 1)[-1] for c in children]},
			],
			"variants": [
				{
					"id": f"gid://shopify/ProductVariant/{code}",
					"sku": code,
					"title": code.rsplit("-", 1)[-1],
					"selectedOptions": [
						{"name": COLOUR, "value": "Beige"},
						{"name": SIZE, "value": code.rsplit("-", 1)[-1]},
					],
					"inventoryItem": {"id": f"gid://shopify/InventoryItem/{code}", "measurement": {}},
				}
				for code in children
			],
		}

	def _bare_item(self, prefix: str) -> str:
		"""A plain Item, for standing in as the twin template a bad echo invented."""
		from shopify_integration.tests.test_integration import with_hsn

		code = f"{prefix}-{frappe.generate_hash(length=5).upper()}"
		doc = frappe.new_doc("Item")
		doc.item_code, doc.item_name = code, code
		doc.item_group = frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0]
		doc.stock_uom = "Nos"
		with_hsn(doc)
		doc.flags.ignore_mandatory = True
		doc.insert(ignore_permissions=True)
		self.made.append(("Item", code))
		frappe.db.commit()
		return code

	def _link(self, item_code, template, product=PRODUCT, variant=None):
		doc = frappe.new_doc("Shopify Item Link")
		doc.store, doc.item_code = self.store, item_code
		doc.product_gid = product
		doc.variant_gid = variant or f"gid://shopify/ProductVariant/{item_code}"
		doc.is_variant, doc.template_item = 1, template
		doc.insert(ignore_permissions=True)
		self.made.append(("Shopify Item Link", doc.name))
		frappe.db.commit()
		return doc.name


class TestRecognisingOurOwnRange(TemplateCase):
	def test_a_range_with_no_links_at_all_is_still_recognised(self):
		"""The race itself. Links are written last; the echo arrives before them."""
		template, children = self._range()
		self.assertEqual(
			frappe.db.count("Shopify Item Link", {"store": self.store, "item_code": ["in", children]}),
			0,
			"this test is only meaningful with no links",
		)
		self.assertEqual(_template_already_linked(self.store, self._payload(children)), template)

	def test_the_echo_creates_no_second_template(self):
		template, children = self._range()
		before = frappe.db.count("Item")

		result = write_product_mapping(self.store, self._payload(children))

		self.assertEqual(result["template"], template)
		self.assertEqual(
			frappe.db.count("Item"), before, "the echo of our own publish created an ERPNext Item"
		)
		for name in frappe.get_all(
			"Shopify Item Link", filters={"store": self.store, "item_code": ["in", children]}, pluck="name"
		):
			self.made.append(("Shopify Item Link", name))
		self.assertEqual(
			sorted(
				frappe.get_all(
					"Shopify Item Link",
					filters={"store": self.store, "item_code": ["in", children]},
					pluck="template_item",
				)
			),
			[template] * len(children),
			"a variant link was pointed at something other than its real template",
		)

	def test_a_genuinely_new_shopify_range_is_still_imported(self):
		"""The guard must not stop a range this shop really does not have."""
		payload = self._payload(["ZZ-NOTOURS-A", "ZZ-NOTOURS-B"], product="gid://shopify/Product/new")
		result = write_product_mapping(self.store, payload)
		for code in ("ZZ-NOTOURS-A", "ZZ-NOTOURS-B"):
			self.made.append(("Item", code))
		self.made.append(("Item", result["template"]))
		for name in frappe.get_all(
			"Shopify Item Link", filters={"store": self.store, "product_gid": "gid://shopify/Product/new"}, pluck="name"
		):
			self.made.append(("Shopify Item Link", name))
		self.assertTrue(frappe.db.exists("Item", result["template"]))

	def test_skus_we_already_sell_but_cannot_place_are_refused(self):
		"""Items exist, but not as one range. Guessing here is how a catalogue is wrecked."""
		from shopify_integration.tests.test_integration import with_hsn

		group = frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0]
		codes = []
		for suffix in ("P", "Q"):
			code = f"ZZ-ORPHAN-{suffix}-{frappe.generate_hash(length=4).upper()}"
			doc = frappe.new_doc("Item")
			doc.item_code, doc.item_name = code, code
			doc.item_group, doc.stock_uom = group, "Nos"
			with_hsn(doc)
			doc.flags.ignore_mandatory = True
			doc.insert(ignore_permissions=True)
			self.made.append(("Item", code))
			codes.append(code)
		frappe.db.commit()

		with self.assertRaises(frappe.ValidationError):
			write_product_mapping(self.store, self._payload(codes, product="gid://shopify/Product/clash"))


class TestTheTemplateOwnsItsProduct(TemplateCase):
	def test_the_template_gets_a_link_of_its_own(self):
		template, _children = self._range()
		name = link_template(self.store, template, PRODUCT)
		self.made.append(("Shopify Item Link", name))

		row = frappe.db.get_value(
			"Shopify Item Link", name, ["product_gid", "is_template", "variant_gid"], as_dict=True
		)
		self.assertEqual(row.product_gid, PRODUCT)
		self.assertEqual(row.is_template, 1)
		self.assertIsNone(row.variant_gid, "a template has no Shopify variant of its own")

	def test_writing_it_twice_is_the_same_row(self):
		template, _children = self._range()
		first = link_template(self.store, template, PRODUCT)
		second = link_template(self.store, template, PRODUCT)
		self.made.append(("Shopify Item Link", first))
		self.assertEqual(first, second)

	def test_a_corrupted_child_no_longer_detaches_the_range(self):
		"""What the 3 October echo did: it pulled one child onto a twin template."""
		template, children = self._range()
		self.made.append(("Shopify Item Link", link_template(self.store, template, PRODUCT)))
		self._link(children[0], template=self._bare_item("ZZ-TWIN"), product=OTHER_PRODUCT)

		found = product_link_for(self.store, template)
		self.assertIsNotNone(found)
		self.assertEqual(
			found.product_gid, PRODUCT, "the range was lost because one child's link was wrong"
		)

	def test_without_a_template_row_it_still_falls_back_to_the_children(self):
		"""Sites upgraded mid-flight have the children and not yet the template row."""
		template, children = self._range()
		self._link(children[0], template=template, product=PRODUCT)
		self.assertEqual(product_link_for(self.store, template).product_gid, PRODUCT)


class TestSharedAttributesAreLeftAlone(TemplateCase):
	def test_an_attribute_is_not_rewritten_when_nothing_is_new(self):
		name = self._attribute(COLOUR, ["Beige"])
		before = frappe.db.get_value("Item Attribute", name, "modified")

		ensure_item_attribute({"name": name, "values": ["Beige"]})

		self.assertEqual(
			frappe.db.get_value("Item Attribute", name, "modified"),
			before,
			"an echo rewrote a document every variant product in the catalogue shares",
		)

	def test_a_genuinely_new_value_is_still_added(self):
		name = self._attribute(SIZE, ["L"])
		ensure_item_attribute({"name": name, "values": ["L", "XXXL"]})
		values = frappe.get_all(
			"Item Attribute Value", filters={"parent": name}, pluck="attribute_value"
		)
		self.assertIn("XXXL", values)


class _RepairClient:
	def __init__(self):
		self.deleted_products, self.deleted_variants = [], []

	def execute(self, query, variables=None, cost_hint=0):
		variables = variables or {}
		if "variantsIds" in variables:
			self.deleted_variants.extend(variables["variantsIds"])
			return {"productVariantsBulkDelete": {"product": {"id": variables["productId"]}, "userErrors": []}}
		if "input" in variables:
			self.deleted_products.append(variables["input"]["id"])
			return {"productDelete": {"deletedProductId": variables["input"]["id"], "userErrors": []}}
		return {}


class TestRepairingASplitRange(TemplateCase):
	def _split(self):
		template, children = self._range()
		# L and XXL dragged onto a duplicate product, XL left on the right one.
		self._link(children[0], template=template, product=OTHER_PRODUCT)
		self._link(children[2], template=template, product=OTHER_PRODUCT)
		self._link(children[1], template=self._bare_item("ZZ-TWIN"), product=PRODUCT)
		return template, children

	def test_a_dry_run_changes_nothing(self):
		template, children = self._split()
		client = _RepairClient()
		with patch.object(ShopifyClient, "for_store", staticmethod(lambda store: client)):
			report = repair_split_template(
				self.store, template, PRODUCT, drop_product=OTHER_PRODUCT, drop_variants=[children[1]]
			)

		self.assertFalse(report["apply"])
		self.assertEqual(client.deleted_products, [])
		self.assertEqual(client.deleted_variants, [])
		self.assertEqual(len(report["repointed"]), 3)
		self.assertEqual(
			frappe.db.get_value("Shopify Item Link", {"item_code": children[0]}, "product_gid"),
			OTHER_PRODUCT,
			"a dry run moved a link",
		)

	def test_applying_it_puts_the_range_back_on_one_product(self):
		template, children = self._split()
		client = _RepairClient()
		with patch.object(ShopifyClient, "for_store", staticmethod(lambda store: client)):
			report = repair_split_template(
				self.store, template, PRODUCT, drop_product=OTHER_PRODUCT, apply=True
			)
		self.made.append(("Shopify Item Link", report["template_link"]))

		self.assertEqual(client.deleted_products, [OTHER_PRODUCT])
		survivors = frappe.get_all(
			"Shopify Item Link",
			filters={"store": self.store, "item_code": ["in", children]},
			fields=["item_code", "product_gid", "template_item"],
		)
		for row in survivors:
			self.made.append(("Shopify Item Link", frappe.db.get_value(
				"Shopify Item Link", {"store": self.store, "item_code": row.item_code}, "name")))
			self.assertEqual(row.product_gid, PRODUCT)
			self.assertEqual(row.template_item, template)
		self.assertEqual(
			frappe.db.get_value("Shopify Item Link", {"store": self.store, "item_code": template}, "is_template"),
			1,
		)

	def test_it_will_not_delete_a_twin_that_still_has_variants(self):
		template, children = self._split()
		other, _kids = self._range(sizes=("S",))
		client = _RepairClient()
		with patch.object(ShopifyClient, "for_store", staticmethod(lambda store: client)):
			report = repair_split_template(
				self.store, template, PRODUCT, drop_template=other, apply=True
			)
		self.made.append(("Shopify Item Link", report["template_link"]))

		self.assertTrue(frappe.db.exists("Item", other))
		self.assertTrue(any("still has variants" in w for w in report["warnings"]))


class TestDisablingOneSize(TemplateCase):
	"""One size out of production must stop selling, and only that size."""

	LOCATION = "gid://shopify/Location/zz-tpl-1"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Without a mapped warehouse and the two toggles, every assertion below passes
		# vacuously: `_stores_for` returns nothing and the loop never runs.
		store = frappe.get_doc("Shopify Store", cls.store)
		store.sync_items = 1
		store.sync_inventory = 1
		if not any(row.location_gid == cls.LOCATION for row in (store.location_map or [])):
			warehouse = frappe.get_all(
				"Warehouse", filters={"is_group": 0}, limit=1, pluck="name"
			)[0]
			store.append("location_map", {"location_gid": cls.LOCATION, "warehouse": warehouse})
		store.save(ignore_permissions=True)
		frappe.db.commit()

	def test_a_disabled_item_offers_nothing(self):
		from shopify_integration.outbound.inventory import available_for_location

		template, children = self._range()
		store_doc = frappe.get_cached_doc("Shopify Store", self.store)
		location = self.LOCATION

		frappe.db.set_value("Item", children[0], "disabled", 1, update_modified=False)
		self.assertEqual(
			available_for_location(store_doc, children[0], location),
			0,
			"a size taken out of production was still offered at its last known stock",
		)

	def test_an_enabled_sibling_is_unaffected(self):
		from shopify_integration.outbound.inventory import available_for_location

		template, children = self._range()
		store_doc = frappe.get_cached_doc("Shopify Store", self.store)
		location = self.LOCATION

		frappe.db.set_value("Item", children[0], "disabled", 1, update_modified=False)
		# The sibling has no stock either, but it must not be short-circuited to zero by the
		# disabled check -- it should be answered from its bins like any other item.
		with patch(
			"shopify_integration.outbound.inventory.available_quantity", return_value=4
		):
			self.assertEqual(available_for_location(store_doc, children[1], location), 4)
			self.assertEqual(available_for_location(store_doc, children[0], location), 0)

	def test_disabling_queues_an_inventory_push(self):
		from shopify_integration.outbound import product as product_module

		template, children = self._range()
		self.made.append(("Shopify Item Link", link_template(self.store, template, PRODUCT)))
		self.made.append(("Shopify Item Link", self._link(children[0], template=template)))

		doc = frappe.get_doc("Item", children[0])
		doc.disabled = 1
		with patch.object(product_module, "enqueue_sync"), patch(
			"shopify_integration.outbound.inventory.enqueue_for_all_locations"
		) as pushed:
			doc.save(ignore_permissions=True)

		self.assertTrue(
			pushed.called,
			"disabling a size moved no stock, so nothing else would ever tell Shopify",
		)

	def test_an_ordinary_save_queues_no_inventory_push(self):
		from shopify_integration.outbound import product as product_module

		template, children = self._range()
		self.made.append(("Shopify Item Link", self._link(children[0], template=template)))

		doc = frappe.get_doc("Item", children[0])
		doc.item_name = "renamed, nothing to do with selling"
		with patch.object(product_module, "enqueue_sync"), patch(
			"shopify_integration.outbound.inventory.enqueue_for_all_locations"
		) as pushed:
			doc.save(ignore_permissions=True)

		self.assertFalse(pushed.called, "every save would push stock for every item")
