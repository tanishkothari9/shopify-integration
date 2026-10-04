"""Website content, with ERPNext as the master.

The Website (Shopify) section on the Item is where the shop's own tooling writes the
storefront copy. This app sends it; Shopify holds a copy and never sends it back. The last
part is the one that was broken: a title edited in the Shopify admin renamed three ERPNext
Items that bills, POS receipts and barcodes are printed from.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.catalogue.mapping import erpnext_owns_content, write_product_mapping
from shopify_integration.outbound.content import (
	ContentError,
	collection_plan,
	desired_image_alts,
	desired_image_order,
	desired_metafields,
	tags_of,
	validate_category_metafields,
	website_payload,
)

GST_FIVE = "gid://shopify/Collection/gst-5"
MANUAL_A = "gid://shopify/Collection/manual-a"
MANUAL_B = "gid://shopify/Collection/manual-b"
SMART = "gid://shopify/Collection/smart-under-15k"
SARIS = "gid://shopify/TaxonomyCategory/aa-1-23-2-1"


class ContentCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from shopify_integration.tests.test_integration import SECRET_A, make_store

		cls.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)
		frappe.db.set_value("Shopify Store", cls.store, "sync_website_content", 1)
		frappe.db.commit()

	def setUp(self):
		self.made = []
		self.addCleanup(self._clean)
		self.store_doc = frappe.get_cached_doc("Shopify Store", self.store)

	def _clean(self):
		for doctype, name in reversed(self.made):
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_missing=True)
		frappe.db.commit()

	def _item(self, **content):
		from shopify_integration.tests.test_integration import with_hsn

		code = f"ZZ-WEB-{frappe.generate_hash(length=6)}"
		doc = frappe.new_doc("Item")
		doc.item_code = code
		doc.item_name = content.pop("item_name", "Plain ERPNext Name")
		doc.description = content.pop("description", "Plain ERPNext description")
		doc.item_group = frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0]
		doc.stock_uom = "Nos"
		metafields = content.pop("metafields", [])
		collections = content.pop("collections", [])
		alts = content.pop("image_alts", [])
		for field, value in content.items():
			doc.set(field, value)
		for row in metafields:
			doc.append("shopify_metafields", row)
		for row in collections:
			doc.append("shopify_collections", row)
		for row in alts:
			doc.append("shopify_image_alts", row)
		with_hsn(doc)
		doc.flags.ignore_mandatory = True
		doc.insert(ignore_permissions=True)
		self.made.append(("Item", code))
		frappe.db.commit()
		return doc


class TestWhatIsSent(ContentCase):
	def test_every_field_reaches_the_payload(self):
		item = self._item(
			shopify_title="Banarasi Silk Saree, Festive Edition",
			shopify_description="<p>Handwoven in Varanasi.</p>",
			shopify_seo_title="Banarasi Silk Saree | Smart Choice",
			shopify_seo_description="Handwoven Banarasi silk, free delivery across India.",
			shopify_handle="banarasi-silk-saree-festive",
			shopify_tags="saree, silk, festive",
			shopify_product_category=SARIS,
		)
		payload = website_payload(self.store_doc, item, creating=True)

		self.assertEqual(payload["title"], "Banarasi Silk Saree, Festive Edition")
		self.assertEqual(payload["descriptionHtml"], "<p>Handwoven in Varanasi.</p>")
		self.assertEqual(payload["seo"]["title"], "Banarasi Silk Saree | Smart Choice")
		self.assertEqual(
			payload["seo"]["description"], "Handwoven Banarasi silk, free delivery across India."
		)
		self.assertEqual(payload["handle"], "banarasi-silk-saree-festive")
		self.assertEqual(payload["tags"], ["saree", "silk", "festive"])
		self.assertEqual(payload["category"], SARIS)

	def test_empty_fields_fall_back_to_the_item(self):
		item = self._item()
		payload = website_payload(self.store_doc, item, creating=True)

		self.assertEqual(payload["title"], "Plain ERPNext Name")
		self.assertEqual(payload["descriptionHtml"], "Plain ERPNext description")
		for absent in ("seo", "tags", "category", "handle"):
			self.assertNotIn(absent, payload)

	def test_a_store_with_it_switched_off_sends_nothing(self):
		item = self._item(shopify_title="Written in ERPNext")
		frappe.db.set_value("Shopify Store", self.store, "sync_website_content", 0)
		self.addCleanup(frappe.db.set_value, "Shopify Store", self.store, "sync_website_content", 1)
		store_doc = frappe.get_doc("Shopify Store", self.store)

		self.assertEqual(website_payload(store_doc, item, creating=True), {})

	def test_a_changed_handle_leaves_a_redirect(self):
		item = self._item(shopify_handle="new-handle")
		self.assertTrue(website_payload(self.store_doc, item, creating=False)["redirectNewHandle"])

	def test_a_handle_set_at_creation_needs_no_redirect(self):
		item = self._item(shopify_handle="new-handle")
		self.assertNotIn("redirectNewHandle", website_payload(self.store_doc, item, creating=True))

	def test_tags_are_trimmed_and_de_duplicated(self):
		item = self._item(shopify_tags=" silk , Silk ,, festive ")
		self.assertEqual(tags_of(item), ["silk", "festive"])


class TestValidation(ContentCase):
	def test_the_field_lengths_stop_it_at_the_keyboard(self):
		"""The first line of defence, and the one a person actually meets.

		ERPNext refuses to save an over-long value at all, so the validator below only ever
		sees one that arrived some other way -- a data import, a patch, an API write.
		"""
		meta = frappe.get_meta("Item")
		self.assertEqual(meta.get_field("shopify_title").length, 255)
		self.assertEqual(meta.get_field("shopify_seo_title").length, 70)
		self.assertEqual(meta.get_field("shopify_seo_description").length, 320)

		with self.assertRaises(frappe.CharacterLengthExceededError):
			self._item(shopify_seo_title="x" * 71)

	def test_a_title_too_long_is_refused_by_name(self):
		item = self._item()
		item.shopify_title = "x" * 256
		with self.assertRaises(ContentError) as caught:
			website_payload(self.store_doc, item, creating=True)
		self.assertIn("Website Title", str(caught.exception))

	def test_an_seo_title_too_long_is_refused(self):
		item = self._item()
		item.shopify_seo_title = "x" * 71
		with self.assertRaises(ContentError) as caught:
			website_payload(self.store_doc, item, creating=True)
		self.assertIn("SEO Title", str(caught.exception))

	def test_an_seo_description_too_long_is_refused(self):
		item = self._item()
		item.shopify_seo_description = "x" * 321
		with self.assertRaises(ContentError) as caught:
			website_payload(self.store_doc, item, creating=True)
		self.assertIn("SEO Description", str(caught.exception))

	def test_a_handle_that_is_not_a_handle_is_refused(self):
		for bad in ("Banarasi Saree", "saree_silk", "-saree", "saree--silk"):
			item = self._item(shopify_handle=bad)
			with self.assertRaises(ContentError, msg=bad):
				website_payload(self.store_doc, item, creating=True)

	def test_a_good_handle_passes(self):
		item = self._item(shopify_handle="banarasi-silk-saree-2")
		self.assertEqual(
			website_payload(self.store_doc, item, creating=True)["handle"], "banarasi-silk-saree-2"
		)

	def test_a_category_attribute_without_a_category_is_refused(self):
		item = self._item(metafields=[{"namespace": "shopify", "key": "color", "type": "x", "value": "y"}])
		with self.assertRaises(ContentError) as caught:
			website_payload(self.store_doc, item, creating=True)
		self.assertIn("no Product Category", str(caught.exception))


class _TaxonomyClient:
	"""Shopify's taxonomy, in miniature: one category with one choice-list attribute."""

	def __init__(self, attributes=None, values=None):
		self.attributes = attributes if attributes is not None else [
			{"id": "gid://shopify/TaxonomyAttribute/colour", "name": "Color"},
			{"id": "gid://shopify/TaxonomyAttribute/fabric", "name": "Fabric"},
		]
		self.values = values if values is not None else {
			"gid://shopify/TaxonomyValue/red": "Red",
			"gid://shopify/TaxonomyValue/blue": "Blue",
		}

	def execute(self, query, variables=None, cost_hint=0):
		return {
			"node": {
				"id": SARIS,
				"fullName": "Apparel > Saris",
				"isLeaf": True,
				"attributes": {"nodes": self.attributes},
			}
		}

	def paginate(self, query, variables, path, *, cost_hint=0):
		for gid, name in self.values.items():
			yield {"id": gid, "name": name}


