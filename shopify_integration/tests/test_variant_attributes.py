"""ERPNext refuses to change a variant's attributes once the item has stock.

Clearing the rows and re-adding identical ones counts as a change, so every products/update
for a stocked variant failed -- and took the rest of the update (name, description, archived
flag, weight) with it. Shopify fires products/update on inventory movements among much else,
so on the test site that was 35 of 81 events.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.catalogue.mapping import (
	_apply_variant_attributes,
	_attribute_pairs,
	_same_attributes,
)


def variant(options):
	return {"selectedOptions": [{"name": name, "value": value} for name, value in options]}


class _Row:
	def __init__(self, attribute, attribute_value):
		self.attribute = attribute
		self.attribute_value = attribute_value


class _Item:
	"""Just enough Item to exercise the comparison without touching the database."""

	def __init__(self, name, rows):
		self.name = name
		self._rows = [_Row(*row) for row in rows]
		self.cleared = False

	def get(self, field):
		return self._rows if field == "attributes" else None

	def set(self, field, value):
		if field == "attributes":
			self.cleared = True
			self._rows = list(value)

	def append(self, field, value):
		if field == "attributes":
			self._rows.append(_Row(value["attribute"], value["attribute_value"]))


class TestComparingAttributes(FrappeTestCase):
	def test_identical_options_are_the_same(self):
		item = _Item("EAR-GOL", [("Colour", "Golden")])
		self.assertTrue(_same_attributes(item, _attribute_pairs(variant([("Colour", "Golden")]))))

	def test_case_and_spacing_do_not_count_as_a_change(self):
		"""'Golden' and 'golden ' are the same option to a merchant, and rewriting the row for
		that difference is exactly what ERPNext refuses."""
		item = _Item("EAR-GOL", [("Colour", "Golden")])
		self.assertTrue(_same_attributes(item, _attribute_pairs(variant([("colour", " golden ")]))))

	def test_order_does_not_count_as_a_change(self):
		item = _Item("TEE", [("Size", "M"), ("Colour", "Red")])
		self.assertTrue(_same_attributes(item, _attribute_pairs(variant([("Colour", "Red"), ("Size", "M")]))))

	def test_a_different_value_is_a_change(self):
		item = _Item("EAR-GOL", [("Colour", "Golden")])
		self.assertFalse(_same_attributes(item, _attribute_pairs(variant([("Colour", "Silver")]))))

	def test_an_extra_option_is_a_change(self):
		item = _Item("TEE", [("Size", "M")])
		self.assertFalse(
			_same_attributes(item, _attribute_pairs(variant([("Size", "M"), ("Colour", "Red")])))
		)

	def test_blank_names_and_values_are_dropped(self):
		self.assertEqual(
			_attribute_pairs(variant([("Colour", "Red"), ("", "X"), ("Size", "")])),
			[("Colour", "Red")],
		)


class TestWhatGetsWritten(FrappeTestCase):
	"""_apply_variant_attributes decides between rewriting, leaving alone, and warning."""

	def _with_stock(self, has_stock):
		from unittest.mock import patch

		return patch("shopify_integration.catalogue.mapping._has_stock_history", return_value=has_stock)

	def test_unchanged_options_on_a_stocked_item_touch_nothing(self):
		"""The regression: this used to clear and re-append, which ERPNext rejects."""
		item = _Item("EAR-GOL", [("Colour", "Golden")])
		with self._with_stock(True):
			warning = _apply_variant_attributes(item, variant([("Colour", "Golden")]), existing=True)
		self.assertIsNone(warning)
		self.assertFalse(item.cleared, "attributes must not be rewritten when they already match")

	def test_a_changed_option_on_a_stocked_item_is_reported_not_forced(self):
		item = _Item("EAR-GOL", [("Colour", "Golden")])
		with self._with_stock(True):
			warning = _apply_variant_attributes(item, variant([("Colour", "Silver")]), existing=True)
		self.assertFalse(item.cleared, "ERPNext would refuse this change; it must not be attempted")
		self.assertIn("EAR-GOL", warning)
		self.assertIn("Silver", warning)
		self.assertIn("stock transactions", warning)

	def test_a_changed_option_without_stock_is_applied(self):
		item = _Item("EAR-GOL", [("Colour", "Golden")])
		with self._with_stock(False), patch_ensure():
			warning = _apply_variant_attributes(item, variant([("Colour", "Silver")]), existing=True)
		self.assertIsNone(warning)
		self.assertTrue(item.cleared)
		self.assertEqual(
			[(r.attribute, r.attribute_value) for r in item.get("attributes")], [("Colour", "Silver")]
		)

	def test_a_brand_new_item_always_gets_its_attributes(self):
		item = _Item("NEW-VAR", [])
		with self._with_stock(True), patch_ensure():
			warning = _apply_variant_attributes(item, variant([("Colour", "Red")]), existing=False)
		self.assertIsNone(warning)
		self.assertEqual(
			[(r.attribute, r.attribute_value) for r in item.get("attributes")], [("Colour", "Red")]
		)

	def test_a_variant_shopify_sends_no_options_for_is_left_alone(self):
		"""Nothing to apply, and clearing the rows would be a change ERPNext refuses."""
		item = _Item("EAR-GOL", [("Colour", "Golden")])
		with self._with_stock(True):
			warning = _apply_variant_attributes(item, variant([]), existing=True)
		self.assertIsNone(warning)
		self.assertFalse(item.cleared)


def patch_ensure():
	from unittest.mock import patch

	return patch("shopify_integration.catalogue.mapping.ensure_attribute_value", return_value=None)


class TestStockHistoryIsRealityChecked(FrappeTestCase):
	def test_an_item_with_no_ledger_entries_has_no_stock_history(self):
		from shopify_integration.catalogue.mapping import _has_stock_history

		self.assertFalse(_has_stock_history("ZZ-ITEM-THAT-DOES-NOT-EXIST"))

	def test_an_item_with_a_ledger_entry_has_stock_history(self):
		from shopify_integration.catalogue.mapping import _has_stock_history

		entry = frappe.get_all(
			"Stock Ledger Entry", filters={"is_cancelled": 0}, limit=1, fields=["item_code"]
		)
		if not entry:
			self.skipTest("no stock ledger entry on this site to check against")
		self.assertTrue(_has_stock_history(entry[0].item_code))
