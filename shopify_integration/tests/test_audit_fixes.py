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


class TestShippingAsAnItem(FrappeTestCase):
	"""A store that books postage as a Shipping Item rather than a charge row -- which is
	what `utils/taxes.py` tells an india_compliance store to do, so that freight carries its
	own tax template like any other line.

	On a tax-inclusive store that configuration imported nothing at all. Every other line is
	booked gross and ERPNext backs the tax out of it; the shipping line was booked net, so
	its tax came off twice, the order came up short against what the customer paid, and
	`assert_total_matches` refused it. No test in the suite set a Shipping Item, so the
	whole configuration was uncovered.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from shopify_integration.tests.test_integration import SECRET_A, make_store, with_hsn

		cls.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)

		if not frappe.db.exists("Item", "ZZ-FREIGHT"):
			item = frappe.new_doc("Item")
			item.item_code = "ZZ-FREIGHT"
			item.item_name = "Delivery"
			item.item_group = frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0]
			item.stock_uom = "Nos"
			item.is_stock_item = 0
			with_hsn(item)
			item.insert(ignore_permissions=True)
			frappe.db.commit()

	def setUp(self):
		from shopify_integration.tests.test_orders import OrderTestCase

		# Reuse the order fixture's store wiring -- company, warehouse, tax map and the
		# ORD-TEE item its payloads refer to -- then add the Shipping Item on top.
		OrderTestCase.setUpClass()
		self.store_doc = frappe.get_doc("Shopify Store", OrderTestCase.store)
		self.store_doc.shipping_item = "ZZ-FREIGHT"
		self.store_doc.flags.ignore_mandatory = True
		self.store_doc.save(ignore_permissions=True)
		frappe.db.commit()
		self.addCleanup(self._unset_shipping_item)
		self.variant_gid = OrderTestCase.variant_gid

	def _unset_shipping_item(self):
		frappe.db.set_value("Shopify Store", self.store_doc.name, "shipping_item", None)
		frappe.clear_document_cache("Shopify Store", self.store_doc.name)
		frappe.db.commit()

	def _order(self, **kwargs):
		from shopify_integration.tests.test_orders import build_order

		kwargs.setdefault("variant_gid", self.variant_gid)
		return build_order(**kwargs)

	def test_a_tax_inclusive_order_with_shipping_imports(self):
		"""The regression. 100 of goods and 118 of postage, both tax-inclusive: the customer
		paid 218 and the order has to book 218."""
		from shopify_integration.inbound import order as order_module

		payload = self._order(
			gid=f"gid://shopify/Order/{frappe.generate_hash(length=8)}",
			qty=1,
			unit_price="100.00",
			tax="15.25",
			shipping="118.00",
			shipping_tax="18.00",
			taxes_included=True,
			total="218.00",
		)
		so = frappe.get_doc("Sales Order", order_module.create_sales_order(self.store_doc, payload))

		self.assertAlmostEqual(so.grand_total, 218.00, places=2)
		freight = next(row for row in so.items if row.item_code == "ZZ-FREIGHT")
		self.assertAlmostEqual(
			freight.rate, 118.00, places=2, msg="the postage line is gross, like every other line"
		)

	def test_a_tax_exclusive_order_with_shipping_still_imports(self):
		"""The configuration that already worked has to keep working."""
		from shopify_integration.inbound import order as order_module

		payload = self._order(
			gid=f"gid://shopify/Order/{frappe.generate_hash(length=8)}",
			qty=1,
			unit_price="100.00",
			tax="10.00",
			shipping="20.00",
			shipping_tax="2.00",
			taxes_included=False,
			total="132.00",
		)
		so = frappe.get_doc("Sales Order", order_module.create_sales_order(self.store_doc, payload))

		self.assertAlmostEqual(so.grand_total, 132.00, places=2)
		freight = next(row for row in so.items if row.item_code == "ZZ-FREIGHT")
		self.assertAlmostEqual(freight.rate, 20.00, places=2, msg="net, with its tax added on top")

	def test_the_shipping_item_is_credited_on_a_refund(self):
		"""Refunded postage is a credit line on a store that books it as an Item. It used to
		be looked for among the invoice's charge rows, where there is none -- so the note
		came up short and the whole refund was thrown away, not just the postage."""
		from shopify_integration.inbound import refund as refund_module

		quantities = {"ORD-TEE": {"qty": 1, "restock": False, "tax": 0, "amount": 0}}
		refund = {
			"id": "gid://shopify/Refund/ship-1",
			"refundShippingLines": {
				"nodes": [{"subtotalAmountSet": _bag("20.00"), "taxAmountSet": _bag("2.00")}]
			},
		}
		credit_note = frappe._dict({"taxes": []})  # exclusive: no included_in_print_rate rows

		with_shipping = refund_module._with_refunded_shipping(
			self.store_doc, credit_note, refund, quantities, "shopMoney"
		)

		self.assertIn("ZZ-FREIGHT", with_shipping)
		entry = with_shipping["ZZ-FREIGHT"]
		self.assertEqual(entry["qty"], 1)
		self.assertEqual(float(entry["rate"]), 20.00, "net, because the invoice was exclusive")
		self.assertEqual(entry["tax"], 0, "the shipping tax is added by the tax rows, not twice")

	def test_an_inclusive_credit_note_carries_the_postage_gross(self):
		from shopify_integration.inbound import refund as refund_module

		refund = {
			"id": "gid://shopify/Refund/ship-2",
			"refundShippingLines": {
				"nodes": [{"subtotalAmountSet": _bag("100.00"), "taxAmountSet": _bag("18.00")}]
			},
		}
		inclusive_note = frappe._dict(
			{"taxes": [frappe._dict({"charge_type": "On Net Total", "included_in_print_rate": 1})]}
		)

		with_shipping = refund_module._with_refunded_shipping(
			self.store_doc, inclusive_note, refund, {}, "shopMoney"
		)

		self.assertEqual(float(with_shipping["ZZ-FREIGHT"]["rate"]), 118.00)

	def test_a_refund_with_no_postage_adds_no_line(self):
		from shopify_integration.inbound import refund as refund_module

		self.assertEqual(
			refund_module._with_refunded_shipping(
				self.store_doc, frappe._dict({"taxes": []}), {"id": "r"}, {"ORD-TEE": {}}, "shopMoney"
			),
			{"ORD-TEE": {}},
		)

	def test_a_store_without_a_shipping_item_is_unchanged(self):
		from shopify_integration.inbound import refund as refund_module

		plain = frappe._dict({"shipping_item": None})
		refund = {
			"id": "r",
			"refundShippingLines": {
				"nodes": [{"subtotalAmountSet": _bag("20.00"), "taxAmountSet": _bag("2.00")}]
			},
		}
		self.assertEqual(
			refund_module._with_refunded_shipping(
				plain, frappe._dict({"taxes": []}), refund, {}, "shopMoney"
			),
			{},
		)


def _bag(amount: str) -> dict:
	return {"shopMoney": {"amount": amount}, "presentmentMoney": {"amount": amount}}


class TestPickingTheRetailPrice(FrappeTestCase):
	"""ERPNext allows many Item Price rows for one item and price list, differing by
	validity dates, UOM, quantity break or customer. The old lookup took whichever the
	database returned first, so a lapsed festival rate, a wholesale break or one customer's
	negotiated price could be published to the storefront as the retail price.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from shopify_integration.tests.test_integration import with_hsn

		cls.price_list = "ZZ Audit Retail"
		if not frappe.db.exists("Price List", cls.price_list):
			doc = frappe.new_doc("Price List")
			doc.price_list_name = cls.price_list
			doc.selling = 1
			doc.currency = frappe.defaults.get_global_default("currency") or "INR"
			doc.insert(ignore_permissions=True)

		cls.item = "ZZ-PRICED"
		if not frappe.db.exists("Item", cls.item):
			item = frappe.new_doc("Item")
			item.item_code = cls.item
			item.item_name = cls.item
			item.item_group = frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0]
			item.stock_uom = "Nos"
			with_hsn(item)
			item.insert(ignore_permissions=True)
		# No Standard Selling Rate: `current_price` falls back to it, which would mask a
		# lookup that found nothing.
		frappe.db.set_value("Item", cls.item, "standard_rate", 0)
		frappe.db.commit()

	def setUp(self):
		frappe.db.delete("Item Price", {"item_code": self.item})
		frappe.db.commit()
		self.addCleanup(frappe.db.commit)
		self.addCleanup(frappe.db.delete, "Item Price", {"item_code": self.item})

	def _price(self, rate, **kwargs):
		doc = frappe.new_doc("Item Price")
		doc.item_code = self.item
		doc.price_list = self.price_list
		doc.selling = 1
		doc.price_list_rate = rate
		for field, value in kwargs.items():
			setattr(doc, field, value)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc

	def _rate(self):
		"""Through `current_price`, which is what the catalogue actually calls -- testing the
		helper alone would pass against a version that never calls it."""
		from shopify_integration.outbound.price import current_price

		store_doc = frappe._dict({"name": "ZZ Audit", "selling_price_list": self.price_list})
		price = current_price(store_doc, self.item)
		return None if price is None else float(price)

	def test_the_plain_rate_is_used(self):
		self._price(1200)
		self.assertEqual(self._rate(), 1200)

	def test_a_lapsed_rate_is_not_used(self):
		self._price(1200)
		self._price(600, valid_from="2020-01-01", valid_upto="2020-12-31")
		self.assertEqual(self._rate(), 1200, "a festival price that ended in 2020 is not today's price")

	def test_a_rate_that_has_not_started_is_not_used(self):
		self._price(1200)
		self._price(900, valid_from="2099-01-01")
		self.assertEqual(self._rate(), 1200)

	def test_a_wholesale_break_is_not_the_retail_price(self):
		self._price(1200)
		self._price(800, packing_unit=50)
		self.assertEqual(self._rate(), 1200, "a price for 50 at a time is not what one costs")

	def test_one_customers_negotiated_price_is_not_published(self):
		customer = _a_customer()
		self._price(1200)
		self._price(700, customer=customer)
		self.assertEqual(self._rate(), 1200)

	def test_the_most_recently_valid_of_two_live_rates_wins(self):
		"""Deterministic, and the same choice ERPNext itself makes."""
		self._price(1200, valid_from="2026-01-01")
		self._price(1100, valid_from="2026-06-01")
		self.assertEqual(self._rate(), 1100)

	def test_no_usable_row_is_none(self):
		self._price(800, packing_unit=50)
		self.assertIsNone(self._rate(), "a quantity break alone is not a retail price")