class TestCategoryAttributes(ContentCase):
	def test_a_good_value_passes(self):
		item = self._item(
			shopify_product_category=SARIS,
			metafields=[{
				"namespace": "shopify", "key": "color",
				"type": "list.metaobject_reference",
				"value": '["gid://shopify/TaxonomyValue/red"]',
			}],
		)
		self.assertEqual(validate_category_metafields(_TaxonomyClient(), item), [])

	def test_a_value_the_category_does_not_allow_is_reported(self):
		item = self._item(
			shopify_product_category=SARIS,
			metafields=[{
				"namespace": "shopify", "key": "color",
				"type": "list.metaobject_reference",
				"value": '["gid://shopify/TaxonomyValue/puce"]',
			}],
		)
		problems = validate_category_metafields(_TaxonomyClient(), item)
		self.assertEqual(len(problems), 1)
		self.assertIn("puce", problems[0])
		self.assertIn("Color", problems[0])

	def test_an_attribute_the_category_does_not_have_is_reported(self):
		item = self._item(
			shopify_product_category=SARIS,
			metafields=[{
				"namespace": "shopify", "key": "horsepower",
				"type": "single_line_text_field", "value": "12",
			}],
		)
		problems = validate_category_metafields(_TaxonomyClient(), item)
		self.assertEqual(len(problems), 1)
		self.assertIn("horsepower", problems[0])

	def test_a_name_with_spaces_maps_to_its_hyphenated_key(self):
		item = self._item(
			shopify_product_category=SARIS,
			metafields=[{
				"namespace": "shopify", "key": "embellishment-technique",
				"type": "single_line_text_field", "value": "",
			}],
		)
		client = _TaxonomyClient(
			attributes=[{"id": "gid://a/1", "name": "Embellishment technique"}], values={}
		)
		self.assertEqual(validate_category_metafields(client, item), [])

	def test_the_shops_own_metafields_are_not_checked_against_the_taxonomy(self):
		item = self._item(
			metafields=[{
				"namespace": "custom", "key": "saree_length",
				"type": "dimension", "value": '{"value":5.5,"unit":"METERS"}',
			}],
		)
		self.assertEqual(validate_category_metafields(_TaxonomyClient(), item), [])
		self.assertEqual(len(desired_metafields(item)), 1)


