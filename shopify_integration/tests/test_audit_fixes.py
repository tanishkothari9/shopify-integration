"""Faults found by auditing the integration against the real Shopify schema and ERPNext.

None of these had a test, and most could not have had one in the shape the suite was
written: a fake client never reads the query text, never prices it, and never tells you
that the field you are sending was removed two API versions ago.
"""

from __future__ import annotations

import gzip
import json
import pathlib
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.api.client import QUERY_DIR


class TestSavingAVariantDoesNotArchiveTheWholeProduct(FrappeTestCase):
	"""Every child of a template shares one `product_gid`, so a variant's link points at the
	parent product. `push_products` built the product payload from whichever Item was saved:
	ticking Disabled on one out-of-production size archived the entire range from the
	storefront, and with sync_item_titles on it renamed the product after that one variant.
	"""

	def test_one_disabled_variant_does_not_archive_the_range(self):
		from shopify_integration.outbound.product import _product_is_dead

		template = _template_with(disabled=0, children=[0, 0, 1])
		self.assertFalse(
			_product_is_dead(template, frappe.get_doc("Item", template)),
			"one size going out of production is not a reason to take the range off sale",
		)

	def test_a_template_whose_variants_are_all_disabled_is_archived(self):
		from shopify_integration.outbound.product import _product_is_dead

		template = _template_with(disabled=0, children=[1, 1])
		self.assertTrue(_product_is_dead(template, frappe.get_doc("Item", template)))

	def test_a_disabled_template_is_archived(self):
		from shopify_integration.outbound.product import _product_is_dead

		template = _template_with(disabled=1, children=[0])
		self.assertTrue(_product_is_dead(template, frappe.get_doc("Item", template)))

	def test_a_plain_disabled_item_is_still_archived(self):
		from shopify_integration.outbound.product import _product_is_dead

		item = _plain_item(disabled=1)
		self.assertTrue(_product_is_dead(item, frappe.get_doc("Item", item)))

	def test_a_plain_live_item_is_not(self):
		from shopify_integration.outbound.product import _product_is_dead

		item = _plain_item(disabled=0)
		self.assertFalse(_product_is_dead(item, frappe.get_doc("Item", item)))

	def test_the_payload_is_built_from_the_template_not_the_saved_variant(self):
		import inspect

		from shopify_integration.outbound import product as module

		source = inspect.getsource(module.push_products)
		self.assertIn('variant_of") or item_code', source)
		self.assertIn("_product_is_dead(subject, item)", source)
		self.assertNotIn(
			'"status": "ARCHIVED" if item.disabled else "ACTIVE"',
			source,
			"the product's status must not be read off whichever variant happened to be saved",
		)


def _template_with(disabled: int, children: list[int]) -> str:
	from shopify_integration.tests.test_integration import with_hsn

	# Deliberately not a real ERPNext variant template -- nothing under test reads the
	# attribute table, and building one needs an Item Attribute plus matching values.
	code = f"ZZ-ARCH-{frappe.generate_hash(length=6)}"
	attribute = _an_attribute()
	doc = frappe.new_doc("Item")
	doc.item_code = code
	doc.item_name = code
	doc.item_group = frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0]
	doc.stock_uom = "Nos"
	doc.has_variants = 1
	doc.disabled = disabled
	doc.append("attributes", {"attribute": attribute})
	with_hsn(doc)
	doc.flags.ignore_mandatory = True
	doc.insert(ignore_permissions=True)

	for n, child_disabled in enumerate(children):
		child = frappe.new_doc("Item")
		child.item_code = f"{code}-{n}"
		child.item_name = child.item_code
		child.item_group = doc.item_group
		child.stock_uom = "Nos"
		child.variant_of = code
		child.disabled = child_disabled
		child.append("attributes", {"attribute": attribute, "attribute_value": f"V{n}"})
		with_hsn(child)
		child.flags.ignore_mandatory = True
		child.insert(ignore_permissions=True)
	frappe.db.commit()
	return code