def _a_customer(name: str = "ZZ Audit Customer") -> str:
	if not frappe.db.exists("Customer", name):
		doc = frappe.new_doc("Customer")
		doc.customer_name = name
		doc.customer_type = "Individual"
		doc.customer_group = frappe.get_all("Customer Group", filters={"is_group": 0}, limit=1, pluck="name")[
			0
		]
		doc.territory = frappe.get_all("Territory", filters={"is_group": 0}, limit=1, pluck="name")[0]
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
	return name


class TestOneBadRowDoesNotFailTheRest(FrappeTestCase):
	"""A drain claims up to fifty rows and hands them to one handler. When that handler
	raised, every row in the group was marked failed -- including the ones after the fault,
	which had never been attempted. Nothing reconciles product, price or media, so those
	were lost until somebody found them in the dashboard.
	"""

	def test_only_the_failing_row_is_failed(self):
		from shopify_integration.sync import engine

		rows = [{"name": f"ROW-{n}", "operation": "product"} for n in range(1, 4)]
		boom = RuntimeError("Shopify refused this one")

		with (
			patch.object(engine, "claim_rows", return_value=rows),
			patch.object(engine, "_succeed") as succeeded,
			patch.object(engine, "_handle_error", return_value="failed") as failed,
			patch.object(
				engine,
				"OPERATION_HANDLERS",
				{"product": "shopify_integration.tests.test_audit_fixes._raiser"},
			),
			patch.object(engine, "has_pending", return_value=False),
			patch.object(engine.frappe, "get_attr", return_value=_partial_raiser({"ROW-2": boom})),
		):
			result = engine._drain_locked("ZZ Store", 50)

		self.assertEqual(sorted(c[0][0] for c in succeeded.call_args_list), ["ROW-1", "ROW-3"])
		self.assertEqual([c[0][0]["name"] for c in failed.call_args_list], ["ROW-2"])
		self.assertEqual(result["done"], 2)

	def test_each_row_is_failed_with_its_own_reason(self):
		"""So the queue row's last_error says what actually happened to *that* product."""
		from shopify_integration.sync import engine

		rows = [{"name": "ROW-1", "operation": "product"}, {"name": "ROW-2", "operation": "product"}]
		first, second = RuntimeError("no such variant"), RuntimeError("image rejected")

		seen = {}
		with (
			patch.object(engine, "claim_rows", return_value=rows),
			patch.object(engine, "_succeed"),
			patch.object(
				engine,
				"_handle_error",
				side_effect=lambda row, exc: (seen.__setitem__(row["name"], str(exc)), "failed")[1],
			),
			patch.object(engine, "OPERATION_HANDLERS", {"product": "x"}),
			patch.object(engine, "has_pending", return_value=False),
			patch.object(
				engine.frappe, "get_attr", return_value=_partial_raiser({"ROW-1": first, "ROW-2": second})
			),
		):
			engine._drain_locked("ZZ Store", 50)

		self.assertEqual(seen, {"ROW-1": "no such variant", "ROW-2": "image rejected"})

	def test_a_handler_that_breaks_outright_still_fails_everything(self):
		"""A batched mutation, or a fault before any row was reached, is all-or-nothing."""
		from shopify_integration.sync import engine

		rows = [{"name": f"ROW-{n}", "operation": "inventory"} for n in range(1, 4)]

		def explode(store, claimed):
			raise RuntimeError("the whole call was refused")

		with (
			patch.object(engine, "claim_rows", return_value=rows),
			patch.object(engine, "_succeed") as succeeded,
			patch.object(engine, "_handle_error", return_value="failed") as failed,
			patch.object(engine, "OPERATION_HANDLERS", {"inventory": "x"}),
			patch.object(engine, "has_pending", return_value=False),
			patch.object(engine.frappe, "get_attr", return_value=explode),
		):
			engine._drain_locked("ZZ Store", 50)

		self.assertEqual(succeeded.call_count, 0)
		self.assertEqual(failed.call_count, 3)

	def test_the_row_handlers_isolate_their_rows(self):
		import inspect

		from shopify_integration.outbound import collections, media, product

		for handler in (product.push_products, collections.push_collections, media.push_media):
			source = inspect.getsource(handler)
			self.assertIn("raise PartialFailure(failures)", source, handler.__name__)