class TestCollections(ContentCase):
	def _describe(self, automatic=()):
		def describe(gids):
			return {
				gid: {"title": gid.rsplit("/", 1)[-1], "automatic": gid in automatic}
				for gid in gids
			}

		return describe

	def _link(self, item_code, owned=None):
		import json

		doc = frappe.new_doc("Shopify Item Link")
		doc.store, doc.item_code = self.store, item_code
		doc.product_gid = "gid://shopify/Product/coll-1"
		doc.variant_gid = f"gid://shopify/ProductVariant/{item_code}"
		doc.app_collections = json.dumps(owned) if owned else None
		doc.insert(ignore_permissions=True)
		self.made.append(("Shopify Item Link", doc.name))
		frappe.db.commit()
		return doc.name

    # -- the happy paths ----------------------------------------------------------
	def test_it_joins_what_the_table_names(self):
		item = self._item(collections=[{"collection_gid": MANUAL_A, "collection_title": "New In"}])
		link = self._link(item.name)
		plan = collection_plan(self.store_doc, item, link, self._describe())

		self.assertEqual(plan["join"], [MANUAL_A])
		self.assertEqual(plan["leave"], [])

	def test_removing_a_row_leaves_that_collection(self):
		item = self._item(collections=[{"collection_gid": MANUAL_A}])
		link = self._link(item.name, owned=[MANUAL_A, MANUAL_B])
		plan = collection_plan(self.store_doc, item, link, self._describe())

		self.assertEqual(plan["leave"], [MANUAL_B])
		self.assertEqual(plan["join"], [])
		self.assertEqual(plan["owned"], [MANUAL_A])

	def test_a_collection_the_merchant_added_by_hand_is_never_left(self):
		"""It is not in the app's record, so the app has no business removing it."""
		item = self._item(collections=[{"collection_gid": MANUAL_A}])
		link = self._link(item.name, owned=[MANUAL_A])
		plan = collection_plan(self.store_doc, item, link, self._describe())

		self.assertEqual(plan["leave"], [])

    # -- the guards ---------------------------------------------------------------
	def test_a_gst_collection_is_refused(self):
		store = frappe.get_doc("Shopify Store", self.store)
		template = frappe.get_all("Item Tax Template", limit=1, pluck="name")
		if not template:
			self.skipTest("this site has no Item Tax Template to map")
		store.append("tax_collection_map", {
			"item_tax_template": template[0], "collection_gid": GST_FIVE,
		})
		store.save(ignore_permissions=True)
		self.addCleanup(frappe.db.commit)
		self.addCleanup(
			frappe.db.sql,
			"DELETE FROM `tabShopify Tax Collection` WHERE collection_gid = %s",
			GST_FIVE,
		)
		frappe.db.commit()

		item = self._item(collections=[{"collection_gid": GST_FIVE, "collection_title": "GST 5%"}])
		link = self._link(item.name)
		plan = collection_plan(frappe.get_doc("Shopify Store", self.store), item, link, self._describe())

		self.assertEqual(plan["join"], [])
		self.assertEqual(len(plan["problems"]), 1)
		self.assertIn("GST", plan["problems"][0])

	def test_an_automatic_collection_is_skipped_with_a_reason(self):
		item = self._item(collections=[{"collection_gid": SMART, "collection_title": "Under 15k"}])
		link = self._link(item.name)
		plan = collection_plan(self.store_doc, item, link, self._describe(automatic={SMART}))

		self.assertEqual(plan["join"], [])
		self.assertIn("automatic", plan["problems"][0])

	def test_a_collection_that_does_not_exist_is_reported(self):
		item = self._item(collections=[{"collection_gid": MANUAL_A}])
		link = self._link(item.name)
		plan = collection_plan(self.store_doc, item, link, lambda gids: {})

		self.assertEqual(plan["join"], [])
		self.assertIn("does not exist", plan["problems"][0])


