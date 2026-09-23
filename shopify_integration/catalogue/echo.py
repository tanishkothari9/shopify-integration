"""Echo suppression (spec §8.7).

An Item written from a Shopify webhook must not immediately push itself back to Shopify.
Without this you get Shopify -> ERPNext -> Shopify forever; the spec calls it "the single
most common bug in bidirectional integrations" and it is.

Two mechanisms, deliberately, because one is not enough:

* ``doc.flags.from_shopify`` -- what the spec prescribes. Precise, but only covers the exact
  document object the inbound writer touched.
* ``frappe.flags.shopify_inbound`` -- a request/job-wide flag set by the ``inbound_write``
  context manager. Catches the writes the first mechanism misses: variant Items saved while
  building a template, an Item Group or Supplier created on the way, and anything a future
  handler adds without remembering the per-document flag.

The outbound hook checks both. A missed suppression is an infinite loop against a live shop,
so the belt and the braces are both worth their cost.
"""

from __future__ import annotations

from contextlib import contextmanager

import frappe


@contextmanager
def inbound_write():
	"""Mark everything saved inside this block as originating from Shopify."""
	previous = getattr(frappe.flags, "shopify_inbound", False)
	frappe.flags.shopify_inbound = True
	try:
		yield
	finally:
		frappe.flags.shopify_inbound = previous


def mark(doc) -> None:
	"""Flag one document as Shopify-originated before it is saved."""
	doc.flags.from_shopify = True


def is_echo(doc) -> bool:
	"""True when this save came from Shopify and must not be pushed back."""
	return bool(getattr(doc.flags, "from_shopify", False)) or bool(
		getattr(frappe.flags, "shopify_inbound", False)
	)
