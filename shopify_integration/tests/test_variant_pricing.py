"""A variant template carries no price of its own -- the prices are on its variants.

Judging the template by its own price made almost the whole catalogue go live as DRAFT,
correctly priced variants and all. And DRAFT products are never published to the Online
Store, so they were invisible twice over. Seen on STOITEM202001143 (KURTI CRE NET): five
variants at 550, template unpriced, product created DRAFT.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.outbound.product import _sellable_variants


def child(item_code, values=None):
	return {"item_code": item_code, "values": values or []}


def priced(*codes):
	"""A _selling_price stand-in: the named items have one, everything else does not."""
	wanted = set(codes)
	return lambda item_code, store_doc: 550 if item_code in wanted else None


class TestChoosingWhichVariantsGoUp(FrappeTestCase):
	STORE = object()

	def _split(self, children, with_prices):
		with patch("shopify_integration.outbound.product._selling_price", side_effect=priced(*with_prices)):
			return _sellable_variants(children, self.STORE)

	def test_every_variant_priced_means_all_of_them_and_a_live_product(self):
		children = [child("KURTI-S"), child("KURTI-M"), child("KURTI-L")]
		sellable, unpriced, any_priced = self._split(children, ("KURTI-S", "KURTI-M", "KURTI-L"))

		self.assertEqual([c["item_code"] for c in sellable], ["KURTI-S", "KURTI-M", "KURTI-L"])
		self.assertEqual(unpriced, [])
		self.assertTrue(any_priced, "the template goes ACTIVE on its variants' prices")

	def test_an_unpriced_variant_is_held_back_and_named(self):
		"""Listing it would put it in the shop at 0.00, which is worse than not listing it."""
		children = [child("KURTI-S"), child("KURTI-M"), child("KURTI-L")]
		sellable, unpriced, any_priced = self._split(children, ("KURTI-S", "KURTI-M"))

		self.assertEqual([c["item_code"] for c in sellable], ["KURTI-S", "KURTI-M"])
		self.assertEqual(unpriced, ["KURTI-L"], "and it has to be named, so someone can price it")
		self.assertTrue(any_priced)

	def test_no_variant_priced_keeps_them_all_and_stays_draft(self):
		"""The product sells nothing either way, so keep every SKU, mapping and inventory link
		ready for the day it is priced."""
		children = [child("KURTI-S"), child("KURTI-M")]
		sellable, unpriced, any_priced = self._split(children, ())

		self.assertEqual([c["item_code"] for c in sellable], ["KURTI-S", "KURTI-M"])
		self.assertEqual(unpriced, [], "nothing is held back when nothing is going up")
		self.assertFalse(any_priced, "DRAFT is right here")

	def test_a_template_with_no_variants_is_nothing(self):
		self.assertEqual(_sellable_variants([], self.STORE), ([], [], False))

	def test_one_priced_variant_out_of_five_is_enough_to_go_live(self):
		children = [child(f"K-{n}") for n in range(5)]
		sellable, unpriced, any_priced = self._split(children, ("K-3",))

		self.assertTrue(any_priced)
		self.assertEqual([c["item_code"] for c in sellable], ["K-3"])
		self.assertEqual(len(unpriced), 4)


class TestTheStatusThatReachesShopify(FrappeTestCase):
	"""What _create_product actually sends, with Shopify and the database stubbed out."""

	def _create(self, has_variants, variant_prices=(), own_price=None):
		from shopify_integration.outbound import product as product_module

		sent = {}

		class _Client:
			def execute(self, query, variables, cost_hint=0):
				if "productCreate" in query:
					sent.update(variables["product"])
					return {
						"productCreate": {
							"product": {"id": "gid://shopify/Product/1", "variants": {"nodes": []}}
						}
					}
				return {}

		children = [child(code) for code in ("K-S", "K-M", "K-L")] if has_variants else []
		item = frappe._dict(
			{
				"item_name": "KURTI CRE NET",
				"description": "",
				"item_group": "X",
				"disabled": 0,
				"has_variants": 1 if has_variants else 0,
			}
		)

		def selling_price(item_code, store_doc):
			if item_code in variant_prices:
				return 550
			return own_price if item_code == "TPL" else None

		with (
			patch.object(product_module.frappe, "get_doc", return_value=item),
			patch.object(
				product_module.frappe,
				"get_cached_doc",
				return_value=frappe._dict({"selling_price_list": "Standard Selling"}),
			),
			patch.object(product_module, "_variants_of", return_value=children),
			patch.object(product_module, "_selling_price", side_effect=selling_price),
			patch.object(product_module, "_options_from", return_value=[]),
			patch.object(product_module, "_create_variants"),
			patch.object(product_module, "_fill_variant"),
			patch.object(product_module, "_reread", return_value={"id": "gid://shopify/Product/1"}),
			patch.object(product_module, "_link_variants"),
			patch.object(product_module, "_link"),
			patch.object(product_module, "publish_to_online_store") as published,
		):
			product_module._create_product(_Client(), "Test Store A", "TPL")

		return sent.get("status"), published.called

	def test_a_template_whose_variants_are_priced_goes_live(self):
		"""The regression: this used to be DRAFT, and DRAFT is never published."""
		status, published = self._create(True, variant_prices=("K-S", "K-M", "K-L"))
		self.assertEqual(status, "ACTIVE")
		self.assertTrue(published, "and an ACTIVE product belongs on the Online Store")

	def test_one_priced_variant_is_enough(self):
		status, published = self._create(True, variant_prices=("K-M",))
		self.assertEqual(status, "ACTIVE")
		self.assertTrue(published)

	def test_a_template_with_nothing_priced_stays_draft(self):
		status, published = self._create(True, variant_prices=())
		self.assertEqual(status, "DRAFT")
		self.assertFalse(published, "a draft is not sellable, so there is nothing to publish")

	def test_a_plain_item_still_follows_its_own_price(self):
		self.assertEqual(self._create(False, own_price=550)[0], "ACTIVE")
		self.assertEqual(self._create(False, own_price=None)[0], "DRAFT")


class TestPricingItLaterStillWorks(FrappeTestCase):
	"""DRAFT -> ACTIVE on a later save is the existing pending/decided path; this pins that the
	template case goes through it rather than around it."""

	def test_the_update_path_publishes_a_draft_that_becomes_active(self):
		import inspect

		from shopify_integration.outbound import product as product_module

		source = inspect.getsource(product_module.push_products)
		self.assertIn("_publish_decided", source)
		self.assertIn('was in ("DRAFT", "ARCHIVED")', source)
		self.assertIn("publish_to_online_store", source)
