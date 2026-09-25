"""Customer, Address and Contact from Shopify (spec §8.2, §8.3).

The one rule that matters: never create two ERPNext Customers for the same Shopify customer
GID. Duplicates here are not cosmetic -- they split a buyer's history across two ledgers and
are painful to merge after the fact.

The GID only recognises someone who has bought here online before. A shopper who already buys
from you in person and then signs up on the website is a *new* Shopify customer with a fresh
GID, so matching on the GID alone books them a second ERPNext record and splits the very
history this module exists to keep together. Where a store says so, the mobile number is used
as the second key -- see ``find_customer_by_mobile``.

Guest checkouts carry no customer at all, and fall back to the store's default customer.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr

from shopify_integration.catalogue.echo import inbound_write, mark
from shopify_integration.inbound.webhook import payload_of

#: An identifier on more than this many customers is a placeholder, not a person -- a shop's
#: own landline, the 9999999999 typed past a required field, or info@ on a company account.
#: Matching on one would merge every online shopper onto a single record, far worse than a
#: duplicate, so past this count we decline to match at all.
MAX_CUSTOMERS_PER_IDENTIFIER = 3


def normalise_mobile(raw) -> str | None:
	"""The last 10 digits of a phone number, or None if there are not 10.

	One person's number reaches us spelled a dozen ways -- ``+919800011122``, ``09800011122``,
	``+91 98000 11122``, ``98000-11122`` -- and ERPNext stores whatever was typed. Comparing the
	last 10 digits is what makes those the same number. Ten because that is an Indian mobile;
	the country code and any trunk prefix sit in front of it.
	"""
	digits = "".join(character for character in cstr(raw) if character.isdigit())
	return digits[-10:] if len(digits) >= 10 else None


def normalise_email(raw) -> str | None:
	"""An email lowercased and trimmed, or None if it is not one.

	Strict enough that a fragment cannot become a matching key: ``@gmail.com`` has an at-sign
	and a dotted domain but no person in front of it, and matching customers on it would put
	every buyer who left the field half-filled onto one record.
	"""
	email = cstr(raw).strip().lower()
	if email.count("@") != 1:
		return None
	local, _, domain = email.partition("@")
	if not local or not domain or domain.startswith(".") or domain.endswith("."):
		return None
	return email if "." in domain else None


def customer_mobile(shopify_customer: dict) -> str | None:
	"""The buyer's own mobile.

	Deliberately *only* the customer record's phone. The number on a shipping or billing
	address is not reliably the buyer -- it is as often a receptionist, a neighbour taking
	delivery, or the person a gift is going to -- and matching a customer on it merges people
	who never met.
	"""
	return normalise_mobile(shopify_customer.get("phone"))


def customer_email(shopify_customer: dict) -> str | None:
	return normalise_email(shopify_customer.get("email"))


def _matched(customers: list[str], identifier: str, kind: str) -> str | None:
	"""One customer from a match, or None when the identifier is too common to trust."""
	customers = [customer for customer in customers if customer]
	if not customers:
		return None
	if len(customers) > MAX_CUSTOMERS_PER_IDENTIFIER:
		frappe.logger("shopify_integration").warning(
			f"{kind} {identifier} is on {len(customers)} customers; treating it as a placeholder "
			"and not matching on it."
		)
		return None
	# Oldest wins: the record carrying the longest history is the one worth keeping.
	return customers[0] if len(customers) == 1 else _oldest(customers)


def find_customer_by_mobile(number: str) -> str | None:
	"""An existing Customer reachable on this mobile, or None.

	Both places ERPNext keeps a number are searched. ``Customer.mobile_no`` is a read-only
	field fetched from the primary contact, so it is empty on every customer that has no
	primary contact set -- which, on a real site, is most of them. The contact's own phone rows
	are where the number actually lives, so they are the authority and the customer field is
	the cheap first look. ERPNext's own POS writes both, which is what lets a counter sale and
	a web order find each other.

	Compared on the last 10 digits, in SQL, so one query does the whole catalogue.
	"""
	if not number:
		return None

	matches = frappe.db.sql(
		"""
		SELECT DISTINCT link.link_name AS customer
		FROM `tabContact Phone` phone
		JOIN `tabDynamic Link` link
		  ON link.parent = phone.parent
		 AND link.parenttype = 'Contact'
		 AND link.link_doctype = 'Customer'
		WHERE RIGHT(REGEXP_REPLACE(phone.phone, '[^0-9]', ''), 10) = %(number)s

		UNION

		SELECT name AS customer
		FROM `tabCustomer`
		WHERE RIGHT(REGEXP_REPLACE(IFNULL(mobile_no, ''), '[^0-9]', ''), 10) = %(number)s
		""",
		{"number": number},
		as_dict=True,
	)
	return _matched([row.customer for row in matches], number, "Mobile")


def find_customer_by_email(email: str) -> str | None:
	"""An existing Customer reachable at this email, or None.

	The weaker of the two keys, and only ever tried after the mobile finds nobody: a household
	shares an email far more readily than a mobile, and a company account's info@ address would
	otherwise sweep every buyer from that company onto one record. The placeholder guard is
	what stops that becoming a silent merge.
	"""
	if not email:
		return None

	matches = frappe.db.sql(
		"""
		SELECT DISTINCT link.link_name AS customer
		FROM `tabContact Email` mail
		JOIN `tabDynamic Link` link
		  ON link.parent = mail.parent
		 AND link.parenttype = 'Contact'
		 AND link.link_doctype = 'Customer'
		WHERE LOWER(TRIM(mail.email_id)) = %(email)s

		UNION

		SELECT name AS customer
		FROM `tabCustomer`
		WHERE LOWER(TRIM(IFNULL(email_id, ''))) = %(email)s
		""",
		{"email": email},
		as_dict=True,
	)
	return _matched([row.customer for row in matches], email, "Email")


def _oldest(customers: list[str]) -> str | None:
	rows = frappe.get_all(
		"Customer", filters={"name": ["in", customers]}, fields=["name"], order_by="creation asc", limit=1
	)
	return rows[0].name if rows else None


def resolve_customer(store_doc, order: dict) -> str:
	"""The ERPNext Customer for an order, creating one if this buyer is new.

	Two keys, in order. The Shopify GID first: it is exact, and unlike an email it cannot be
	changed by the shopper or shared by a household. Then, where the store allows it, the
	mobile number -- which is what recognises a buyer who already shops here in person and has
	just signed up online for the first time.
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

	if not existing and store_doc.get("match_existing_customers"):
		existing = _match_existing(shopify_customer)
		if existing:
			# Claiming the record -- stamping this GID on it -- is what makes the search happen
			# once per person rather than once per order.
			frappe.db.set_value("Customer", existing, "shopify_customer_gid", gid, update_modified=False)
			with inbound_write():
				enrich_contact(existing, shopify_customer)

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