class TestImages(ContentCase):
	def test_alt_text_is_keyed_by_file(self):
		item = self._item(image_alts=[
			{"file_url": "/files/a.png", "alt_text": "Front", "position": 1},
			{"file_url": "/files/b.png", "alt_text": "Back", "position": 2},
			{"file_url": "/files/c.png", "alt_text": "", "position": 3},
		])
		self.assertEqual(
			desired_image_alts(item), {"/files/a.png": "Front", "/files/b.png": "Back"}
		)

	def test_order_follows_position(self):
		item = self._item(image_alts=[
			{"file_url": "/files/b.png", "alt_text": "Back", "position": 2},
			{"file_url": "/files/a.png", "alt_text": "Front", "position": 1},
		])
		self.assertEqual(desired_image_order(item), ["/files/a.png", "/files/b.png"])

	def test_rows_with_no_position_do_not_reorder_anything(self):
		item = self._item(image_alts=[{"file_url": "/files/a.png", "alt_text": "Front"}])
		self.assertEqual(desired_image_order(item), [])


class TestShopifyNeverWritesBack(ContentCase):
	"""STOITEM202605492, 498 and 499 were renamed "Aariz Luxe Kurta" from the Shopify admin.

	`item_name` is printed on bills, POS receipts and barcodes. Shopify holds a copy of the
	storefront title for the shop to edit; it is not the shop's record of what the thing is.
	"""

	def _product(self, item_code, title="Edited In The Shopify Admin"):
		return {
			"id": "gid://shopify/Product/writeback-1",
			"handle": "edited-in-shopify",
			"title": title,
			"description": "<p>Rewritten by the merchant on the storefront.</p>",
			"status": "ACTIVE",
			"vendor": "",
			"options": [],
			"variants": [{
				"id": f"gid://shopify/ProductVariant/{item_code}",
				"sku": item_code,
				"title": "Default Title",
				"selectedOptions": [],
				"inventoryItem": {"id": f"gid://shopify/InventoryItem/{item_code}", "measurement": {}},
			}],
		}

	def _published(self, **content):
		"""An Item this app published: ticked for Shopify, and linked with an ERPNext origin."""
		item = self._item(**content)
		frappe.db.set_value("Item", item.name, "publish_to_shopify", 1, update_modified=False)
		link = frappe.new_doc("Shopify Item Link")
		link.store, link.item_code = self.store, item.name
		link.product_gid = "gid://shopify/Product/writeback-1"
		link.variant_gid = f"gid://shopify/ProductVariant/{item.name}"
		link.origin = "ERPNext"
		link.insert(ignore_permissions=True)
		self.made.append(("Shopify Item Link", link.name))
		frappe.db.commit()
		return item

	def test_erpnext_is_recognised_as_the_owner(self):
		item = self._published()
		self.assertTrue(erpnext_owns_content(self.store, item.name))

	def test_the_checkbox_alone_is_enough_before_the_link_exists(self):
		"""The echo of our own publish can beat the link into existence."""
		item = self._item()
		frappe.db.set_value("Item", item.name, "publish_to_shopify", 1, update_modified=False)
		self.assertTrue(erpnext_owns_content(self.store, item.name))

	def test_a_product_imported_from_shopify_is_not_ours(self):
		item = self._item()
		self.assertFalse(erpnext_owns_content(self.store, item.name))

	def test_a_title_edited_in_shopify_does_not_rename_the_item(self):
		item = self._published(shopify_title="Smart Choice Banarasi Saree")
		before = frappe.db.get_value("Item", item.name, ["item_name", "description"], as_dict=True)

		write_product_mapping(self.store, self._product(item.name))

		after = frappe.db.get_value("Item", item.name, ["item_name", "description"], as_dict=True)
		self.assertEqual(after.item_name, before.item_name, "the Item was renamed from Shopify")
		self.assertEqual(after.description, before.description)

	def test_it_does_not_even_save_the_item(self):
		"""No save means no doc_events, which is what kept the loop going as well."""
		item = self._published()
		modified = frappe.db.get_value("Item", item.name, "modified")

		write_product_mapping(self.store, self._product(item.name))

		self.assertEqual(frappe.db.get_value("Item", item.name, "modified"), modified)

	def test_the_website_fields_are_left_exactly_as_erpnext_holds_them(self):
		item = self._published(
			shopify_title="Smart Choice Banarasi Saree",
			shopify_handle="smart-choice-banarasi",
			shopify_seo_title="Banarasi Saree | Smart Choice",
			shopify_tags="saree, silk",
		)
		write_product_mapping(self.store, self._product(item.name))

		kept = frappe.db.get_value(
			"Item",
			item.name,
			["shopify_title", "shopify_handle", "shopify_seo_title", "shopify_tags"],
			as_dict=True,
		)
		self.assertEqual(kept.shopify_title, "Smart Choice Banarasi Saree")
		self.assertEqual(kept.shopify_handle, "smart-choice-banarasi")
		self.assertEqual(kept.shopify_seo_title, "Banarasi Saree | Smart Choice")
		self.assertEqual(kept.shopify_tags, "saree, silk")

	def test_a_product_imported_from_shopify_still_takes_its_content(self):
		"""The other half: a range this shop does not master is Shopify's to describe."""
		item = self._item()
		link = frappe.new_doc("Shopify Item Link")
		link.store, link.item_code = self.store, item.name
		link.product_gid = "gid://shopify/Product/writeback-1"
		link.variant_gid = f"gid://shopify/ProductVariant/{item.name}"
		link.origin = "Shopify"
		link.insert(ignore_permissions=True)
		self.made.append(("Shopify Item Link", link.name))
		frappe.db.commit()

		write_product_mapping(self.store, self._product(item.name, title="Imported Name"))

		self.assertEqual(frappe.db.get_value("Item", item.name, "item_name"), "Imported Name")


