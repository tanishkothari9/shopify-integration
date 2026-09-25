"""Matching a Shopify shopper to the customer you already have (by mobile number).

The gap this closes: the Shopify GID only recognises someone who has bought online here
before. A shopper who already buys in person and then signs up on the website arrives with a
brand new GID, so GID-only matching books them a second ERPNext record and splits their
history across two ledgers -- exactly what this module claims never to do.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.inbound.customer import (
	MAX_CUSTOMERS_PER_MOBILE,
	find_customer_by_mobile,
	mobile_from,
	normalise_mobile,
)


class TestNormalisingAMobile(FrappeTestCase):
	"""One person's number reaches us spelled a dozen ways."""

	def test_the_same_number_written_five_ways_is_one_number(self):
		for spelling in (
			"+919800011122",
			"09800011122",
			"+91 98000 11122",
			"98000-11122",
			"9800011122",
		):
			self.assertEqual(normalise_mobile(spelling), "9800011122", spelling)

	def test_a_number_too_short_to_be_a_mobile_is_not_one(self):
		"""Better to match nobody than to match everybody on a 4-digit extension."""
		for junk in ("", None, "12345", "n/a", "+91"):
			self.assertIsNone(normalise_mobile(junk))

	def test_a_longer_international_number_keeps_its_last_ten(self):
		self.assertEqual(normalise_mobile("+1 (415) 555-0134"), "4155550134")


class TestFindingTheNumber(FrappeTestCase):
	"""Shopify often leaves the customer's phone blank and puts it on the address."""

	def test_the_customers_own_phone_wins(self):
		self.assertEqual(
			mobile_from({"phone": "+919800011122"}, {"shippingAddress": {"phone": "9000000000"}}),
			"9800011122",
		)

	def test_the_shipping_address_is_used_when_the_customer_has_none(self):
		self.assertEqual(
			mobile_from({"phone": None}, {"shippingAddress": {"phone": "+91 98000 11122"}}),
			"9800011122",
		)

	def test_the_billing_address_is_the_last_resort(self):
		self.assertEqual(
			mobile_from({}, {"shippingAddress": {}, "billingAddress": {"phone": "9800011122"}}),
			"9800011122",
		)

	def test_no_number_anywhere_is_none(self):
		self.assertIsNone(mobile_from({"phone": ""}, {"shippingAddress": {"phone": None}}))


class TestFindingTheCustomer(FrappeTestCase):
	"""The lookup has to see numbers wherever ERPNext actually keeps them."""

	def setUp(self):
		self.made = []

	def tearDown(self):
		for doctype, name in reversed(self.made):
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_missing=True)
		frappe.db.commit()

	def _customer(self, name):
		doc = frappe.new_doc("Customer")
		doc.customer_name = name
		doc.customer_group = frappe.get_all("Customer Group", filters={"is_group": 0}, limit=1, pluck="name")[
			0
		]
		doc.territory = frappe.get_all("Territory", filters={"is_group": 0}, limit=1, pluck="name")[0]
		doc.insert(ignore_permissions=True)
		self.made.append(("Customer", doc.name))
		return doc.name

	def _contact_with_phone(self, customer, phone):
		doc = frappe.new_doc("Contact")
		doc.first_name = f"{customer} contact"[:140]
		doc.append("phone_nos", {"phone": phone, "is_primary_mobile_no": 1})
		doc.append("links", {"link_doctype": "Customer", "link_name": customer})
		doc.insert(ignore_permissions=True)
		self.made.append(("Contact", doc.name))
		return doc.name

	def test_a_customer_is_found_through_their_contacts_phone(self):
		"""The real-world case: Customer.mobile_no is a read-only fetched field and is empty on
		most customers, so the contact's phone rows are the only place the number lives."""
		customer = self._customer("ZZ Mobile Walkin")
		self._contact_with_phone(customer, "+91 98000 11122")
		self.assertEqual(find_customer_by_mobile("9800011122"), customer)

	def test_a_differently_formatted_number_still_finds_them(self):
		customer = self._customer("ZZ Mobile Formats")
		self._contact_with_phone(customer, "098000-22233")
		self.assertEqual(find_customer_by_mobile("9800022233"), customer)

	def test_an_unknown_number_finds_nobody(self):
		self.assertIsNone(find_customer_by_mobile("9111100000"))

	def test_a_placeholder_number_on_many_customers_is_refused(self):
		"""A shop's own landline, or the 9999999999 staff type past a required field. Matching
		on it would merge every online shopper onto one record."""
		for index in range(MAX_CUSTOMERS_PER_MOBILE + 1):
			customer = self._customer(f"ZZ Placeholder {index}")
			self._contact_with_phone(customer, "9999999999")
		self.assertIsNone(find_customer_by_mobile("9999999999"))
