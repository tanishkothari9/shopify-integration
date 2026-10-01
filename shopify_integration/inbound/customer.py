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

#: An identifier on more than this many *active* customers is a placeholder, not a person --
#: a shop's own landline, the 9999999999 typed past a required field, or info@ on a company
#: account. Matching on one would merge every online shopper onto a single record, far worse
#: than a duplicate, so past this count we decline to match at all.
#:
#: Disabled customers are excluded from the count and from the candidates. They were not, and
#: since claiming an account disables the empty duplicate it left behind, this app was
#: manufacturing the very records that pushed a real number over the threshold: 8459867853 sat
#: on four customers of which two were disabled, so a genuine shopper was read as a
#: placeholder and given a fifth record.
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


def _customers_on(rows) -> list[str]:
	return [row.customer for row in rows if row.customer]


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
		JOIN `tabCustomer` customer
		  ON customer.name = link.link_name
		 AND IFNULL(customer.disabled, 0) = 0
		WHERE RIGHT(REGEXP_REPLACE(phone.phone, '[^0-9]', ''), 10) = %(number)s

		UNION

		SELECT name AS customer
		FROM `tabCustomer`
		WHERE RIGHT(REGEXP_REPLACE(IFNULL(mobile_no, ''), '[^0-9]', ''), 10) = %(number)s
		  AND IFNULL(disabled, 0) = 0
		""",
		{"number": number},
		as_dict=True,
	)
	return _matched(_customers_on(matches), number, "Mobile")


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
		JOIN `tabCustomer` customer
		  ON customer.name = link.link_name
		 AND IFNULL(customer.disabled, 0) = 0
		WHERE LOWER(TRIM(mail.email_id)) = %(email)s

		UNION

		SELECT name AS customer
		FROM `tabCustomer`
		WHERE LOWER(TRIM(IFNULL(email_id, ''))) = %(email)s
		  AND IFNULL(disabled, 0) = 0
		""",
		{"email": email},
		as_dict=True,
	)
	return _matched(_customers_on(matches), email, "Email")


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
		# _write_address matches on content, so a repeat order to the same place reuses the
		# address and a new place gets a new one.
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
				# Qualified: filtering on Dynamic Link makes Frappe join `tabDynamic Link`, and
				# both tables carry a `creation` column, so a bare one is rejected outright --
				# "Column 'creation' in ORDER BY is ambiguous".
				order_by="`tabContact`.creation asc",
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


def order_addresses(customer: str, order: dict) -> dict:
	"""The Address records *this* order ships and bills to.

	Returned so the documents can point at them. A repeat customer ordering somewhere new was
	otherwise given the address from their first order for ever: the parcel would go to the
	wrong place, and -- because place of supply follows the address -- an order to another
	state was refused outright by india_compliance with "Cannot charge IGST for intra-state
	supplies".
	"""
	with inbound_write():
		return {
			"shipping": _write_address(customer, order.get("shippingAddress"), "Shipping"),
			"billing": _write_address(customer, order.get("billingAddress"), "Billing"),
		}


def _address_values(address: dict) -> dict:
	"""Shopify's address in ERPNext's field names, trimmed to their column widths."""
	return {
		"address_line1": cstr(address.get("address1"))[:240],
		"address_line2": cstr(address.get("address2"))[:240] or None,
		"city": cstr(address.get("city"))[:100] or "Unknown",
		"state": cstr(address.get("province"))[:100] or None,
		"pincode": cstr(address.get("zip"))[:20] or None,
		"country": _country(address),
		"phone": cstr(address.get("phone"))[:20] or None,
	}


#: The fields that decide whether two addresses are the same place.
ADDRESS_FIELDS = ("address_line1", "address_line2", "city", "state", "pincode", "country", "phone")


def _address_key(values: dict) -> str:
	"""A comparable form of an address: case, spacing and punctuation do not make it new."""
	return "|".join(" ".join(cstr(values.get(field)).split()).casefold() for field in ADDRESS_FIELDS)


def _matching_address(customer: str, values: dict, address_type: str) -> str | None:
	"""An address of this customer that is already this place, or None.

	Matched on content rather than on a title, which is what the first version did -- and a
	title is the same for every address a customer ever has, so the second one was thrown away.
	"""
	rows = frappe.db.sql(
		"""
		SELECT a.name, a.address_line1, a.address_line2, a.city, a.state, a.pincode,
		       a.country, a.phone
		FROM `tabAddress` a
		JOIN `tabDynamic Link` link
		  ON link.parent = a.name
		 AND link.parenttype = 'Address'
		 AND link.link_doctype = 'Customer'
		WHERE link.link_name = %(customer)s
		  AND a.address_type = %(address_type)s
		  AND IFNULL(a.disabled, 0) = 0
		""",
		{"customer": customer, "address_type": address_type},
		as_dict=True,
	)

	wanted = _address_key(values)
	for row in rows:
		if _address_key(row) == wanted:
			return row.name
	return None


