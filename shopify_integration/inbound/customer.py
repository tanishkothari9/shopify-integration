"""Customer, Address and Contact from Shopify (spec §8.2, §8.3).

The one rule that matters: never create two ERPNext Customers for the same Shopify customer
GID. Duplicates here are not cosmetic -- they split a buyer's history across two ledgers and
are painful to merge after the fact.

Guest checkouts carry no customer at all, and fall back to the store's default customer.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr

from shopify_integration.catalogue.echo import inbound_write, mark
from shopify_integration.inbound.webhook import payload_of


def resolve_customer(store_doc, order: dict) -> str:
	"""The ERPNext Customer for an order, creating one if this buyer is new.

	Matching is on the Shopify GID rather than on email: a shopper can change their email,
	and two different people can share one (a household, a company address).
	"""
	shopify_customer = order.get("customer") or {}
	gid = cstr(shopify_customer.get("id"))

	if not gid:
		if not store_doc.default_customer:
			frappe.throw(
				frappe._("Order {0} is a guest checkout but store {1} has no Default Customer set.").format(
					order.get("name") or order.get("id"), store_doc.name
				)
			)
		return store_doc.default_customer

	existing = frappe.db.get_value("Customer", {"shopify_customer_gid": gid}, "name")
	if existing:
		# Shopify fires customers/create a fraction of a second before orders/create, so by the
		# time an order is handled its buyer usually exists already -- created from a customer
		# payload, which carries no address. Returning here without writing the order's
		# addresses is why every webhook-created customer lost theirs, permanently and silently:
		# addresses were only ever written on the branch that creates the customer, and that
		# branch had already been taken by the other webhook.
		#
		# _write_address dedupes on title, so a repeat customer does not accumulate copies.
		with inbound_write():
			_write_address(existing, order.get("shippingAddress"), "Shipping")
			_write_address(existing, order.get("billingAddress"), "Billing")
		return existing

	with inbound_write():
		customer = frappe.new_doc("Customer")
		customer.customer_name = customer_name(shopify_customer, order)
		customer.customer_group = _customer_group_for(store_doc)
		customer.customer_type = "Individual"
		customer.territory = default_territory()
		customer.shopify_customer_gid = gid
		mark(customer)
		customer.insert(ignore_permissions=True)

		_write_address(customer.name, order.get("shippingAddress"), "Shipping")
		_write_address(customer.name, order.get("billingAddress"), "Billing")
		_write_contact(customer.name, shopify_customer)

	return customer.name


def customer_name(shopify_customer: dict, order: dict | None = None) -> str:
	"""A display name, falling back through what Shopify actually provides.

	Shopify customers can have no name at all -- an email-only checkout, say -- so this walks
	down to the email and finally to the GID rather than creating a blank customer.
	"""
	parts = [
		cstr(shopify_customer.get("firstName")).strip(),
		cstr(shopify_customer.get("lastName")).strip(),
	]
	full = " ".join(p for p in parts if p).strip()
	if full:
		return full[:140]

	email = cstr(shopify_customer.get("email")).strip()
	if email:
		return email[:140]

	gid = cstr(shopify_customer.get("id"))
	return f"Shopify Customer {gid.rstrip('/').split('/')[-1]}"[:140]


def _customer_group_for(store_doc) -> str:
	"""The store's customer group, if it is one a Customer can actually hold.

	ERPNext keeps a global default of `customer_group = All Customer Groups`, and Frappe
	fills that into any new document with a matching Link field -- including ours, without
	anyone choosing it. But `All Customer Groups` is the *root* of the tree, and
	`Customer.validate_customer_group` refuses a group node outright. Left alone, a fresh
	install imports no orders at all: the store looks correctly configured, and every
	customer throws "Cannot select a Group type Customer Group".

	So a group node here means nobody picked anything, and we fall back.
	"""
	chosen = store_doc.get("customer_group")
	if chosen and not frappe.db.get_value("Customer Group", chosen, "is_group"):
		return chosen
	return default_customer_group()


def default_customer_group() -> str:
	"""A leaf group. Never the root -- a Customer cannot hold a group node."""
	if frappe.db.get_value("Customer Group", "Individual", "is_group") == 0:
		return "Individual"
	return frappe.db.get_value("Customer Group", {"is_group": 0}, "name")


def default_territory() -> str:
	"""Likewise a leaf: `All Territories` is the root of its own tree."""
	if frappe.db.get_value("Territory", "Rest Of The World", "is_group") == 0:
		return "Rest Of The World"
	return frappe.db.get_value("Territory", {"is_group": 0}, "name")


def _write_address(customer: str, address: dict | None, address_type: str) -> str | None:
	if not address or not cstr(address.get("address1")).strip():
		return None

	title = f"{customer}-{address_type}"[:140]
	if frappe.db.exists("Address", {"address_title": title, "address_type": address_type}):
		return None

	doc = frappe.new_doc("Address")
	doc.address_title = title
	doc.address_type = address_type
	doc.address_line1 = cstr(address.get("address1"))[:240]
	doc.address_line2 = cstr(address.get("address2"))[:240] or None
	doc.city = cstr(address.get("city"))[:100] or "Unknown"
	doc.state = cstr(address.get("province"))[:100] or None
	doc.pincode = cstr(address.get("zip"))[:20] or None
	doc.country = _country(address)
	doc.phone = cstr(address.get("phone"))[:20] or None
	doc.append("links", {"link_doctype": "Customer", "link_name": customer})
	mark(doc)
	doc.insert(ignore_permissions=True)
	return doc.name


def _country(address: dict) -> str:
	"""Map Shopify's country onto an ERPNext Country, falling back rather than failing.

	An unrecognised country must not block an order: the address is reference data, and the
	sale is the thing that matters.
	"""
	name = cstr(address.get("country")).strip()
	if name and frappe.db.exists("Country", name):
		return name

	code = cstr(address.get("countryCodeV2")).strip()
	if code:
		by_code = frappe.db.get_value("Country", {"code": code.lower()}, "name")
		if by_code:
			return by_code

	return frappe.db.get_default("country") or "United States"


def _write_contact(customer: str, shopify_customer: dict) -> str | None:
	email = cstr(shopify_customer.get("email")).strip()
	phone = cstr(shopify_customer.get("phone")).strip()
	if not email and not phone:
		return None

	first = cstr(shopify_customer.get("firstName")).strip() or customer
	doc = frappe.new_doc("Contact")
	doc.first_name = first[:140]
	doc.last_name = cstr(shopify_customer.get("lastName")).strip()[:140] or None
	if email:
		doc.append("email_ids", {"email_id": email, "is_primary": 1})
	if phone:
		doc.append("phone_nos", {"phone": phone, "is_primary_phone": 1})
	doc.append("links", {"link_doctype": "Customer", "link_name": customer})
	mark(doc)
	doc.insert(ignore_permissions=True)
	return doc.name


# --------------------------------------------------------------------------------------
# Webhook handlers
# --------------------------------------------------------------------------------------


def on_customer_create(event_log: str):
	return _upsert_customer(event_log)


def on_customer_update(event_log: str):
	return _upsert_customer(event_log)


def _upsert_customer(event_log: str):
	"""Create or refresh a Customer from a customers/* webhook."""
	log = frappe.get_doc("Shopify Event Log", event_log)
	try:
		payload = payload_of(event_log)
		gid = payload.get("admin_graphql_api_id") or (
			f"gid://shopify/Customer/{payload['id']}" if payload.get("id") else None
		)
		if not gid:
			log.mark_error("Webhook payload carried no customer id")
			return {"skipped": "no customer id"}

		store_doc = frappe.get_cached_doc("Shopify Store", log.store)
		shopify_customer = {
			"id": gid,
			"firstName": payload.get("first_name"),
			"lastName": payload.get("last_name"),
			"email": payload.get("email"),
			"phone": payload.get("phone"),
		}

		existing = frappe.db.get_value("Customer", {"shopify_customer_gid": gid}, "name")
		if existing:
			with inbound_write():
				doc = frappe.get_doc("Customer", existing)
				doc.customer_name = customer_name(shopify_customer)
				mark(doc)
				doc.save(ignore_permissions=True)
			name = existing
		else:
			name = resolve_customer(store_doc, {"customer": shopify_customer})

		frappe.db.commit()
		log.mark_success(ref_doctype="Customer", ref_docname=name)
		return {"customer": name}
	except Exception:
		log.mark_error(frappe.get_traceback())
		raise