def _an_attribute(name: str = "ZZ Audit Size") -> str:
	if not frappe.db.exists("Item Attribute", name):
		doc = frappe.new_doc("Item Attribute")
		doc.attribute_name = name
		for n in range(6):
			doc.append("item_attribute_values", {"attribute_value": f"V{n}", "abbr": f"V{n}"})
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
	return name


def _plain_item(disabled: int) -> str:
	from shopify_integration.tests.test_integration import with_hsn

	code = f"ZZ-PLAIN-{frappe.generate_hash(length=6)}"
	doc = frappe.new_doc("Item")
	doc.item_code = code
	doc.item_name = code
	doc.item_group = frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0]
	doc.stock_uom = "Nos"
	doc.disabled = disabled
	with_hsn(doc)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return code


class TestABadProductDoesNotDiscardTheGoodOnes(FrappeTestCase):
	"""`frappe.db.rollback()` with no save_point issues a full ROLLBACK and opens a new
	transaction. In the import loop that threw away every product written since the last
	200-product checkpoint, while `applied` still counted them and `lines_consumed` then
	advanced past them -- so they existed on Shopify, had no ERPNext Item, and nothing
	would ever revisit them.
	"""

	def test_the_import_rolls_back_to_a_savepoint(self):
		import inspect

		from shopify_integration.api import bulk

		source = inspect.getsource(bulk.process)
		self.assertIn("frappe.db.savepoint(save_point)", source)
		self.assertIn("frappe.db.rollback(save_point=save_point)", source)
		self.assertIn("frappe.db.release_savepoint(save_point)", source)

	def test_a_bare_rollback_really_would_discard_everything(self):
		"""The premise, confirmed against Frappe rather than assumed: this is why the
		savepoint matters."""
		import inspect

		source = inspect.getsource(frappe.db.rollback)
		self.assertIn("save_point", source)
		self.assertIn('self.sql("rollback")', source)


class TestTheBulkPollDoesNotSpin(FrappeTestCase):
	def test_a_running_operation_does_not_reschedule_itself(self):
		"""It used to, and the behaviour could not be reasoned about: `deduplicate`
		suppresses a job that is QUEUED *or* STARTED, so depending on when the after-commit
		enqueue landed the chain either stopped dead on the first poll or span as fast as a
		worker could turn it over."""
		import inspect

		from shopify_integration.api import bulk

		source = inspect.getsource(bulk.poll)
		running = source.split('if status in ("Created", "Running"):')[1].split("return status")[0]
		self.assertNotIn("schedule_poll", running)

	def test_the_cron_is_what_drives_it(self):
		cron = frappe.get_hooks("scheduler_events").get("cron") or {}
		self.assertIn("shopify_integration.api.bulk.poll_running_operations", cron.get("*/5 * * * *") or [])


class TestTheMediaQueryCanActuallyRun(FrappeTestCase):
	"""Shopify prices a query by its shape before running it, and charges every field inside
	a connection once per node requested. `product_media` used to nest
	`variants(first: 100) { media(first: 250) }` under its own `media(first: 250)`, which
	multiplies out to roughly 26,000 points against a ceiling of 1,000 -- so the entire
	image feature was refused with MAX_COST_EXCEEDED before it did anything. No fake client
	could have noticed.
	"""

	#: Shopify's documented model: a connection costs 2 + first x (cost of one node);
	#: an object costs 1; scalars and enums are free.
	CEILING = 1000

	def _cost(self, name: str) -> int:
		from graphql import parse

		document = parse((QUERY_DIR / f"{name}.graphql").read_text())
		return _selection_cost(document.definitions[0].selection_set)

	def test_the_product_media_query_fits_in_the_budget(self):
		self.assertLess(self._cost("product_media"), self.CEILING)

	def test_the_variant_media_query_fits_in_the_budget(self):
		self.assertLess(self._cost("product_variant_media"), self.CEILING)

	def test_the_old_shape_would_not_have(self):
		"""The calculator has to be able to see the fault, or the two tests above prove
		nothing."""
		from graphql import parse

		old = parse("""
			query ProductMedia($id: ID!) {
			  product(id: $id) {
			    id
			    media(first: 250) { nodes { ... on MediaImage { id status image { url } } } }
			    variants(first: 100) {
			      nodes { id media(first: 250) { nodes { ... on MediaImage { id } } } }
			    }
			  }
			}
		""")
		self.assertGreater(_selection_cost(old.definitions[0].selection_set), 20000)

	def test_variant_media_is_only_asked_for_when_there_is_one_to_attach(self):
		import inspect

		from shopify_integration.outbound import media

		source = inspect.getsource(media._attach_to_variants)
		self.assertIn('if not plan["by_variant"]:', source)
		self.assertLess(
			source.index('if not plan["by_variant"]:'),
			source.index("_variant_media("),
			"the cheap check has to come first, or every product pays for the call",
		)