def _raiser(store, rows):
	raise RuntimeError("unused")


def _partial_raiser(failures):
	from shopify_integration.exceptions import PartialFailure

	def handler(store, rows):
		raise PartialFailure(failures)

	return handler


class TestRealTimeStockIsNotParkedBehindASweep(FrappeTestCase):
	"""Editing the tax rules on a big Item Group queues one collection row per published
	product under it. On strict `creation ASC` every POS sale from that moment queued
	behind thousands of them -- hours, on a real catalogue, for a figure that is supposed
	to reach the storefront in seconds.
	"""

	def test_inventory_outranks_everything(self):
		from shopify_integration.sync.engine import (
			DEFAULT_PRIORITY,
			PRIORITY_BY_OPERATION,
			REALTIME_PRIORITY,
			SWEEP_PRIORITY,
		)

		self.assertEqual(PRIORITY_BY_OPERATION["inventory"], REALTIME_PRIORITY)
		self.assertLess(REALTIME_PRIORITY, DEFAULT_PRIORITY)
		self.assertLess(DEFAULT_PRIORITY, SWEEP_PRIORITY)

	def test_the_claim_takes_priority_before_age(self):
		import inspect

		from shopify_integration.sync import engine

		self.assertIn("ORDER BY priority ASC, creation ASC", inspect.getsource(engine.claim_rows))

	def test_a_group_wide_recheck_queues_rows_that_may_wait(self):
		import inspect

		from shopify_integration.outbound import collections

		self.assertIn("sweep=True", inspect.getsource(collections.recheck_item_group))
		self.assertIn("SWEEP_PRIORITY if sweep else None", inspect.getsource(collections.enqueue_for_item))

	def test_an_ordinary_save_is_not_a_sweep(self):
		"""One item changing group still has to reach Shopify promptly."""
		import inspect

		from shopify_integration.outbound import collections

		self.assertNotIn("sweep=True", inspect.getsource(collections.on_item_change))


