"""A repeat customer ordering somewhere new must get that address, not their first one.

Addresses were named "<customer>-<type>" and skipped when that title existed, so a customer
only ever kept the first address Shopify sent. The parcel would go to the wrong place -- and
because place of supply follows the address, an order to another state was refused outright
by india_compliance with "Cannot charge IGST for intra-state supplies". Seen on #1123,
shipped to Bengaluru against a customer whose first address was in Nagpur.
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.inbound.customer import (
	_address_key,
	_matching_address,
	_write_address,
	order_addresses,
)


def shopify_address(city="Nagpur", state="Maharashtra", line1="1 First Road", zip_="440001", phone=None):
	return {
		"address1": line1,
		"address2": None,
		"city": city,
		"province": state,
		"zip": zip_,
		"country": "India",
		"countryCodeV2": "IN",
		"phone": phone,
	}


class TestWhatMakesTwoAddressesTheSame(FrappeTestCase):
	def test_case_and_spacing_do_not_make_a_new_address(self):
		a = {"address_line1": "1 First Road", "city": "Nagpur", "state": "Maharashtra"}
		b = {"address_line1": "1  first   ROAD", "city": " nagpur ", "state": "maharashtra"}
		self.assertEqual(_address_key(a), _address_key(b))

	def test_a_different_street_is_a_different_address(self):
		a = {"address_line1": "1 First Road", "city": "Nagpur"}
		b = {"address_line1": "2 First Road", "city": "Nagpur"}
		self.assertNotEqual(_address_key(a), _address_key(b))

	def test_a_different_city_is_a_different_address(self):
		a = {"address_line1": "1 First Road", "city": "Nagpur"}
		b = {"address_line1": "1 First Road", "city": "Bengaluru"}
		self.assertNotEqual(_address_key(a), _address_key(b))

	def test_a_different_pincode_is_a_different_address(self):
		self.assertNotEqual(
			_address_key({"address_line1": "1 A Road", "pincode": "440001"}),
			_address_key({"address_line1": "1 A Road", "pincode": "560001"}),
		)


class TestAddressesForRealOrders(FrappeTestCase):
	def setUp(self):
		self.made = []
		grp = frappe.get_all("Customer Group", filters={"is_group": 0}, limit=1, pluck="name")[0]
		ter = frappe.get_all("Territory", filters={"is_group": 0}, limit=1, pluck="name")[0]
		doc = frappe.new_doc("Customer")
		doc.customer_name = f"ZZ Address Test {frappe.generate_hash(length=6)}"
		doc.customer_group = grp
		doc.territory = ter
		doc.insert(ignore_permissions=True)
		self.customer = doc.name
		self.made.append(("Customer", doc.name))
		frappe.db.commit()

	def tearDown(self):
		for name in frappe.get_all(
			"Address",
			filters=[
				["Dynamic Link", "link_name", "=", self.customer],
				["Dynamic Link", "link_doctype", "=", "Customer"],
			],
			pluck="name",
		):
			frappe.delete_doc("Address", name, force=True, ignore_permissions=True, ignore_missing=True)
		for doctype, name in reversed(self.made):
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_missing=True)
		frappe.db.commit()

	def test_the_first_address_is_created(self):
		name = _write_address(self.customer, shopify_address(), "Shipping")
		self.assertTrue(name)
		self.assertEqual(frappe.db.get_value("Address", name, "city"), "Nagpur")

	def test_the_same_address_again_is_reused(self):
		first = _write_address(self.customer, shopify_address(), "Shipping")
		second = _write_address(self.customer, shopify_address(), "Shipping")
		self.assertEqual(first, second, "the same place must not become a second Address")

	def test_the_same_address_typed_differently_is_still_reused(self):
		first = _write_address(self.customer, shopify_address(), "Shipping")
		second = _write_address(
			self.customer, shopify_address(city=" nagpur ", line1="1  First  Road"), "Shipping"
		)
		self.assertEqual(first, second)

	def test_a_second_address_in_another_state_is_created(self):
		"""The regression. This used to return None, and the order kept the Nagpur address --
		which is what made india_compliance refuse the IGST."""
		nagpur = _write_address(self.customer, shopify_address(), "Shipping")
		bengaluru = _write_address(
			self.customer,
			shopify_address(city="Bengaluru", state="Karnataka", line1="9 Second Road", zip_="560001"),
			"Shipping",
		)

		self.assertTrue(bengaluru)
		self.assertNotEqual(nagpur, bengaluru)
		self.assertEqual(frappe.db.get_value("Address", bengaluru, "state"), "Karnataka")

	def test_the_old_address_is_not_overwritten(self):
		"""Documents already raised keep the address they were raised with."""
		nagpur = _write_address(self.customer, shopify_address(), "Shipping")
		_write_address(
			self.customer,
			shopify_address(city="Bengaluru", state="Karnataka", line1="9 Second Road", zip_="560001"),
			"Shipping",
		)
		self.assertEqual(frappe.db.get_value("Address", nagpur, "city"), "Nagpur")
		self.assertEqual(frappe.db.get_value("Address", nagpur, "state"), "Maharashtra")

	def test_billing_and_shipping_are_kept_apart(self):
		shipping = _write_address(self.customer, shopify_address(), "Shipping")
		billing = _write_address(self.customer, shopify_address(), "Billing")
		self.assertNotEqual(shipping, billing)

	def test_an_order_reports_the_addresses_it_used(self):
		order = {
			"shippingAddress": shopify_address(city="Bengaluru", state="Karnataka", zip_="560001"),
			"billingAddress": shopify_address(),
		}
		found = order_addresses(self.customer, order)
		self.assertEqual(frappe.db.get_value("Address", found["shipping"], "city"), "Bengaluru")
		self.assertEqual(frappe.db.get_value("Address", found["billing"], "city"), "Nagpur")

	def test_an_order_with_no_address_reports_none(self):
		self.assertEqual(
			order_addresses(self.customer, {"shippingAddress": None, "billingAddress": None}),
			{"shipping": None, "billing": None},
		)

	def test_a_matching_address_is_found_for_the_right_customer_only(self):
		mine = _write_address(self.customer, shopify_address(), "Shipping")
		self.assertEqual(
			_matching_address(
				self.customer,
				{
					"address_line1": "1 First Road",
					"address_line2": None,
					"city": "Nagpur",
					"state": "Maharashtra",
					"pincode": "440001",
					"country": "India",
					"phone": None,
				},
				"Shipping",
			),
			mine,
		)
		self.assertIsNone(
			_matching_address(
				"ZZ Somebody Else", {"address_line1": "1 First Road", "city": "Nagpur"}, "Shipping"
			)
		)


class TestTheOrderUsesThisOrdersAddress(FrappeTestCase):
	def test_the_sales_order_is_pointed_at_them(self):
		"""The wiring: without this the documents fall back to the customer's primary address."""
		import inspect

		from shopify_integration.inbound import order as order_module

		source = inspect.getsource(order_module.create_sales_order)
		self.assertIn("order_addresses", source)
		self.assertIn("shipping_address_name", source)
		self.assertIn("customer_address", source)