class TestContentBelongsToTheTemplate(ContentCase):
	def test_a_variant_reads_its_templates_content(self):
		from shopify_integration.outbound.content import subject_of

		template = self._item(shopify_title="The Whole Range")
		frappe.db.set_value("Item", template.name, "has_variants", 1, update_modified=False)
		variant = self._item()
		frappe.db.set_value("Item", variant.name, "variant_of", template.name, update_modified=False)
		frappe.db.commit()

		self.assertEqual(subject_of(variant.name), template.name)
		self.assertEqual(subject_of(template.name), template.name)


class _DefinitionClient:
	"""Shopify answering metafieldDefinitionCreate: made, already there, or refused."""

	def __init__(self, mode="created"):
		self.mode = mode
		self.calls = 0

	def execute(self, query, variables=None, cost_hint=0):
		from shopify_integration.exceptions import ShopifyUserError

		self.calls += 1
		key = (variables or {})["definition"]["key"]
		if self.mode == "created":
			return {"metafieldDefinitionCreate": {
				"createdDefinition": {"id": f"gid://d/{key}", "namespace": "custom", "key": key},
				"userErrors": [],
			}}
		if self.mode == "raises_taken":
			raise ShopifyUserError(
				"metafieldDefinitionCreate rejected the write: definition.key: Key is in use "
				"for Product metafields on the 'custom' namespace.",
				user_errors=[{"field": ["definition", "key"], "message": "Key is in use", "code": "TAKEN"}],
			)
		if self.mode == "returns_taken":
			return {"metafieldDefinitionCreate": {
				"createdDefinition": None,
				"userErrors": [{"field": ["key"], "message": "Key is in use", "code": "TAKEN"}],
			}}
		raise ShopifyUserError("something else went wrong", user_errors=[{"message": "nope"}])