class TestInboundFailuresAreRetried(FrappeTestCase):
	"""The outbound queue has retried since the beginning; the inbound side never did. A
	webhook that failed -- a restarting worker, a locked document, an `orders/paid` that
	landed before its order -- sat in the log until a person noticed, and reconciliation
	only ever replays `orders/create`.
	"""

	def _event(self, topic="orders/create", status="Error", attempts=0, due="2020-01-01 00:00:00"):
		log = frappe.new_doc("Shopify Event Log")
		log.store = frappe.get_all("Shopify Store", limit=1, pluck="name")[0]
		log.topic = topic
		log.webhook_id = f"zz-{frappe.generate_hash(length=10)}"
		log.status = status
		log.attempts = attempts
		log.next_attempt_at = due
		log.payload = "{}"
		log.insert(ignore_permissions=True)
		frappe.db.commit()
		self.addCleanup(frappe.db.commit)
		self.addCleanup(frappe.delete_doc, "Shopify Event Log", log.name, force=True, ignore_permissions=True)
		return log

	def test_a_failed_event_that_is_due_is_queued_again(self):
		from shopify_integration.inbound.webhook import retry_failed_events

		log = self._event()
		with patch("shopify_integration.inbound.webhook.frappe.enqueue") as enqueued:
			retry_failed_events()

		self.assertIn(log.name, [c.kwargs.get("event_log") for c in enqueued.call_args_list])
		self.assertEqual(frappe.db.get_value("Shopify Event Log", log.name, "status"), "Queued")

	def test_one_not_yet_due_is_left_alone(self):
		from shopify_integration.inbound.webhook import retry_failed_events

		log = self._event(due="2099-01-01 00:00:00")
		with patch("shopify_integration.inbound.webhook.frappe.enqueue") as enqueued:
			retry_failed_events()

		self.assertNotIn(log.name, [c.kwargs.get("event_log") for c in enqueued.call_args_list])

	def test_it_gives_up_after_enough_attempts(self):
		"""A permanently broken event must not be retried for ever."""
		from shopify_integration.inbound.webhook import retry_failed_events
		from shopify_integration.shopify_integration.doctype.shopify_event_log.shopify_event_log import (
			ShopifyEventLog,
		)

		log = self._event(attempts=ShopifyEventLog.MAX_ATTEMPTS)
		with patch("shopify_integration.inbound.webhook.frappe.enqueue") as enqueued:
			retry_failed_events()

		self.assertNotIn(log.name, [c.kwargs.get("event_log") for c in enqueued.call_args_list])

	def test_a_successful_event_is_never_retried(self):
		from shopify_integration.inbound.webhook import retry_failed_events

		log = self._event(status="Success")
		with patch("shopify_integration.inbound.webhook.frappe.enqueue") as enqueued:
			retry_failed_events()

		self.assertNotIn(log.name, [c.kwargs.get("event_log") for c in enqueued.call_args_list])

	def test_a_topic_with_no_handler_stops_being_considered(self):
		from shopify_integration.inbound.webhook import retry_failed_events

		log = self._event(topic="zz/nonsense")
		with patch("shopify_integration.inbound.webhook.frappe.enqueue"):
			retry_failed_events()

		self.assertIsNone(frappe.db.get_value("Shopify Event Log", log.name, "next_attempt_at"))

	def test_failing_schedules_the_next_attempt_with_backoff(self):
		log = self._event(status="Queued", attempts=0, due=None)
		log.mark_error("boom")

		row = frappe.db.get_value(
			"Shopify Event Log", log.name, ["status", "attempts", "next_attempt_at"], as_dict=True
		)
		self.assertEqual(row.status, "Error")
		self.assertEqual(row.attempts, 1)
		self.assertIsNotNone(row.next_attempt_at, "a first failure has to come round again")

	def test_the_last_attempt_stops_scheduling(self):
		from shopify_integration.shopify_integration.doctype.shopify_event_log.shopify_event_log import (
			ShopifyEventLog,
		)

		log = self._event(status="Queued", attempts=ShopifyEventLog.MAX_ATTEMPTS - 1)
		log.mark_error("boom")

		self.assertIsNone(frappe.db.get_value("Shopify Event Log", log.name, "next_attempt_at"))

	def test_a_person_pressing_retry_starts_the_count_over(self):
		"""They changed something; that is why they are asking."""
		from shopify_integration.shopify_integration.doctype.shopify_event_log.shopify_event_log import (
			ShopifyEventLog,
		)

		log = self._event(attempts=ShopifyEventLog.MAX_ATTEMPTS)
		with patch(
			"shopify_integration.shopify_integration.doctype.shopify_event_log.shopify_event_log.frappe.enqueue"
		):
			frappe.get_doc("Shopify Event Log", log.name).retry()

		self.assertEqual(frappe.db.get_value("Shopify Event Log", log.name, "attempts"), 0)

	def test_the_sweep_is_scheduled(self):
		cron = frappe.get_hooks("scheduler_events").get("cron") or {}
		self.assertTrue(
			any(
				"shopify_integration.inbound.webhook.retry_failed_events" in methods
				for methods in cron.values()
			),
			"nothing retries inbound failures unless this is on the scheduler",
		)


