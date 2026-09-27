"""Shopify's new customer accounts sign up with an email and nothing else.

There is nothing to recognise the buyer by, so the app makes a new customer. The shopper then
fills in their name, phone and address on the website profile -- and only then is there enough
to see that this is somebody the shop has known for years. Without acting on that, the account
stays a second, empty record and every website order lands on it.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.inbound.customer import (
	claim_existing_customer,
	customers_matching,
	has_transactions,
)


class _CustomerCase(FrappeTestCase):
	def setUp(self):
		self.made = []

	def tearDown(self):
		for doctype, name in reversed(self.made):
			if doctype == "Customer":
				for contact in frappe.get_all(
					"Contact",
					filters=[
						["Dynamic Link", "link_name", "=", name],
						["Dynamic Link", "link_doctype", "=", "Customer"],
					],
					pluck="name",
				):
					frappe.delete_doc(
						"Contact", contact, force=True, ignore_permissions=True, ignore_missing=True
					)
			try:
				doc = frappe.get_doc(doctype, name)
				if doc.docstatus == 1:
					doc.cancel()
			except Exception:
				pass
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_missing=True)
		frappe.db.commit()

	def customer(self, label, phone=None, email=None, gid=None):
		doc = frappe.new_doc("Customer")
		doc.customer_name = f"ZZ {label} {frappe.generate_hash(length=5)}"
		doc.customer_group = frappe.get_all("Customer Group", filters={"is_group": 0}, limit=1, pluck="name")[
			0
		]
		doc.territory = frappe.get_all("Territory", filters={"is_group": 0}, limit=1, pluck="name")[0]
		if gid:
			doc.shopify_customer_gid = gid
		doc.insert(ignore_permissions=True)
		self.made.append(("Customer", doc.name))

		if phone or email:
			contact = frappe.new_doc("Contact")
			contact.first_name = doc.customer_name[:140]
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


class TestWhetherACustomerHasAHistory(_CustomerCase):
	def test_a_brand_new_customer_has_none(self):
		self.assertFalse(has_transactions(self.customer("Empty")))

	def test_a_customer_with_a_submitted_order_has_one(self):
		customer = self.customer("Busy")
		company = frappe.get_all("Company", limit=1, pluck="name")[0]
		warehouse = frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")
		item = frappe.get_all("Item", filters={"has_variants": 0, "is_stock_item": 1}, limit=1, pluck="name")
		if not (warehouse and item):
			self.skipTest("no warehouse or item on this site")

		so = frappe.new_doc("Sales Order")
		so.customer = customer
		so.company = company
		so.currency = frappe.get_cached_value("Company", company, "default_currency")
		so.conversion_rate = 1
		so.transaction_date = frappe.utils.nowdate()
		so.delivery_date = frappe.utils.add_days(frappe.utils.nowdate(), 3)
		so.append(
			"items",
			{
				"item_code": item[0],
				"qty": 1,
				"rate": 10,
				"warehouse": warehouse,
				"delivery_date": so.delivery_date,
			},
		)
		so.insert(ignore_permissions=True)
		so.submit()
		self.made.insert(0, ("Sales Order", so.name))
		frappe.db.commit()

		self.assertTrue(has_transactions(customer))


class TestClaiming(_CustomerCase):
	GID = "gid://shopify/Customer/57034"

	def test_an_empty_account_is_moved_onto_the_customer_it_belongs_to(self):
		"""The regression: CUST-57034 created for an email-only signup, while CUST-01304
		already held that phone."""
		known = self.customer("Known", phone="7499441808")
		empty = self.customer("Signup", email="socials@example.com", gid=self.GID)

		claimed = claim_existing_customer("Test Store A", empty, {"id": self.GID, "phone": "7499441808"})
		frappe.db.commit()

		self.assertEqual(claimed, known)
		self.assertEqual(frappe.db.get_value("Customer", known, "shopify_customer_gid"), self.GID)
		self.assertFalse(frappe.db.get_value("Customer", empty, "shopify_customer_gid"))
		self.assertTrue(frappe.db.get_value("Customer", empty, "disabled"))

	def test_the_email_works_as_the_second_key(self):
		known = self.customer("KnownByMail", email="known@example.com")
		empty = self.customer("Signup", gid=self.GID)

		claimed = claim_existing_customer(
			"Test Store A", empty, {"id": self.GID, "email": "known@example.com"}
		)
		frappe.db.commit()
		self.assertEqual(claimed, known)

	def test_nothing_is_claimed_when_there_is_no_match(self):
		empty = self.customer("Signup", gid=self.GID)
		self.assertIsNone(
			claim_existing_customer("Test Store A", empty, {"id": self.GID, "phone": "9111100000"})
		)
		self.assertFalse(frappe.db.get_value("Customer", empty, "disabled"))

	def test_a_customer_with_a_history_is_never_given_up(self):
		"""Merging two histories is not something to do on a guess."""
		self.customer("Known", phone="7499441809")
		busy = self.customer("HasOrders", phone="7499441809", gid=self.GID)

		from unittest.mock import patch

		with patch("shopify_integration.inbound.customer.has_transactions", return_value=True):
			self.assertIsNone(
				claim_existing_customer("Test Store A", busy, {"id": self.GID, "phone": "7499441809"})
			)
		self.assertFalse(frappe.db.get_value("Customer", busy, "disabled"))

	def test_a_target_that_already_has_a_shopify_account_is_left_alone(self):
		"""Two real accounts. A person decides, not this."""
		other = self.customer("AlsoOnShopify", phone="7499441810", gid="gid://shopify/Customer/99999")
		empty = self.customer("Signup", gid=self.GID)

		self.assertIsNone(
			claim_existing_customer("Test Store A", empty, {"id": self.GID, "phone": "7499441810"})
		)
		self.assertEqual(
			frappe.db.get_value("Customer", other, "shopify_customer_gid"),
			"gid://shopify/Customer/99999",
		)
		self.assertFalse(frappe.db.get_value("Customer", empty, "disabled"))

	def test_the_customer_itself_is_never_its_own_match(self):
		empty = self.customer("Signup", phone="7499441811", gid=self.GID)
		self.assertEqual(customers_matching({"phone": "7499441811"}, exclude=empty), [])
		self.assertIsNone(
			claim_existing_customer("Test Store A", empty, {"id": self.GID, "phone": "7499441811"})
		)


class TestTheUpdateHandlerUsesIt(FrappeTestCase):
	"""The wiring: profile details have to be written and the claim attempted."""

	def test_the_handler_writes_the_profile_and_tries_to_claim(self):
		import inspect

		from shopify_integration.inbound import customer as customer_module

		source = inspect.getsource(customer_module._upsert_customer)
		self.assertIn("enrich_contact", source, "the profile's phone and email must be written")
		self.assertIn("write_profile_address", source, "and its default address")
		self.assertIn("claim_existing_customer", source, "and the account matched to a real customer")