class TestMetafieldDefinitions(FrappeTestCase):
	def test_a_fresh_store_gets_all_eight(self):
		from shopify_integration.outbound.content import CUSTOM_DEFINITIONS, ensure_metafield_definitions

		report = ensure_metafield_definitions(_DefinitionClient("created"))
		self.assertEqual(len(report["created"]), len(CUSTOM_DEFINITIONS))
		self.assertEqual(report["failed"], [])

	def test_running_it_twice_is_a_no_op(self):
		"""Shopify raises rather than returning the error, which the first version missed."""
		from shopify_integration.outbound.content import CUSTOM_DEFINITIONS, ensure_metafield_definitions

		report = ensure_metafield_definitions(_DefinitionClient("raises_taken"))
		self.assertEqual(report["created"], [])
		self.assertEqual(len(report["already_there"]), len(CUSTOM_DEFINITIONS))
		self.assertEqual(report["failed"], [])

	def test_a_returned_taken_is_also_understood(self):
		from shopify_integration.outbound.content import CUSTOM_DEFINITIONS, ensure_metafield_definitions

		report = ensure_metafield_definitions(_DefinitionClient("returns_taken"))
		self.assertEqual(len(report["already_there"]), len(CUSTOM_DEFINITIONS))

	def test_a_real_refusal_is_reported_not_swallowed(self):
		from shopify_integration.outbound.content import ensure_metafield_definitions

		report = ensure_metafield_definitions(_DefinitionClient("other"))
		self.assertEqual(report["created"], [])
		self.assertTrue(report["failed"])

	def test_they_are_product_metafields_the_storefront_can_read(self):
		from shopify_integration.outbound.content import CUSTOM_DEFINITIONS

		keys = [d["key"] for d in CUSTOM_DEFINITIONS]
		self.assertEqual(sorted(keys), sorted([
			"saree_length", "blouse_piece", "care_instructions", "country_of_origin",
			"manufacturer", "net_quantity", "occasion", "work",
		]))


class TestImageOrderAtUpload(ContentCase):
	"""Shopify lists media in creation order and will not reorder what is still processing.

	On the pass that creates them, that is all of them -- so the order has to be decided
	before they go up, not after.
	"""

	def _ordered(self, files, rows):
		from shopify_integration.outbound.media import _in_merchants_order

		item = self._item(image_alts=rows)
		frappe.db.set_value("Shopify Store", self.store, "sync_website_content", 1)
		store_doc = frappe.get_doc("Shopify Store", self.store)
		# `_in_merchants_order` reads the Item fresh, so the rows must be on the saved doc.
		return _in_merchants_order(item.name, store_doc, files)

	def test_positions_decide_the_upload_order(self):
		files = ["/files/a.png", "/files/b.png", "/files/c.png"]
		self.assertEqual(
			self._ordered(files, [
				{"file_url": "/files/a.png", "alt_text": "Third", "position": 3},
				{"file_url": "/files/b.png", "alt_text": "Second", "position": 2},
				{"file_url": "/files/c.png", "alt_text": "First", "position": 1},
			]),
			["/files/c.png", "/files/b.png", "/files/a.png"],
		)

	def test_files_with_no_position_keep_their_place_at_the_back(self):
		files = ["/files/a.png", "/files/b.png", "/files/c.png"]
		self.assertEqual(
			self._ordered(files, [{"file_url": "/files/c.png", "alt_text": "First", "position": 1}]),
			["/files/c.png", "/files/a.png", "/files/b.png"],
		)

	def test_no_positions_at_all_changes_nothing(self):
		files = ["/files/a.png", "/files/b.png"]
		self.assertEqual(self._ordered(files, []), files)
