"""Shopify states every timestamp in UTC. ERPNext dates documents by the merchant's calendar.

The gap between those two is a real day boundary for part of every day, and crossing it does
more than mis-date a document: a return dated before the Delivery Note it returns is refused
by ERPNext outright.
"""

from __future__ import annotations

from datetime import date, time, timedelta
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.tests.test_orders import OrderTestCase, build_order
from shopify_integration.utils.timestamps import shopify_date, shopify_datetime, shopify_time


class TestReadingAShopifyTimestamp(FrappeTestCase):
	"""One instant, read in whichever time zone the site is configured for."""

	#: 2026-09-26T20:22:49Z is 01:52:49 on the 27th in Kolkata, 16:22:49 on the 26th in New York.
	INSTANT = "2026-09-26T20:22:49Z"

	def test_east_of_utc_the_date_rolls_forward(self):
		with patch("frappe.utils.data.get_system_timezone", return_value="Asia/Kolkata"):
			self.assertEqual(
				shopify_datetime(self.INSTANT).replace(microsecond=0).isoformat(),
				"2026-09-27T01:52:49",
			)

	def test_west_of_utc_the_same_instant_is_the_day_before(self):
		with patch("frappe.utils.data.get_system_timezone", return_value="America/New_York"):
			self.assertEqual(
				shopify_datetime(self.INSTANT).replace(microsecond=0).isoformat(),
				"2026-09-26T16:22:49",
			)

	def test_utc_is_left_where_it_is(self):
		with patch("frappe.utils.data.get_system_timezone", return_value="UTC"):
			self.assertEqual(
				shopify_datetime(self.INSTANT).replace(microsecond=0).isoformat(),
				"2026-09-26T20:22:49",
			)

	def test_an_explicit_offset_is_honoured_not_ignored(self):
		"""REST webhook bodies carry an offset rather than a Z, and it is the same instant."""
		with patch("frappe.utils.data.get_system_timezone", return_value="Asia/Kolkata"):
			self.assertEqual(
				shopify_datetime("2026-09-27T01:52:49+05:30").replace(microsecond=0).isoformat(),
				"2026-09-27T01:52:49",
			)
			self.assertEqual(
				shopify_datetime("2026-09-26T16:22:49-04:00").replace(microsecond=0).isoformat(),
				"2026-09-27T01:52:49",
			)

	def test_a_value_with_no_zone_is_read_as_utc(self):
		"""What Shopify means by a bare timestamp, defensively handled."""
		with patch("frappe.utils.data.get_system_timezone", return_value="Asia/Kolkata"):
			self.assertEqual(shopify_date("2026-09-26 20:22:49"), date(2026, 9, 27))

	def test_nothing_in_nothing_out(self):
		for empty in (None, "", 0):
			self.assertIsNone(shopify_datetime(empty))
			self.assertIsNone(shopify_date(empty))
			self.assertIsNone(shopify_time(empty))

	def test_the_date_and_time_helpers_agree_with_the_datetime(self):
		with patch("frappe.utils.data.get_system_timezone", return_value="Asia/Kolkata"):
			self.assertEqual(shopify_date(self.INSTANT), date(2026, 9, 27))
			self.assertEqual(shopify_time(self.INSTANT).replace(microsecond=0), time(1, 52, 49))

	def test_the_site_time_zone_is_what_decides_it(self):
		"""Nothing is hard-coded: the answer follows System Settings."""
		with patch("frappe.utils.data.get_system_timezone", return_value="Pacific/Kiritimati"):
			self.assertEqual(shopify_date(self.INSTANT), date(2026, 9, 27))
		with patch("frappe.utils.data.get_system_timezone", return_value="Pacific/Midway"):
			self.assertEqual(shopify_date(self.INSTANT), date(2026, 9, 26))


