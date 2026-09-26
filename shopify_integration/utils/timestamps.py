"""Shopify timestamps, read in the site's own time zone.

Shopify's GraphQL Admin API states every timestamp in UTC -- ``2026-09-26T20:22:49Z`` --
whatever time zone the store is configured in. Taking the date straight off that value dates
the document by UTC's calendar rather than the merchant's, and the two disagree for part of
every day:

* **East of UTC.** On an Asia/Kolkata site (UTC+5:30) everything between midnight and 05:30
  local is dated the *previous* day. A refund at 01:52 IST on the 27th was booked on the 26th.
* **West of UTC.** On America/New_York, evening activity is dated the *next* day.

Two consequences, and the second is worse than a wrong date. A return dated before the
Delivery Note it returns is refused outright by ERPNext -- "Posting timestamp must be after
..." -- so refunding an order delivered earlier the same local day fails. And a document
booked across midnight on the last of the month lands in the previous GST period.

The time zone is never hard-coded: it comes from System Settings, so the app is correct for a
site anywhere.
"""

from __future__ import annotations

from datetime import datetime

from frappe.utils import convert_utc_to_system_timezone, get_datetime


def shopify_datetime(value) -> datetime | None:
	"""A Shopify timestamp as a naive datetime in the site's time zone, or None.

	Handles the three shapes that reach us: ``...Z`` from GraphQL, an explicit offset such as
	``+05:30`` from a REST webhook body, and -- defensively -- a value with no zone at all,
	which is read as UTC because that is what Shopify means by it.

	Naive on the way out because that is what Frappe stores in a Datetime field; an aware value
	would be compared against naive ones elsewhere and raise.
	"""
	if not value:
		return None

	moment = get_datetime(value)
	if moment is None:
		return None

	return convert_utc_to_system_timezone(moment).replace(tzinfo=None)


def shopify_date(value):
	"""The calendar date a Shopify timestamp falls on, here. None stays None."""
	moment = shopify_datetime(value)
	return moment.date() if moment else None


def shopify_time(value):
	"""The wall-clock time a Shopify timestamp falls on, here. None stays None."""
	moment = shopify_datetime(value)
	return moment.time() if moment else None
