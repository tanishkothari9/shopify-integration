"""Matching a Shopify buyer to the customer you already have.

Two counters, one person. The shop collects a mobile and rarely an email; the website collects
an email and sometimes a mobile. The Shopify GID only recognises someone who has bought online
here before, so on its own it books a walk-in's first web order to a second record and splits
their history. Mobile is tried first, then email.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.inbound.customer import (
	MAX_CUSTOMERS_PER_IDENTIFIER,
	_match_existing,
	_write_contact,
	customer_email,
	customer_mobile,
	enrich_contact,
	find_customer_by_email,
	find_customer_by_mobile,
	normalise_email,
	normalise_mobile,
)


class TestNormalising(FrappeTestCase):
	def test_the_same_number_written_five_ways_is_one_number(self):
		for spelling in ("+919800011122", "09800011122", "+91 98000 11122", "98000-11122", "9800011122"):
			self.assertEqual(normalise_mobile(spelling), "9800011122", spelling)

	def test_a_number_too_short_to_be_a_mobile_is_not_one(self):
		for junk in ("", None, "12345", "n/a", "+91"):
			self.assertIsNone(normalise_mobile(junk))

	def test_an_email_is_lowercased_and_trimmed(self):
		self.assertEqual(normalise_email("  Ramesh.Kumar@Gmail.COM "), "ramesh.kumar@gmail.com")

	def test_something_that_is_not_an_email_is_rejected(self):
		for junk in ("", None, "ramesh", "ramesh@", "@gmail.com", "ramesh@gmail"):
			self.assertIsNone(normalise_email(junk))


class TestOnlyTheBuyersOwnDetailsCount(FrappeTestCase):
	"""An address phone is as often a receptionist, a neighbour, or a gift recipient."""

	def test_the_buyers_own_phone_is_read(self):
		self.assertEqual(customer_mobile({"phone": "+919800011122"}), "9800011122")

	def test_an_address_phone_is_never_read(self):
		self.assertIsNone(customer_mobile({"phone": None, "shippingAddress": {"phone": "9800011122"}}))

	def test_the_buyers_own_email_is_read(self):
		self.assertEqual(customer_email({"email": "R.Kumar@Example.com"}), "r.kumar@example.com")


class _CustomerCase(FrappeTestCase):
	def setUp(self):
		self.made = []

	def tearDown(self):
		for doctype, name in reversed(self.made):
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_missing=True)
		frappe.db.commit()

	def customer(self, name, phone=None, email=None):
		"""A customer the way the counter makes one: details live on a linked Contact."""
		doc = frappe.new_doc("Customer")
		doc.customer_name = name
		doc.customer_group = frappe.get_all("Customer Group", filters={"is_group": 0}, limit=1, pluck="name")[
			0
		]
		doc.territory = frappe.get_all("Territory", filters={"is_group": 0}, limit=1, pluck="name")[0]
		doc.insert(ignore_permissions=True)
		self.made.append(("Customer", doc.name))

		if phone or email:
			contact = frappe.new_doc("Contact")
			contact.first_name = name[:140]
			if phone:
				contact.append("phone_nos", {"phone": phone, "is_primary_mobile_no": 1})
			if email:
				contact.append("email_ids", {"email_id": email, "is_primary": 1})
			contact.append("links", {"link_doctype": "Customer", "link_name": doc.name})
			contact.insert(ignore_permissions=True)
			self.made.append(("Contact", contact.name))
			frappe.db.set_value(
				"Customer", doc.name, "customer_primary_contact", contact.name, update_modified=False
			)
		frappe.db.commit()
		return doc.name


class TestFindingByMobile(_CustomerCase):
	def test_a_walk_in_is_found_by_the_number_the_counter_took(self):
		customer = self.customer("ZZ Walkin Ramesh", phone="+91 98000 11122")
		self.assertEqual(find_customer_by_mobile("9800011122"), customer)

	def test_an_unknown_number_finds_nobody(self):
		self.assertIsNone(find_customer_by_mobile("9111100000"))

	def test_a_placeholder_number_on_many_customers_is_refused(self):
		for index in range(MAX_CUSTOMERS_PER_IDENTIFIER + 1):
			self.customer(f"ZZ Placeholder {index}", phone="9999999999")
		self.assertIsNone(find_customer_by_mobile("9999999999"))


class TestFindingByEmail(_CustomerCase):
	def test_a_customer_is_found_by_the_email_on_their_contact(self):
		customer = self.customer("ZZ Email Only", email="ramesh@example.com")
		self.assertEqual(find_customer_by_email("ramesh@example.com"), customer)

	def test_case_and_spacing_do_not_matter(self):
		customer = self.customer("ZZ Email Case", email="Ramesh.K@Example.com")
		self.assertEqual(find_customer_by_email("ramesh.k@example.com"), customer)

	def test_a_shared_company_address_is_refused(self):
		"""info@ on a company account would otherwise sweep every buyer onto one record."""
		for index in range(MAX_CUSTOMERS_PER_IDENTIFIER + 1):
			self.customer(f"ZZ Shared Inbox {index}", email="info@company.example.com")
		self.assertIsNone(find_customer_by_email("info@company.example.com"))


class TestWhichKeyWins(_CustomerCase):
	def test_the_mobile_is_tried_first(self):
		customer = self.customer("ZZ Both Keys", phone="9800022233", email="both@example.com")
		self.assertEqual(_match_existing({"phone": "+91 98000 22233", "email": "both@example.com"}), customer)

	def test_email_is_used_when_the_buyer_gave_no_phone(self):
		"""The website signup that never asked for a number."""
		customer = self.customer("ZZ Mail Signup", phone="9800033344", email="signup@example.com")
		self.assertEqual(_match_existing({"phone": None, "email": "signup@example.com"}), customer)

	def test_two_different_customers_are_not_welded_together(self):
		"""Mobile points at one, email at another. Use the mobile; a human decides the rest."""
		by_phone = self.customer("ZZ Clash Phone", phone="9800044455")
		self.customer("ZZ Clash Mail", email="clash@example.com")
		self.assertEqual(_match_existing({"phone": "9800044455", "email": "clash@example.com"}), by_phone)

	def test_a_buyer_with_neither_matches_nobody(self):
		self.assertIsNone(_match_existing({"phone": None, "email": None}))


class TestEnrichingWhatWeLearn(_CustomerCase):
	"""The counter has no email, the website has no number. Each order teaches the record."""

	def test_the_email_from_shopify_is_added_to_a_walk_in(self):
		customer = self.customer("ZZ Learns Email", phone="9800055566")
		enrich_contact(customer, {"phone": "9800055566", "email": "learned@example.com"})
		frappe.db.commit()
		self.assertEqual(find_customer_by_email("learned@example.com"), customer)

	def test_the_phone_from_shopify_is_added_to_a_web_signup(self):
		customer = self.customer("ZZ Learns Phone", email="weblearn@example.com")
		enrich_contact(customer, {"phone": "+91 98000 66677", "email": "weblearn@example.com"})
		frappe.db.commit()
		self.assertEqual(find_customer_by_mobile("9800066677"), customer)

	def test_nothing_your_staff_typed_is_replaced(self):
		customer = self.customer("ZZ Keeps Staff Value", phone="9800077788")
		contact = frappe.db.get_value("Customer", customer, "customer_primary_contact")
		enrich_contact(customer, {"phone": "9800099900", "email": None})
		frappe.db.commit()
		phones = frappe.get_all("Contact Phone", filters={"parent": contact}, pluck="phone")
		self.assertIn("9800077788", phones)
		self.assertIn("9800099900", phones)

	def test_a_value_already_present_is_not_duplicated(self):
		customer = self.customer("ZZ No Dupes", phone="9800088899", email="nodupe@example.com")
		contact = frappe.db.get_value("Customer", customer, "customer_primary_contact")
		enrich_contact(customer, {"phone": "+91 98000 88899", "email": "NoDupe@Example.com"})
		frappe.db.commit()
		self.assertEqual(frappe.db.count("Contact Phone", {"parent": contact}), 1)
		self.assertEqual(frappe.db.count("Contact Email", {"parent": contact}), 1)


class TestWritingANewCustomersContact(_CustomerCase):
	def test_a_new_buyer_is_findable_by_both_keys_afterwards(self):
		customer = self.customer("ZZ Brand New")
		contact = _write_contact(
			customer,
			{"firstName": "Priya", "lastName": "S", "email": "priya@example.com", "phone": "9777700001"},
		)
		self.made.append(("Contact", contact))
		frappe.db.commit()
		self.assertEqual(find_customer_by_mobile("9777700001"), customer)
		self.assertEqual(find_customer_by_email("priya@example.com"), customer)

	def test_the_customers_mobile_field_is_populated_so_pos_search_finds_them(self):
		"""``Customer.mobile_no`` is a search field, and it is fetched from the primary contact.
		Without that link the record is invisible to a counter search for the number."""
		customer = self.customer("ZZ POS Findable")
		contact = _write_contact(customer, {"firstName": "Arun", "phone": "9777700002"})
		self.made.append(("Contact", contact))
		frappe.db.commit()
		self.assertEqual(frappe.db.get_value("Customer", customer, "customer_primary_contact"), contact)
		self.assertTrue(frappe.db.get_value("Customer", customer, "mobile_no"))