class TestTheDayBoundaryInPractice(FrappeTestCase):
	"""The shapes that actually reach the mappers."""

	def test_an_order_placed_just_after_local_midnight_is_dated_today(self):
		"""00:30 IST on the 27th is 19:00Z on the 26th. Dating it by UTC loses a day."""
		with patch("frappe.utils.data.get_system_timezone", return_value="Asia/Kolkata"):
			self.assertEqual(shopify_date("2026-09-26T19:00:00Z"), date(2026, 9, 27))

	def test_an_order_placed_in_the_evening_west_of_utc_is_not_dated_tomorrow(self):
		"""20:00 in New York on the 26th is 00:00Z on the 27th."""
		with patch("frappe.utils.data.get_system_timezone", return_value="America/New_York"):
			self.assertEqual(shopify_date("2026-09-27T00:00:00Z"), date(2026, 9, 26))

	def test_a_refund_after_midnight_is_not_dated_before_the_delivery_it_returns(self):
		"""The failure that is more than cosmetic. Goods delivered 14:00 local on the 26th,
		refunded 01:52 local on the 27th. Read as UTC the return lands on the 26th at 20:22 --
		before the despatch it reverses -- and ERPNext refuses it with "Posting timestamp must
		be after". Read locally it is the 27th, comfortably after."""
		with patch("frappe.utils.data.get_system_timezone", return_value="Asia/Kolkata"):
			delivered_on, delivered_at = date(2026, 9, 26), time(14, 0)
			returned_on = shopify_date("2026-09-26T20:22:49Z")
			returned_at = shopify_time("2026-09-26T20:22:49Z")

			self.assertEqual(returned_on, date(2026, 9, 27))
			self.assertGreater(
				frappe.utils.get_datetime(f"{returned_on} {returned_at}"),
				frappe.utils.get_datetime(f"{delivered_on} {delivered_at}"),
			)

	def test_month_end_activity_stays_in_its_own_month(self):
		"""01:00 IST on 1 October is 19:30Z on 30 September. A GST period hangs on this."""
		with patch("frappe.utils.data.get_system_timezone", return_value="Asia/Kolkata"):
			booked = shopify_date("2026-09-30T19:30:00Z")
			self.assertEqual(booked, date(2026, 10, 1))
			self.assertEqual(booked.month, 10)


class TestTheDocumentsThemselves(OrderTestCase):
	"""Not just the helper -- what date actually lands on the document.

	The helper being right is worth nothing if a mapper still reads the raw value, so these go
	through the real mapping code and read the saved document back.
	"""

	def setUp(self):
		self.made = []

	def tearDown(self):
		for doctype, name in reversed(self.made):
			try:
				doc = frappe.get_doc(doctype, name)
				if doc.docstatus == 1:
					doc.cancel()
			except Exception:
				pass
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_missing=True)
		frappe.db.commit()

	def _order_placed_at(self, utc_stamp, gid):
		# INR because the company on this site is INR, and ERPNext refuses a receivable in
		# another currency. Nothing here depends on the currency; it just has to agree.
		company_currency = frappe.get_cached_value("Company", self.store_doc.company, "default_currency")
		order = build_order(gid=gid, name="#TZ1", currency=company_currency)
		order["createdAt"] = utc_stamp
		order["processedAt"] = utc_stamp
		return order

	def test_an_order_just_after_local_midnight_is_dated_the_local_day(self):
		"""19:00Z on the 26th is 00:30 on the 27th in Kolkata. Dated by UTC it loses a day, and
		with it a day of reporting and, at month end, a GST period."""
		from shopify_integration.inbound import order as order_module

		order = self._order_placed_at("2026-09-26T19:00:00Z", "gid://shopify/Order/770001")
		with patch("frappe.utils.data.get_system_timezone", return_value="Asia/Kolkata"):
			name = order_module.create_sales_order(self.store_doc, order)
			self.made.append(("Sales Order", name))
			self.assertEqual(frappe.db.get_value("Sales Order", name, "transaction_date"), date(2026, 9, 27))

	def test_the_invoice_carries_the_local_date_and_the_local_time(self):
		"""posting_time too, not just the date -- a document timed by UTC can sort before the
		one it depends on even when the date is right."""
		from shopify_integration.inbound import order as order_module

		order = self._order_placed_at("2026-09-26T19:00:00Z", "gid://shopify/Order/770003")
		with patch("frappe.utils.data.get_system_timezone", return_value="Asia/Kolkata"):
			name = order_module.create_sales_order(self.store_doc, order)
			self.made.append(("Sales Order", name))

			result = order_module.create_sales_invoice(self.store_doc, order)
			invoice = result["sales_invoice"]
			self.made.append(("Sales Invoice", invoice))
			if result.get("payment_entry"):
				self.made.insert(0, ("Payment Entry", result["payment_entry"]))

			saved = frappe.db.get_value(
				"Sales Invoice", invoice, ["posting_date", "posting_time"], as_dict=True
			)
			self.assertEqual(saved.posting_date, date(2026, 9, 27))
			# Frappe hands a Time field back as a timedelta, not a time.
			self.assertEqual(saved.posting_time, timedelta(hours=0, minutes=30))

	def test_the_same_instant_west_of_utc_is_dated_the_day_before(self):
		"""Proving the fix follows the site rather than favouring one direction."""
		from shopify_integration.inbound import order as order_module

		order = self._order_placed_at("2026-09-27T00:30:00Z", "gid://shopify/Order/770002")
		with patch("frappe.utils.data.get_system_timezone", return_value="America/New_York"):
			name = order_module.create_sales_order(self.store_doc, order)
			self.made.append(("Sales Order", name))
			self.assertEqual(frappe.db.get_value("Sales Order", name, "transaction_date"), date(2026, 9, 26))
