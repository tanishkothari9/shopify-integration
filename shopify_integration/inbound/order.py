"""Orders: Shopify -> ERPNext (spec §8.3 - §8.5).

Four handlers, one shared builder. Every one of them is idempotent, because Shopify delivers
at least once and a Retry replays the same payload on purpose:

* ``orders/create``              -> Sales Order
* ``orders/paid``                -> Sales Invoice + Payment Entry
* ``orders/fulfilled`` and
  ``orders/partially_fulfilled`` -> Delivery Note, one per Shopify fulfilment
* ``orders/cancelled``           -> cancel the linked documents

Submitting the Sales Order is load-bearing beyond the order itself: it raises ``reserved_qty``,
which is what keeps phase 4's inventory push from re-exposing stock that is already sold.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr, get_datetime

from shopify_integration.api.client import ShopifyClient, load_query
from shopify_integration.catalogue.echo import inbound_write, mark
from shopify_integration.inbound.customer import resolve_customer
from shopify_integration.inbound.webhook import payload_of
from shopify_integration.utils.money import quantize, to_float
from shopify_integration.utils.taxes import (
	assert_total_matches,
	build_shipping_row,
	build_tax_rows,
	conversion_rate,
	item_tax_template_for,
	list_rate,
	money_field,
	money_side_for,
	net_shipping_total,
	shipping_tax_template,
	shipping_titles,
	unit_rate,
)

#: Shopify financial statuses that mean money has actually arrived.
PAID_STATUSES = ("PAID", "PARTIALLY_PAID")


# --------------------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------------------


def on_order_create(event_log: str):
	"""orders/create -> Sales Order."""
	log = frappe.get_doc("Shopify Event Log", event_log)
	try:
		store_doc = frappe.get_cached_doc("Shopify Store", log.store)
		if not store_doc.sync_orders:
			log.mark_success()
			return {"skipped": "sync_orders is off"}

		order = fetch_order(log.store, payload_of(event_log))
		if order is None:
			log.mark_success()
			return {"skipped": "order no longer exists"}

		name = create_sales_order(store_doc, order)
		frappe.db.commit()
		log.mark_success(ref_doctype="Sales Order", ref_docname=name)
		return {"sales_order": name}
	except Exception:
		log.mark_error(frappe.get_traceback())
		raise


def on_order_paid(event_log: str):
	"""orders/paid -> Sales Invoice, then a Payment Entry for what was actually received."""
	log = frappe.get_doc("Shopify Event Log", event_log)
	try:
		store_doc = frappe.get_cached_doc("Shopify Store", log.store)
		if not store_doc.sync_invoices:
			log.mark_success()
			return {"skipped": "sync_invoices is off"}

		order = fetch_order(log.store, payload_of(event_log))
		if order is None:
			log.mark_success()
			return {"skipped": "order no longer exists"}

		result = create_sales_invoice(store_doc, order)
		frappe.db.commit()
		log.mark_success(ref_doctype="Sales Invoice", ref_docname=result["sales_invoice"])
		return result
	except Exception:
		log.mark_error(frappe.get_traceback())
		raise


def on_order_fulfilled(event_log: str):
	"""orders/fulfilled and orders/partially_fulfilled -> a Delivery Note per fulfilment."""
	log = frappe.get_doc("Shopify Event Log", event_log)
	try:
		store_doc = frappe.get_cached_doc("Shopify Store", log.store)
		if not store_doc.sync_delivery_notes:
			log.mark_success()
			return {"skipped": "sync_delivery_notes is off"}

		order = fetch_order(log.store, payload_of(event_log))
		if order is None:
			log.mark_success()
			return {"skipped": "order no longer exists"}

		created = create_delivery_notes(store_doc, order)
		frappe.db.commit()
		log.mark_success(ref_doctype="Delivery Note", ref_docname=", ".join(created) if created else None)
		return {"delivery_notes": created}
	except Exception:
		log.mark_error(frappe.get_traceback())
		raise


def on_order_cancelled(event_log: str):
	"""orders/cancelled -> cancel the linked documents, newest dependency first."""
	log = frappe.get_doc("Shopify Event Log", event_log)
	try:
		order = fetch_order(log.store, payload_of(event_log))
		gid = (order or {}).get("id") or _order_gid(payload_of(event_log))
		cancelled = cancel_linked_documents(log.store, gid)
		frappe.db.commit()
		log.mark_success(ref_doctype="Sales Order", ref_docname=", ".join(cancelled) or None)
		return {"cancelled": cancelled}
	except Exception:
		log.mark_error(frappe.get_traceback())
		raise


# --------------------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------------------


def _order_gid(payload: dict) -> str | None:
	gid = payload.get("admin_graphql_api_id")
	if gid:
		return gid
	numeric = payload.get("id")
	return f"gid://shopify/Order/{numeric}" if numeric else None


def fetch_order(store: str, payload: dict) -> dict | None:
	"""Refetch the order over GraphQL. See order_by_id.graphql for why."""
	gid = _order_gid(payload)
	if not gid:
		frappe.throw(_("Webhook payload carried no order id"))

	client = ShopifyClient.for_store(store)
	data = client.execute(load_query("order_by_id"), {"id": gid}, cost_hint=20)
	order = data.get("order")
	if not order:
		return None

	_backfill_addresses(order, payload)

	line_page = order.get("lineItems") or {}
	if (line_page.get("pageInfo") or {}).get("hasNextPage"):
		frappe.throw(
			_(
				"Shopify order {0} has more than 250 line items, which this version does not "
				"page through. Raise it with the maintainers rather than booking a partial order."
			).format(order.get("name") or gid)
		)
	return order


#: The webhook body spells address fields in REST's snake_case; the GraphQL order spells them
#: in camelCase. Only the fields the address writer reads are translated.
_REST_ADDRESS_FIELDS = {
	"firstName": "first_name",
	"lastName": "last_name",
	"address1": "address1",
	"address2": "address2",
	"city": "city",
	"province": "province",
	"provinceCode": "province_code",
	"zip": "zip",
	"country": "country",
	"countryCodeV2": "country_code",
	"phone": "phone",
}


def _address_from_payload(rest: dict | None) -> dict | None:
	"""Translate a REST webhook address into the shape the rest of the app expects."""
	if not rest or not cstr(rest.get("address1")).strip():
		return None
	return {camel: rest.get(snake) for camel, snake in _REST_ADDRESS_FIELDS.items()}


def _backfill_addresses(order: dict, payload: dict) -> None:
	"""Fill in addresses the refetch did not return.

	Shopify's Admin API is eventually consistent. An order refetched immediately after
	``orders/create`` -- which is exactly when this runs -- comes back with `customer` populated
	but `shippingAddress` and `billingAddress` still null. Nothing errors: the customer is
	created without an address, and the loss is silent and permanent, because the addresses are
	only ever written when the customer is new.

	The webhook body is the event's own snapshot of the order and always carries them, so it is
	the fallback. A refetch that *does* return an address always wins, since it is the newer
	read.
	"""
	for graphql_key, rest_key in (
		("shippingAddress", "shipping_address"),
		("billingAddress", "billing_address"),
	):
		if not order.get(graphql_key):
			recovered = _address_from_payload(payload.get(rest_key))
			if recovered:
				order[graphql_key] = recovered


# --------------------------------------------------------------------------------------
# Sales Order
# --------------------------------------------------------------------------------------


def company_cost_center(company: str) -> str | None:
	"""The company's default cost centre.

	ERPNext rejects a document whose rows carry no cost centre, and a store that has not been
	configured with one would otherwise fail every order with a message about a cost centre
	the user never set.
	"""
	return frappe.get_cached_value("Company", company, "cost_center") or frappe.db.get_value(
		"Cost Center", {"company": company, "is_group": 0}, "name"
	)


def ensure_price_list(store_doc) -> str:
	"""The price list Shopify orders are booked against.

	Uses the store's configured list when there is one, and otherwise creates a dedicated
	list named after the store. Auto-creating rather than refusing: a missing price list is a
	setup detail the integration can settle for itself, and failing here would surface as
	ERPNext's opaque "selling_price_list is mandatory" rather than anything actionable.
	"""
	if store_doc.selling_price_list:
		return store_doc.selling_price_list

	name = f"Shopify - {store_doc.name}"[:140]
	if frappe.db.exists("Price List", name):
		return name

	# ERPNext promotes a new selling price list to the site-wide default when none is set
	# (Price List.set_default_if_missing). On a site that has not been fully configured, that
	# would quietly make a Shopify-specific list the default for all manual selling too, so
	# whatever Selling Settings had before is restored afterwards.
	previous_default = frappe.db.get_single_value("Selling Settings", "selling_price_list")

	with inbound_write():
		price_list = frappe.new_doc("Price List")
		price_list.price_list_name = name
		price_list.selling = 1
		price_list.enabled = 1
		price_list.currency = frappe.get_cached_value("Company", store_doc.company, "default_currency")
		mark(price_list)
		price_list.insert(ignore_permissions=True)

	if frappe.db.get_single_value("Selling Settings", "selling_price_list") != previous_default:
		frappe.db.set_single_value("Selling Settings", "selling_price_list", previous_default)
		frappe.db.set_default("selling_price_list", previous_default)
	return price_list.name


def existing_sales_order(store: str, order_gid: str) -> str | None:
	"""The Sales Order already booked for this Shopify order, at any docstatus.

	Deliberately not filtered to ``docstatus < 2``. The unique index does not exclude
	cancelled documents either, so filtering them out here made the Python check and the
	database constraint disagree: a redelivered webhook for a cancelled order passed this
	check, attempted an insert, and surfaced a raw IntegrityError instead of the clean no-op
	that idempotency is meant to provide.
	"""
	return frappe.db.get_value(
		"Sales Order", {"shopify_store": store, "shopify_order_gid": order_gid}, "name"
	)


def usable_sales_order(store: str, order_gid: str) -> str:
	"""A **submitted** Sales Order for this Shopify order, creating one if there is none.

	Separate from ``existing_sales_order`` on purpose, because the two answer different
	questions. "Has this order been booked before?" must match the unique index exactly, so it
	counts cancelled documents. "Can I build an invoice or delivery on it?" must not -- a
	cancelled order cannot carry either, and quietly building against one produces documents
	that look right and reconcile against nothing.
	"""
	name = existing_sales_order(store, order_gid)
	if not name:
		return None

	if frappe.db.get_value("Sales Order", name, "docstatus") == 1:
		return name

	frappe.throw(
		_(
			"Shopify order {0} already has Sales Order {1}, which is cancelled. Amend or delete "
			"it before this order can be invoiced or delivered."
		).format(order_gid, name)
	)


def create_sales_order(store_doc, order: dict) -> str:
	"""Build and submit a Sales Order. Re-delivery returns the existing one (spec §8.3)."""
	order_gid = order["id"]
	existing = existing_sales_order(store_doc.name, order_gid)
	if existing:
		return existing

	company_currency = frappe.get_cached_value("Company", store_doc.company, "default_currency")
	side = money_side_for(order, company_currency)
	currency, rate = conversion_rate(order, company_currency)

	with inbound_write():
		so = frappe.new_doc("Sales Order")
		so.customer = resolve_customer(store_doc, order)
		so.company = store_doc.company
		so.shopify_store = store_doc.name
		so.shopify_order_gid = order_gid
		so.shopify_order_number = cstr(order.get("name"))
		so.currency = currency
		so.conversion_rate = to_float(rate)
		so.transaction_date = get_datetime(order.get("createdAt")).date()
		so.delivery_date = so.transaction_date
		so.set_warehouse = store_doc.default_warehouse

		# A dedicated price list, so Shopify's prices never leak into the price list used by
		# the rest of the business (spec §8.3). Combined with ignore_pricing_rule, this is what
		# guarantees ERPNext cannot rewrite a price the customer has already been charged.
		so.selling_price_list = ensure_price_list(store_doc)
		so.price_list_currency = currency
		so.plc_conversion_rate = to_float(rate)
		so.cost_center = store_doc.cost_center or company_cost_center(store_doc.company)
		if store_doc.sales_order_series:
			so.naming_series = store_doc.sales_order_series

		# Shopify's price is authoritative. ERPNext pricing rules must never rewrite the price
		# a customer has already been charged (spec §8.3).
		so.ignore_pricing_rule = 1

		_add_line_items(so, store_doc, order, side)
		_add_charges(so, store_doc, order, side)

		mark(so)
		so.insert(ignore_permissions=True)
		so.reload()
		assert_total_matches(so, order, side)

		# Submitting raises reserved_qty, which phase 4's inventory push depends on.
		so.submit()

		note = cstr(order.get("note")).strip()
		if note:
			so.add_comment("Comment", text=_("Shopify order note: {0}").format(note))

	return so.name


def _add_line_items(doc, store_doc, order: dict, side: str) -> None:
	for line in (order.get("lineItems") or {}).get("nodes") or []:
		quantity = line.get("quantity") or 0
		if quantity <= 0:
			continue

		item_code = resolve_item(store_doc, line, order)

		row = {
			"item_code": item_code,
			"item_name": cstr(line.get("title"))[:140] or item_code,
			"qty": quantity,
			# Shopify has already spread order-level discounts across lines; take its answer.
			# Not quantized to 2dp: ERPNext multiplies rate by qty, so rounding the rate
			# first turns a half-cent per unit into a whole cent on the line. Let ERPNext
			# round the resulting amount at its own precision instead.
			"rate": to_float(unit_rate(line, side)),
			"price_list_rate": to_float(list_rate(line, side)),
			"warehouse": store_doc.default_warehouse,
		}
		cost_center = store_doc.cost_center or company_cost_center(store_doc.company)
		if cost_center:
			row["cost_center"] = cost_center

		# Each line carries its own rate, so an order mixing a 5% saree with an 18% kurti is
		# taxed correctly line by line rather than at a blend of the two.
		template = item_tax_template_for(store_doc, line, order, side)
		if template:
			row["item_tax_template"] = template

		doc.append("items", row)

	if not doc.get("items"):
		frappe.throw(
			_("Shopify order {0} produced no usable line items.").format(order.get("name") or order.get("id"))
		)


def _add_charges(doc, store_doc, order: dict, side: str) -> None:
	"""Shipping and tax rows. Shipping is a line item or a charge, per store config."""
	taxes_included = bool(order.get("taxesIncluded"))
	shipping = net_shipping_total(order, side, taxes_included)
	if shipping and store_doc.shipping_item:
		row = {
			"item_code": store_doc.shipping_item,
			"item_name": shipping_titles(order)[:140],
			"qty": 1,
			"rate": to_float(shipping),
			"warehouse": store_doc.default_warehouse,
		}
		# Freight carries its own rate, exactly like any other line. Without this ERPNext
		# falls back to whatever the order's tax rows state and bills shipping at the wrong
		# slab -- see shipping_tax_template.
		template = shipping_tax_template(store_doc, order, side)
		if template:
			row["item_tax_template"] = template
		doc.append("items", row)
	elif shipping:
		row = build_shipping_row(store_doc, order, side)
		if row:
			doc.append("taxes", row)

	for row in build_tax_rows(store_doc, order, side):
		doc.append("taxes", row)


def resolve_item(store_doc, line: dict, order: dict) -> str:
	"""Map a Shopify order line to an ERPNext item code.

	Falls back through variant GID then SKU, and finally creates the item on demand when the
	store allows it. The failure message names the SKU, because "sync failed" tells a user
	nothing they can act on (spec §14).
	"""
	from shopify_integration.shopify_integration.doctype.shopify_item_link.shopify_item_link import (
		get_link,
	)

	variant_gid = cstr((line.get("variant") or {}).get("id")) or None
	sku = cstr(line.get("sku")).strip() or None

	link = get_link(store_doc.name, variant_gid=variant_gid, sku=sku)
	if link:
		return link["item_code"]

	if sku and frappe.db.exists("Item", sku):
		return sku

	if store_doc.create_missing_items and variant_gid:
		return _create_missing_item(store_doc, line, variant_gid, sku)

	frappe.throw(
		_(
			"Order {0} has a line for SKU '{1}' which has no Shopify item mapping for store "
			"{2}. Import the catalogue, or enable Create Missing Items on the store."
		).format(order.get("name") or order.get("id"), sku or "(no SKU)", store_doc.name)
	)


def _create_missing_item(store_doc, line: dict, variant_gid: str, sku: str | None) -> str:
	"""Lazy path from §8.7: fetch the product and run it through the one mapping writer."""
	from shopify_integration.catalogue.mapping import write_product_mapping

	product_gid = _product_gid_for_variant(store_doc.name, variant_gid)
	client = ShopifyClient.for_store(store_doc.name)
	data = client.execute(load_query("product_by_id"), {"id": product_gid}, cost_hint=10)
	product = data.get("product")
	if not product:
		frappe.throw(
			_("Could not fetch Shopify product for SKU '{0}' to create the missing item.").format(
				sku or variant_gid
			)
		)

	product["variants"] = [edge["node"] for edge in ((product.get("variants") or {}).get("edges") or [])]
	write_product_mapping(store_doc.name, product)

	from shopify_integration.shopify_integration.doctype.shopify_item_link.shopify_item_link import (
		get_link,
	)

	link = get_link(store_doc.name, variant_gid=variant_gid, sku=sku)
	if not link:
		frappe.throw(
			_("Created the Shopify product mapping but found no link for SKU '{0}'.").format(
				sku or variant_gid
			)
		)
	return link["item_code"]


def _product_gid_for_variant(store: str, variant_gid: str) -> str:
	"""The product a variant belongs to, from an existing link or by asking Shopify."""
	known = frappe.db.get_value(
		"Shopify Item Link", {"store": store, "variant_gid": variant_gid}, "product_gid"
	)
	if known:
		return known

	client = ShopifyClient.for_store(store)
	data = client.execute(
		"query variantProduct($id: ID!) { productVariant(id: $id) { product { id } } }",
		{"id": variant_gid},
		cost_hint=5,
	)
	product = ((data.get("productVariant") or {}).get("product") or {}).get("id")
	if not product:
		frappe.throw(_("Shopify variant {0} has no product.").format(variant_gid))
	return product


# --------------------------------------------------------------------------------------
# Sales Invoice and Payment Entry
# --------------------------------------------------------------------------------------


def create_sales_invoice(store_doc, order: dict) -> dict:
	"""Invoice the order, then record what was actually paid (spec §8.4)."""
	order_gid = order["id"]
	existing = frappe.db.get_value(
		"Sales Invoice",
		{
			"shopify_store": store_doc.name,
			"shopify_order_gid": order_gid,
			"docstatus": ["<", 2],
			"is_return": 0,
		},
		"name",
	)
	if existing:
		return {"sales_invoice": existing, "payment_entry": None, "note": "already invoiced"}

	so_name = usable_sales_order(store_doc.name, order_gid) or create_sales_order(store_doc, order)

	company_currency = frappe.get_cached_value("Company", store_doc.company, "default_currency")
	side = money_side_for(order, company_currency)

	from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

	with inbound_write():
		si = make_sales_invoice(so_name, ignore_permissions=True)
		si.shopify_store = store_doc.name
		si.shopify_order_gid = order_gid
		si.set_posting_time = 1
		si.posting_date = get_datetime(order.get("processedAt") or order.get("createdAt")).date()
		si.ignore_pricing_rule = 1
		if store_doc.sales_invoice_series:
			si.naming_series = store_doc.sales_invoice_series
		if store_doc.cost_center:
			si.cost_center = store_doc.cost_center

		mark(si)
		si.insert(ignore_permissions=True)
		si.reload()
		assert_total_matches(si, order, side)
		si.submit()

		payment = _create_payment_entry(store_doc, si, order, side)

	return {"sales_invoice": si.name, "payment_entry": payment}


def _create_payment_entry(store_doc, invoice, order: dict, side: str) -> str | None:
	"""Record the money received.

	``orders/paid`` does not reliably mean paid in full, so the amount comes from the order's
	outstanding balance rather than from its total (spec §8.4).
	"""
	if not store_doc.cash_bank_account:
		return None

	status = cstr(order.get("displayFinancialStatus")).upper()
	if status not in PAID_STATUSES:
		return None

	total = money_field(order, "totalPriceSet", side)
	outstanding = money_field(order, "totalOutstandingSet", side)
	received = total - outstanding
	if received <= 0:
		return None

	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	entry = get_payment_entry("Sales Invoice", invoice.name)
	entry.reference_no = cstr(order.get("name")) or invoice.name
	entry.reference_date = invoice.posting_date
	entry.paid_to = store_doc.cash_bank_account
	entry.paid_amount = to_float(quantize(received))
	entry.received_amount = to_float(quantize(received))
	for reference in entry.references:
		reference.allocated_amount = min(to_float(quantize(received)), reference.outstanding_amount or 0)

	mark(entry)
	entry.insert(ignore_permissions=True)
	entry.submit()
	return entry.name


# --------------------------------------------------------------------------------------
# Delivery Notes
# --------------------------------------------------------------------------------------


def create_delivery_notes(store_doc, order: dict) -> list[str]:
	"""One Delivery Note per Shopify fulfilment (spec §8.5).

	Partial fulfilment is the normal case, not an edge case: the note carries only the
	quantities in that fulfilment, and a later fulfilment produces a second note.
	"""
	order_gid = order["id"]
	so_name = usable_sales_order(store_doc.name, order_gid) or create_sales_order(store_doc, order)

	created = []
	for fulfilment in order.get("fulfillments") or []:
		if cstr(fulfilment.get("status")).upper() == "CANCELLED":
			continue

		fulfilment_gid = cstr(fulfilment.get("id"))
		if frappe.db.exists(
			"Delivery Note",
			{"shopify_fulfillment_gid": fulfilment_gid, "docstatus": ["<", 2]},
		):
			continue

		name = _create_delivery_note(store_doc, order, so_name, fulfilment)
		if name:
			created.append(name)
	return created


def _create_delivery_note(store_doc, order: dict, so_name: str, fulfilment: dict) -> str | None:
	from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

	warehouse = _warehouse_for(store_doc, fulfilment)
	quantities = _fulfilled_quantities(store_doc, fulfilment)
	if not quantities:
		return None

	with inbound_write():
		dn = make_delivery_note(so_name)
		dn.shopify_store = store_doc.name
		dn.shopify_order_gid = order["id"]
		dn.shopify_fulfillment_gid = cstr(fulfilment.get("id"))
		dn.set_posting_time = 1
		dn.posting_date = get_datetime(fulfilment.get("createdAt") or order.get("createdAt")).date()
		if store_doc.delivery_note_series:
			dn.naming_series = store_doc.delivery_note_series

		# Drawn down as it is spent, not read afresh for every row. `quantities` is the total
		# fulfilled per item, and Shopify routinely puts one variant on two order lines -- two
		# of the same shirt with different engraving, say. Reading the total for each row ships
		# it once per row: one unit fulfilled becomes two units off the shelf, and because each
		# row stays within its own Sales Order row nothing in ERPNext objects.
		remaining = dict(quantities)
		kept = []
		for row in dn.items:
			wanted = remaining.get(row.item_code)
			if not wanted:
				continue
			taken = min(row.qty, wanted)
			if taken <= 0:
				continue
			row.qty = taken
			row.warehouse = warehouse
			remaining[row.item_code] = wanted - taken
			kept.append(row)

		if not kept:
			return None

		dn.set("items", [])
		for row in kept:
			dn.append("items", row.as_dict())

		mark(dn)
		dn.insert(ignore_permissions=True)
		dn.submit()

	return dn.name


def _fulfilled_quantities(store_doc, fulfilment: dict) -> dict[str, int]:
	"""item_code -> quantity in this fulfilment."""
	from shopify_integration.shopify_integration.doctype.shopify_item_link.shopify_item_link import (
		get_link,
	)

	quantities: dict[str, int] = {}
	for node in (fulfilment.get("fulfillmentLineItems") or {}).get("nodes") or []:
		quantity = node.get("quantity") or 0
		if quantity <= 0:
			continue

		line = node.get("lineItem") or {}
		variant_gid = cstr((line.get("variant") or {}).get("id")) or None
		sku = cstr(line.get("sku")).strip() or None

		link = get_link(store_doc.name, variant_gid=variant_gid, sku=sku)
		item_code = link["item_code"] if link else sku
		if not item_code:
			continue
		quantities[item_code] = quantities.get(item_code, 0) + quantity
	return quantities


def _warehouse_for(store_doc, fulfilment: dict) -> str:
	"""Map the fulfilment's Shopify location to a warehouse, else the store default."""
	location_gid = cstr((fulfilment.get("location") or {}).get("id"))
	if location_gid:
		for row in store_doc.location_map or []:
			if cstr(row.location_gid) == location_gid:
				return row.warehouse
	return store_doc.default_warehouse


