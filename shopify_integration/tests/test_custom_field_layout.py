"""The app's fields must not take other people's fields with them.

A Section Break does not merely add a section: Frappe moves every field after it, up to the
next section, inside the new one. Anchoring "Shopify" mid-section hid fifteen Sales Order
fields -- transaction_date, delivery_date, company, pos_profile and more -- eleven on Sales
Invoice and five on Delivery Note, inside a collapsed section nobody would think to open.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.install import (
	CUSTOM_FIELDS,
	END_OF_FORM,
	SECTION_ENDERS,
	end_of_form_anchor,
)

SECTIONED = ("Sales Order", "Sales Invoice", "Delivery Note")


def fields_in_shopify_section(doctype: str) -> list[str]:
	"""Everything Frappe puts inside the Shopify section, in form order."""
	fields = frappe.get_meta(doctype).fields
	start = next((i for i, f in enumerate(fields) if f.fieldname == "shopify_section"), None)
	if start is None:
		return []

	inside = []
	for field in fields[start + 1 :]:
		if field.fieldtype in SECTION_ENDERS:
			break
		inside.append(field.fieldname)
	return inside


class TestTheSectionHoldsOnlyOurFields(FrappeTestCase):
	def test_nothing_of_anyone_elses_is_inside_it(self):
		"""The regression, asked of the live meta on each of the three forms."""
		for doctype in SECTIONED:
			with self.subTest(doctype=doctype):
				ours = {field["fieldname"] for field in CUSTOM_FIELDS[doctype]}
				inside = fields_in_shopify_section(doctype)

				self.assertTrue(inside, f"{doctype} should have a Shopify section")
				strangers = [name for name in inside if name not in ours]
				self.assertEqual(
					strangers,
					[],
					f"{doctype}: these belong to someone else and are now hidden -- {strangers}",
				)

	def test_all_of_ours_are_inside_it(self):
		"""The other half: the section is there to hold them."""
		for doctype in SECTIONED:
			with self.subTest(doctype=doctype):
				ours = [
					field["fieldname"]
					for field in CUSTOM_FIELDS[doctype]
					if field["fieldtype"] != "Section Break"
				]
				self.assertEqual(fields_in_shopify_section(doctype), ours)

	def test_no_data_field_is_left_after_the_section(self):
		"""It belongs at the end of the form's content.

		A Tab Break may still follow -- ERPNext ends these forms with a Connections tab, and
		putting the Shopify panel *after* that would bury it inside Connections, which is
		worse than where it is. What must not follow is an ordinary field.
		"""
		for doctype in SECTIONED:
			with self.subTest(doctype=doctype):
				fields = frappe.get_meta(doctype).fields
				start = next(i for i, f in enumerate(fields) if f.fieldname == "shopify_section")
				ours = {x["fieldname"] for x in CUSTOM_FIELDS[doctype]}
				stragglers = [
					f.fieldname
					for f in fields[start + 1 :]
					if f.fieldname not in ours and f.fieldtype not in (*SECTION_ENDERS, "Column Break")
				]
				self.assertEqual(stragglers, [], f"{doctype} has real fields after the Shopify section")


class TestChoosingTheAnchor(FrappeTestCase):
	def test_the_anchor_is_a_field_that_already_ends_its_section(self):
		"""The whole rule in one assertion: nothing follows it but a new section."""
		for doctype in SECTIONED:
			with self.subTest(doctype=doctype):
				anchor = end_of_form_anchor(doctype)
				self.assertTrue(anchor, f"{doctype} should have somewhere safe to anchor")

				ours = {field["fieldname"] for field in CUSTOM_FIELDS[doctype]}
				fields = [f for f in frappe.get_meta(doctype).fields if f.fieldname not in ours]
				index = next(i for i, f in enumerate(fields) if f.fieldname == anchor)
				following = fields[index + 1] if index + 1 < len(fields) else None

				self.assertTrue(
					following is None or following.fieldtype in SECTION_ENDERS,
					f"{doctype}: anchoring after '{anchor}' would swallow "
					f"'{following.fieldname if following else None}'",
				)

	def test_the_anchor_is_never_one_of_our_own_fields(self):
		"""Otherwise it would drift a little further every time this runs."""
		for doctype in SECTIONED:
			with self.subTest(doctype=doctype):
				ours = {field["fieldname"] for field in CUSTOM_FIELDS[doctype]}
				self.assertNotIn(end_of_form_anchor(doctype), ours)

	def test_it_is_stable_across_runs(self):
		for doctype in SECTIONED:
			with self.subTest(doctype=doctype):
				self.assertEqual(end_of_form_anchor(doctype), end_of_form_anchor(doctype))

	def test_a_doctype_with_no_section_breaks_anchors_on_its_last_field(self):
		with_none = frappe._dict(
			fields=[
				frappe._dict(fieldname="a", fieldtype="Data"),
				frappe._dict(fieldname="b", fieldtype="Data"),
			]
		)
		from unittest.mock import patch

		with patch.object(frappe, "get_meta", return_value=with_none):
			self.assertEqual(end_of_form_anchor("Whatever"), "b")

	def test_a_column_break_does_not_end_a_section(self):
		"""Anchoring before one would pull the rest of the row in with it."""
		from unittest.mock import patch

		meta = frappe._dict(
			fields=[
				frappe._dict(fieldname="a", fieldtype="Data"),
				frappe._dict(fieldname="col", fieldtype="Column Break"),
				frappe._dict(fieldname="b", fieldtype="Data"),
				frappe._dict(fieldname="sec", fieldtype="Section Break"),
				frappe._dict(fieldname="c", fieldtype="Data"),
			]
		)
		with patch.object(frappe, "get_meta", return_value=meta):
			self.assertEqual(end_of_form_anchor("Whatever"), "c")


class TestTheDefinitionsThemselves(FrappeTestCase):
	def test_no_section_break_names_a_hard_coded_anchor(self):
		"""Which field ends a section differs by ERPNext version and by what else is
		installed, so it has to be worked out at install time."""
		for doctype, fields in CUSTOM_FIELDS.items():
			for field in fields:
				if field["fieldtype"] == "Section Break":
					with self.subTest(doctype=doctype, field=field["fieldname"]):
						self.assertEqual(
							field.get("insert_after"),
							END_OF_FORM,
							"a Section Break must never be dropped into the middle of a section",
						)

	def test_plain_fields_may_still_choose_their_place(self):
		"""Customer adds no section, so it cannot move anything."""
		for doctype in ("Customer",):
			breaks = [f for f in CUSTOM_FIELDS[doctype] if f["fieldtype"] == "Section Break"]
			self.assertEqual(breaks, [], f"{doctype} should not introduce a section")

	def test_item_introduces_exactly_one_section(self):
		"""The Website (Shopify) panel, and nothing else.

		One, because a second Section Break inside it would be just as capable of swallowing
		the rest of the form if the first one ever moved -- and the fields after it are
		already all ours, so it would buy nothing but that risk.
		"""
		breaks = [f for f in CUSTOM_FIELDS["Item"] if f["fieldtype"] == "Section Break"]
		self.assertEqual([f["fieldname"] for f in breaks], ["shopify_website_section"])
		self.assertEqual(breaks[0]["insert_after"], END_OF_FORM)