def _match_existing(shopify_customer: dict) -> str | None:
	"""The customer this buyer already is, found by mobile and then by email.

	Mobile first because it is the stronger claim, and because it is the one your counter staff
	actually collect. Email is tried only when the mobile finds nobody.

	When the two disagree -- the mobile points at one customer and the email at another -- the
	mobile wins and the clash is logged rather than resolved. Two records that each match on a
	different identifier are two records a human should look at; welding them together on a
	guess is the one outcome worse than a duplicate.
	"""
	number = customer_mobile(shopify_customer)
	email = customer_email(shopify_customer)

	by_mobile = find_customer_by_mobile(number) if number else None
	by_email = find_customer_by_email(email) if email else None

	if by_mobile and by_email and by_mobile != by_email:
		frappe.logger("shopify_integration").warning(
			f"Shopify buyer matches {by_mobile} on mobile and {by_email} on email. Using the "
			f"mobile match; the two records may be the same person and worth merging by hand."
		)
		return by_mobile

	matched = by_mobile or by_email
	if matched:
		found_on = "mobile" if by_mobile else "email"
		frappe.logger("shopify_integration").info(
			f"Matched Shopify buyer to existing customer {matched} on {found_on}"
		)
	return matched


def enrich_contact(customer: str, shopify_customer: dict) -> None:
	"""Add the identifier the existing record was missing. Never overwrite one it has.

	A walk-in matched on their mobile often arrives with the email nobody ever asked them for
	at the counter, and vice versa. Adding it is what lets the *next* order match even when the
	shopper gives only the other one -- the two channels teach each other who this person is.

	Only ever appends. A value your staff typed is never replaced by one from Shopify.
	"""
	email = customer_email(shopify_customer)
	number = customer_mobile(shopify_customer)
	if not email and not number:
		return

	contact_name = (
		frappe.db.get_value("Customer", customer, "customer_primary_contact")
		or (
			frappe.get_all(
				"Contact",
				filters=[
					["Dynamic Link", "link_name", "=", customer],
					["Dynamic Link", "link_doctype", "=", "Customer"],
				],
				order_by="creation asc",
				limit=1,
				pluck="name",
			)
			or [None]
		)[0]
	)
	if not contact_name:
		_write_contact(customer, shopify_customer)
		return

	contact = frappe.get_doc("Contact", contact_name)
	changed = False

	if email and not any(normalise_email(row.email_id) == email for row in contact.email_ids):
		contact.append("email_ids", {"email_id": cstr(shopify_customer.get("email")).strip()})
		changed = True

	if number and not any(normalise_mobile(row.phone) == number for row in contact.phone_nos):
		contact.append("phone_nos", {"phone": cstr(shopify_customer.get("phone")).strip()})
		changed = True

	if changed:
		mark(contact)
		contact.save(ignore_permissions=True)


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
	"""The person behind the customer: their email, and the number they are found by.

	Only the buyer's own phone and email. An address phone is not reliably the buyer -- it is
	as often a receptionist, a neighbour taking delivery, or the person a gift is going to --
	and recording it here would have the next order match the wrong human entirely.
	"""
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
		# is_primary_mobile_no, not just is_primary_phone. ``Customer.mobile_no`` is a read-only
		# field fetched from the primary contact's *mobile*, so marking it only as the primary
		# phone leaves the customer's mobile blank for ever -- which is why no customer this app
		# created could be found by number.
		doc.append("phone_nos", {"phone": phone, "is_primary_phone": 1, "is_primary_mobile_no": 1})
	doc.append("links", {"link_doctype": "Customer", "link_name": customer})
	mark(doc)
	doc.insert(ignore_permissions=True)

	# Without a primary contact there is nothing for ``mobile_no`` to be fetched from. Only set
	# when the customer has none, so a contact chosen by hand is never quietly replaced.
	if phone and not frappe.db.get_value("Customer", customer, "customer_primary_contact"):
		frappe.db.set_value(
			"Customer",
			customer,
			{"customer_primary_contact": doc.name, "mobile_no": phone},
			update_modified=False,
		)
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