class TestARefundOfPostageAlone(FrappeTestCase):
	"""Refunding the delivery charge for a late parcel names no line items, and that was
	refused outright -- Shopify showed the money returned and ERPNext did not, permanently,
	because nothing retried it either. A store that books shipping as an Item has a line to
	credit it against, so that much can go through on its own.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from shopify_integration.tests.test_integration import SECRET_A, make_store

		make_store("Test Store A", "test-a.myshopify.com", SECRET_A)

	def _refund(self, shipping=True):
		nodes = [{"subtotalAmountSet": _bag("20.00"), "taxAmountSet": _bag("2.00")}] if shipping else []
		return {"id": "gid://shopify/Refund/post-1", "refundShippingLines": {"nodes": nodes}}

	def test_a_store_with_a_shipping_item_can_credit_it(self):
		from shopify_integration.inbound.refund import _refunded_shipping_only

		self.assertTrue(
			_refunded_shipping_only(frappe._dict({"shipping_item": "ZZ-FREIGHT"}), self._refund())
		)

	def test_a_store_without_one_cannot(self):
		"""ERPNext will not take a Sales Invoice with no item lines at all, so there is
		nowhere to put the amount until somebody says where."""
		from shopify_integration.inbound.refund import _refunded_shipping_only

		self.assertFalse(_refunded_shipping_only(frappe._dict({"shipping_item": None}), self._refund()))

	def test_a_refund_of_nothing_in_particular_is_still_refused(self):
		"""A goodwill gesture has no line in ERPNext to credit. Refusing loudly beats
		inventing an account for somebody's money."""
		from shopify_integration.inbound.refund import _refunded_shipping_only

		self.assertFalse(
			_refunded_shipping_only(
				frappe._dict({"shipping_item": "ZZ-FREIGHT"}), self._refund(shipping=False)
			)
		)

	def test_the_refusal_says_what_to_do_about_it(self):
		import inspect

		from shopify_integration.inbound import refund as module

		source = inspect.getsource(module.create_credit_note)
		self.assertIn("Shipping Item", source)
		self.assertIn("goodwill", source)