# --------------------------------------------------------------------------------------
# Cancellation
# --------------------------------------------------------------------------------------


def cancel_linked_documents(store: str, order_gid: str | None) -> list[str]:
	"""Cancel everything booked for an order, dependents before the order itself.

	Delivery Notes and Invoices both depend on the Sales Order, so cancelling the order first
	would simply be refused by ERPNext.
	"""
	if not order_gid:
		return []

	cancelled = []

	# Payment Entries first. They reference the invoices, and ERPNext refuses to cancel a
	# document something else still points at. They carry no Shopify field of their own, so
	# they are found through the invoices they pay.
	invoices = frappe.get_all(
		"Sales Invoice",
		filters={"shopify_store": store, "shopify_order_gid": order_gid, "docstatus": 1},
		pluck="name",
	)
	for payment in frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": "Sales Invoice", "reference_name": ["in", invoices or [""]]},
		pluck="parent",
		distinct=True,
	):
		if frappe.db.get_value("Payment Entry", payment, "docstatus") != 1:
			continue
		with inbound_write():
			doc = frappe.get_doc("Payment Entry", payment)
			mark(doc)
			doc.cancel()
		cancelled.append(f"Payment Entry {payment}")

	# Then the documents themselves, returns before what they return against: a credit note
	# points at its invoice, and a return delivery note at its delivery note. Cancelling the
	# original first is refused, which is how a refunded order used to fail to cancel at all.
	for doctype, is_return in (
		("Delivery Note", 1),
		("Delivery Note", 0),
		("Sales Invoice", 1),
		("Sales Invoice", 0),
		("Sales Order", None),
	):
		filters = {"shopify_store": store, "shopify_order_gid": order_gid, "docstatus": 1}
		if is_return is not None:
			filters["is_return"] = is_return

		names = frappe.get_all(doctype, filters=filters, pluck="name")

		if is_return == 1:
			# Returns created before the order reference was recorded on them are findable
			# only through what they return against. Missing one blocks the original, so look
			# both ways rather than trusting the newer field alone.
			originals = frappe.get_all(
				doctype,
				filters={"shopify_store": store, "shopify_order_gid": order_gid, "is_return": 0},
				pluck="name",
			)
			if originals:
				names += [
					n
					for n in frappe.get_all(
						doctype,
						filters={"return_against": ["in", originals], "docstatus": 1, "is_return": 1},
						pluck="name",
					)
					if n not in names
				]

		for name in names:
			with inbound_write():
				doc = frappe.get_doc(doctype, name)
				mark(doc)
				doc.cancel()
			cancelled.append(f"{doctype} {name}")
	return cancelled