def _write_address(customer: str, address: dict | None, address_type: str) -> str | None:
	"""This order's address: the one the customer already has, or a new one."""
	if not address or not cstr(address.get("address1")).strip():
		return None

	values = _address_values(address)
	existing = _matching_address(customer, values, address_type)
	if existing:
		return existing

	doc = frappe.new_doc("Address")
	# The city in the title so a customer with several can tell them apart on the form.
	# ERPNext appends the type, and a counter if that still collides.
	doc.address_title = f"{customer}-{values['city']}"[:140]
	doc.address_type = address_type
	for field in ADDRESS_FIELDS:
		setattr(doc, field, values[field])
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


#: Documents whose existence means a customer has a history worth keeping.
TRANSACTION_DOCTYPES = ("Sales Order", "Sales Invoice", "Delivery Note", "Payment Entry")


def has_transactions(customer: str) -> bool:
	"""Whether anything has been booked against this customer, cancelled or not."""
	return any(
		frappe.db.exists(doctype, {"customer": customer, "docstatus": ["!=", 0]})
		for doctype in TRANSACTION_DOCTYPES
	)


def customers_matching(shopify_customer: dict, exclude: str) -> list[str]:
	"""Other customers reachable on this buyer's mobile, or failing that their email."""
	number = customer_mobile(shopify_customer)
	email = customer_email(shopify_customer)

	by_mobile = [c for c in (find_customer_by_mobile(number) or "",) if c and c != exclude] if number else []
	if by_mobile:
		return by_mobile
	return [c for c in (find_customer_by_email(email) or "",) if c and c != exclude] if email else []


def claim_existing_customer(store: str, created: str, shopify_customer: dict) -> str | None:
	"""Move a Shopify account onto the customer it turns out to belong to.

	Shopify's new customer accounts sign up with an email and nothing else, so the app has no
	way to recognise the buyer and makes a new customer. The shopper then fills in their name,
	phone and address on the website profile -- and only then is there enough to see that this
	is somebody the shop has known for years. Without this the account stays a second, empty
	record, and every order from the website lands on it.

	The empty record is only ever the one given up. A customer with anything booked against it
	keeps its account, because merging two histories is not something to do on a guess; that
	case is logged for a person to look at instead.
	"""
	if has_transactions(created):
		return None

	candidates = customers_matching(shopify_customer, exclude=created)
	if len(candidates) != 1:
		return None

	target = candidates[0]
	gid = cstr(shopify_customer.get("id"))

	if frappe.db.get_value("Customer", target, "shopify_customer_gid"):
		frappe.logger("shopify_integration").warning(
			f"{created} looks like {target}, but {target} already has a Shopify account. "
			"Leaving both alone for someone to merge by hand."
		)
		return None

	with inbound_write():
		frappe.db.set_value("Customer", created, "shopify_customer_gid", None, update_modified=False)
		frappe.db.set_value("Customer", target, "shopify_customer_gid", gid, update_modified=False)

		empty = frappe.get_doc("Customer", created)
		empty.disabled = 1
		mark(empty)
		empty.save(ignore_permissions=True)
		empty.add_comment(
			"Comment",
			f"Disabled by the Shopify integration: this was an empty account created before the "
			f"shopper filled in their profile, and it is the same person as {target}, which now "
			f"holds the Shopify account.",
		)

		enrich_contact(target, shopify_customer)

	frappe.logger("shopify_integration").info(
		f"Moved Shopify customer {gid} from the empty {created} to {target}"
	)
	return target


def write_profile_address(customer: str, payload: dict) -> str | None:
	"""The address a shopper filled in on their website profile.

	customers/update carries it as `default_address` in REST's spelling, which is not the
	shape the address writer reads.
	"""
	rest = payload.get("default_address") or {}
	if not rest:
		return None

	from shopify_integration.inbound.order import _address_from_payload

	address = _address_from_payload(rest)
	if not address:
		return None

	with inbound_write():
		return _write_address(customer, address, "Shipping")


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
		result = {}
		if existing:
			with inbound_write():
				doc = frappe.get_doc("Customer", existing)
				doc.customer_name = customer_name(shopify_customer)
				mark(doc)
				doc.save(ignore_permissions=True)
				# The details a shopper adds to their profile after signing up. Without these
				# the phone and address they typed never leave Shopify, and the account stays
				# unrecognisable as somebody the shop already knows.
				enrich_contact(existing, shopify_customer)
			write_profile_address(existing, payload)

			claimed = claim_existing_customer(log.store, existing, shopify_customer)
			name = claimed or existing
			if claimed:
				result["merged_into"] = claimed
				result["disabled"] = existing
		else:
			name = resolve_customer(store_doc, {"customer": shopify_customer})
			write_profile_address(name, payload)

		frappe.db.commit()
		result["customer"] = name
		log.mark_success(
			ref_doctype="Customer",
			ref_docname=name,
			result=(f"merged {existing} into {name}" if result.get("merged_into") else None),
		)
		return result
	except Exception:
		log.mark_error(frappe.get_traceback())
		raise