class TestATestOrderOnADevelopmentStore(FrappeTestCase):
	"""Skipping test orders is right for a real shop and wrong for a development store,
	where the Bogus Gateway is the only way to pay at all -- so without a way to opt in,
	the integration could not be exercised end to end anywhere except a live shop with
	live money."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from shopify_integration.tests.test_integration import SECRET_A, make_store

		cls.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)

	def _fetch(self, order):
		from shopify_integration.inbound import order as module

		with patch.object(module.ShopifyClient, "for_store", return_value=_OrderClient(order)):
			return module.fetch_order(self.store, {"admin_graphql_api_id": order["id"]})

	def setUp(self):
		self.addCleanup(frappe.db.set_value, "Shopify Store", self.store, "import_test_orders", 0)

	def test_off_by_default(self):
		frappe.db.set_value("Shopify Store", self.store, "import_test_orders", 0)
		self.assertIsNone(self._fetch({"id": "gid://shopify/Order/t1", "name": "#t1", "test": True}))

	def test_a_store_that_opts_in_gets_them(self):
		frappe.db.set_value("Shopify Store", self.store, "import_test_orders", 1)
		fetched = self._fetch({"id": "gid://shopify/Order/t2", "name": "#t2", "test": True})
		self.assertIsNotNone(fetched)
		self.assertEqual(fetched["name"], "#t2")

	def test_a_real_order_is_unaffected_either_way(self):
		for opted_in in (0, 1):
			frappe.db.set_value("Shopify Store", self.store, "import_test_orders", opted_in)
			self.assertIsNotNone(
				self._fetch({"id": "gid://shopify/Order/r1", "name": "#r1", "test": False}),
				f"import_test_orders={opted_in}",
			)


class TestWhatACreditNoteIsCheckedAgainst(FrappeTestCase):
	"""Found against a live store: refunding one 1,200 saree from a tax-inclusive order was
	refused outright. Shopify reports that line as subtotal 1,200 with tax 183.05 -- the tax
	being *inside* the 1,200, not on top -- so adding the two demanded 1,383.05 from a credit
	note that is rightly 1,200, and the whole refund was thrown away.
	"""

	def _inclusive_line(self):
		"""The exact figures Shopify returned for order #1190 on the live store."""
		return {
			"subtotalSet": {"shopMoney": {"amount": "1200.00"}, "presentmentMoney": {"amount": "1200.00"}},
			"totalTaxSet": {"shopMoney": {"amount": "183.05"}, "presentmentMoney": {"amount": "183.05"}},
		}

	def _refund(self, line=None, shipping=None):
		return {
			"id": "gid://shopify/Refund/1",
			"refundLineItems": {"nodes": [line or self._inclusive_line()]},
			"refundShippingLines": {"nodes": shipping or []},
		}

	def test_an_inclusive_refund_expects_the_gross_subtotal(self):
		from shopify_integration.inbound.refund import _itemised_refund_total

		self.assertEqual(
			float(_itemised_refund_total(self._refund(), "shopMoney", inclusive=True)),
			1200.00,
			"the tax is already inside the subtotal",
		)

	def test_an_exclusive_refund_adds_the_tax_on_top(self):
		from shopify_integration.inbound.refund import _itemised_refund_total

		self.assertEqual(float(_itemised_refund_total(self._refund(), "shopMoney", inclusive=False)), 1383.05)

	def test_shipping_follows_the_same_rule(self):
		from shopify_integration.inbound.refund import _itemised_refund_total

		shipping = [
			{
				"subtotalAmountSet": {
					"shopMoney": {"amount": "100.00"},
					"presentmentMoney": {"amount": "100.00"},
				},
				"taxAmountSet": {"shopMoney": {"amount": "18.00"}, "presentmentMoney": {"amount": "18.00"}},
			}
		]
		refund = self._refund(shipping=shipping)
		self.assertEqual(float(_itemised_refund_total(refund, "shopMoney", inclusive=True)), 1300.00)
		self.assertEqual(float(_itemised_refund_total(refund, "shopMoney", inclusive=False)), 1501.05)

	def test_the_check_reads_inclusivity_off_the_credit_note(self):
		"""Not from the order payload -- the credit note's own tax rows are what the total
		it is being compared against was built from."""
		import inspect

		from shopify_integration.inbound import refund as module

		source = inspect.getsource(module._assert_credit_matches)
		self.assertIn('_is_inclusive(list(credit_note.get("taxes") or []))', source)