def _selection_cost(selection_set) -> int:
	"""Shopify's calculated cost for the fields in one selection set.

	The rules: an object costs 1, a scalar or enum is free, and a connection costs
	2 + first x (1 + the cost of one node) -- the 1 being the node object itself, which is
	what makes a connection inside a connection multiply out so violently. `nodes`, `edges`,
	`node` and `pageInfo` are plumbing and are not themselves charged.
	"""
	from graphql.language import ast as gast

	total = 0
	for field in selection_set.selections:
		if isinstance(field, gast.InlineFragmentNode):
			total += _selection_cost(field.selection_set)
			continue
		if not getattr(field, "selection_set", None):
			continue  # a scalar or an enum

		name = field.name.value
		if name == "pageInfo":
			continue
		if name in ("nodes", "edges", "node"):
			total += _selection_cost(field.selection_set)
			continue

		page = None
		for argument in field.arguments or []:
			if argument.name.value in ("first", "last"):
				page = int(argument.value.value)

		if page is not None:
			total += 2 + page * (1 + _selection_cost(field.selection_set))
		else:
			total += 1 + _selection_cost(field.selection_set)
	return total


class TestTheSchemaIsTheVersionWeThinkItIs(FrappeTestCase):
	"""An audit claimed `compareQuantity` had been removed and the inventory push was
	already broken. It has not: the field the mutation actually takes is on
	`InventoryQuantityInput`, where it is deprecated with removal in 2026-04 -- a different
	type from the unused `InventorySetQuantityInput`. This pins both halves so the next
	person does not have to re-derive it, and fails loudly when 2026-04 is vendored.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		schema = pathlib.Path(__file__).resolve().parent.parent / "api" / "schema"
		version = json.loads(
			(
				pathlib.Path(__file__).resolve().parent.parent
				/ "shopify_integration"
				/ "doctype"
				/ "shopify_store"
				/ "shopify_store.json"
			).read_text()
		)
		cls.api_version = next(f["default"] for f in version["fields"] if f["fieldname"] == "api_version")
		cls.sdl = gzip.decompress((schema / f"admin_{cls.api_version}.graphql.gz").read_bytes()).decode()

	def test_the_field_the_mutation_takes_still_exists(self):
		from graphql import build_schema

		schema = build_schema(self.sdl, assume_valid=True)
		quantities = schema.type_map["InventorySetQuantitiesInput"].fields["quantities"]
		self.assertEqual(str(quantities.type), "[InventoryQuantityInput!]!")
		self.assertIn(
			"compareQuantity",
			schema.type_map["InventoryQuantityInput"].fields,
			"outbound/inventory.py sends compareQuantity; when this fails, move to "
			"changeFromQuantity and update COMPARE_MISMATCH_CODES to CHANGE_FROM_QUANTITY_STALE",
		)

		# The code sends it, so the two must not drift apart silently.

	def test_the_code_still_sends_that_field(self):
		import inspect

		from shopify_integration.outbound import inventory

		self.assertIn('entry["compareQuantity"]', inspect.getsource(inventory._push_batch))


class TestATestOrderIsNotASale(FrappeTestCase):
	"""Bogus Gateway orders are how everybody verifies this integration. Booked as real, one
	submits a Sales Order that reserves stock, a Sales Invoice, a Payment Entry into the
	real cash account and, on fulfilment, a Delivery Note that takes the goods off the shelf
	and tells Shopify the shop has fewer than it does.
	"""

	def test_the_order_query_asks_whether_it_is_one(self):
		self.assertIn("test", (QUERY_DIR / "order_by_id.graphql").read_text().split())

	def test_a_test_order_is_not_imported(self):
		from shopify_integration.inbound import order as module

		store = frappe.get_all("Shopify Store", limit=1, pluck="name")[0]
		client = _OrderClient({"id": "gid://shopify/Order/1", "name": "#1", "test": True})

		with patch.object(module.ShopifyClient, "for_store", return_value=client):
			self.assertIsNone(module.fetch_order(store, {"admin_graphql_api_id": "gid://shopify/Order/1"}))

	def test_a_real_order_still_is(self):
		from shopify_integration.inbound import order as module

		store = frappe.get_all("Shopify Store", limit=1, pluck="name")[0]
		client = _OrderClient({"id": "gid://shopify/Order/2", "name": "#2", "test": False})

		with patch.object(module.ShopifyClient, "for_store", return_value=client):
			fetched = module.fetch_order(store, {"admin_graphql_api_id": "gid://shopify/Order/2"})

		self.assertEqual(fetched["name"], "#2")

	def test_an_order_without_the_field_is_treated_as_real(self):
		"""A payload from before the field was requested must not start being skipped."""
		from shopify_integration.inbound import order as module

		store = frappe.get_all("Shopify Store", limit=1, pluck="name")[0]
		client = _OrderClient({"id": "gid://shopify/Order/3", "name": "#3"})

		with patch.object(module.ShopifyClient, "for_store", return_value=client):
			self.assertIsNotNone(module.fetch_order(store, {"admin_graphql_api_id": "gid://shopify/Order/3"}))


class _OrderClient:
	def __init__(self, order):
		self.order = order

	def execute(self, query, variables=None, cost_hint=0):
		return {"order": self.order}


class TestReadingInventoryLevelsAffordably(FrappeTestCase):
	"""`inventoryLevels(first: 20)` costs Shopify 62 points per item. At a batch of 100 that
	is ~6,200 against a per-query ceiling of 1,000, so a Stock Reconciliation touching
	thirty lines was refused with MAX_COST_EXCEEDED -- a permanent error, so every row in
	the claimed group was Failed and the stock never sent. The nightly drift check paged at
	200 and failed on its first page for every store.
	"""

	def test_the_level_read_asks_about_one_known_location(self):
		from graphql import parse

		document = (QUERY_DIR / "inventory_level_at.graphql").read_text()
		self.assertIn("inventoryLevel(locationId: $locationId)", document)
		self.assertLess(
			_selection_cost(parse(document).definitions[0].selection_set) * 100,
			1000,
			"a batch of 100 has to fit inside one query's budget",
		)

	def test_the_old_connection_shape_did_not_fit(self):
		from graphql import parse

		per_item = _selection_cost(
			parse((QUERY_DIR / "inventory_levels.graphql").read_text()).definitions[0].selection_set
		)
		self.assertGreater(per_item * 100, 1000, "this is the shape that was being refused")

	def test_the_nightly_page_fits(self):
		from graphql import parse

		from shopify_integration.sync.reconcile import LEVELS_PAGE

		per_item = _selection_cost(
			parse((QUERY_DIR / "inventory_levels.graphql").read_text()).definitions[0].selection_set
		)
		self.assertLess(per_item * LEVELS_PAGE, 1000)

	def test_one_call_per_location_not_per_item(self):
		from shopify_integration.outbound.inventory import _current_levels

		calls = []

		class _Client:
			def execute(self, query, variables, cost_hint=0):
				calls.append(variables)
				return {
					"nodes": [
						{"id": gid, "inventoryLevel": {"quantities": [{"name": "available", "quantity": 4}]}}
						for gid in variables["ids"]
					]
				}

		batch = [
			{"inventory_item_gid": f"gid://shopify/InventoryItem/{n}", "location_gid": "gid://L/1"}
			for n in range(30)
		] + [{"inventory_item_gid": "gid://shopify/InventoryItem/99", "location_gid": "gid://L/2"}]

		levels = _current_levels(_Client(), batch)

		self.assertEqual(len(calls), 2, "two locations, two calls -- not thirty-one")
		self.assertEqual(levels[("gid://shopify/InventoryItem/0", "gid://L/1")], 4)
		self.assertEqual(levels[("gid://shopify/InventoryItem/99", "gid://L/2")], 4)


class TestTheCompareIsAllOrNothing(FrappeTestCase):
	"""Shopify's wording for COMPARE_QUANTITY_REQUIRED is "The compareQuantity argument must
	be given to each quantity or ignored using ignoreCompareQuantity". Sending it for some
	entries and not others does not merely lose the check on those entries -- it fails the
	whole mutation, so every other item in the batch goes unset too. An item Shopify has
	never stocked at a location has no level, which is ordinary the first time a warehouse
	is mapped.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from shopify_integration.tests.test_integration import SECRET_A, make_store

		cls.store_doc = frappe.get_cached_doc(
			"Shopify Store", make_store("Test Store A", "test-a.myshopify.com", SECRET_A)
		)

	def _run(self, known: dict):
		from shopify_integration.outbound import inventory as module

		sent = []

		class _Client:
			def execute(self, query, variables, cost_hint=0):
				if "locationId" in variables:
					return {
						"nodes": [
							{
								"id": gid,
								"inventoryLevel": (
									{"quantities": [{"name": "available", "quantity": known[gid]}]}
									if gid in known
									else None
								),
							}
							for gid in variables["ids"]
						]
					}
				sent.append(variables["input"])
				return {"inventorySetQuantities": {"userErrors": []}}

		batch = [
			{
				"link": f"LINK-{n}",
				"item_code": f"I{n}",
				"inventory_item_gid": f"gid://shopify/InventoryItem/{n}",
				"location_gid": "gid://L/1",
				"quantity": 5,
			}
			for n in (1, 2)
		]
		with patch.object(module, "stamp_synced"):
			module._push_batch(_Client(), self.store_doc, batch, allow_retry=False)
		return sent

	def test_entries_with_a_level_all_carry_compare(self):
		sent = self._run({"gid://shopify/InventoryItem/1": 3, "gid://shopify/InventoryItem/2": 7})

		self.assertEqual(len(sent), 1)
		self.assertTrue(all("compareQuantity" in q for q in sent[0]["quantities"]))
		self.assertNotIn("ignoreCompareQuantity", sent[0])

	def test_an_item_with_no_level_goes_in_a_call_of_its_own(self):
		sent = self._run({"gid://shopify/InventoryItem/1": 3})

		self.assertEqual(len(sent), 2, "mixing them in one call fails the whole mutation")
		ignored = next(call for call in sent if call.get("ignoreCompareQuantity"))
		compared = next(call for call in sent if not call.get("ignoreCompareQuantity"))

		self.assertEqual(
			[q["inventoryItemId"] for q in ignored["quantities"]], ["gid://shopify/InventoryItem/2"]
		)
		self.assertTrue(all("compareQuantity" not in q for q in ignored["quantities"]))
		self.assertEqual(
			[q["inventoryItemId"] for q in compared["quantities"]], ["gid://shopify/InventoryItem/1"]
		)

	def test_an_item_nobody_has_a_level_for_is_still_set(self):
		"""The first push to a newly mapped warehouse. Failing it would leave the whole
		location permanently empty on the storefront."""
		sent = self._run({})

		self.assertEqual(len(sent), 1)
		self.assertTrue(sent[0]["ignoreCompareQuantity"])
		self.assertEqual(len(sent[0]["quantities"]), 2)