class TestTheReversingPaymentIsBookedOnAccount(FrappeTestCase):
	"""ERPNext will not allocate a payment against a credit note: its outstanding is
	negative, and both signs are refused -- a negative allocation leaves debit and credit
	unequal, a positive one trips "Allocated Amount cannot be greater than outstanding
	amount", and Receive is refused against a negative outstanding. Every shape was tried
	against a real site; only an on-account Pay submits.

	The books are right regardless: the credit note credits Debtors and this debits Debtors,
	so the customer nets to zero and the cash leaves. Only the document link is missing,
	which is Payment Reconciliation's job.
	"""

	def test_it_clears_the_reference_rows(self):
		import inspect

		from shopify_integration.inbound import refund as module

		source = inspect.getsource(module._reverse_payment)
		self.assertIn('entry.set("references", [])', source)

	def test_it_pays_what_actually_left_the_bank(self):
		"""Not the credit note's total: part of a refund can be settled in store credit."""
		import inspect

		from shopify_integration.inbound import refund as module

		source = inspect.getsource(module._reverse_payment)
		self.assertIn("refunded_amount(refund, side)", source)
		self.assertIn("entry.paid_amount = wanted", source)

	def test_it_names_the_credit_note_for_whoever_reconciles(self):
		import inspect

		from shopify_integration.inbound import refund as module

		self.assertIn("entry.remarks", inspect.getsource(module._reverse_payment))
