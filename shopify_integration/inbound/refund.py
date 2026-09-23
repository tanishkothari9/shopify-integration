"""Refunds -> Credit Note (spec §8.6).

The capability the existing app has none of, and the one with the most ways to be subtly
wrong. Three things have to come out right together: the money, the stock, and the paperwork
that links them.

**The money.** A refund becomes a return Sales Invoice against the original, carrying only
the refunded lines and quantities, with tax and shipping taken from Shopify's own breakdown
rather than re-derived.

**The stock.** Whether units come back is Shopify's decision, not ours -- ``restockType``
carries it per line. And *how* they come back depends on whether they ever left:

* the order was delivered, so a Delivery Note exists -> a return Delivery Note brings the
  stock back into the warehouse
* the order was never fulfilled -> nothing left the warehouse, so nothing needs to return.
  The credit note is accounting only.

Booking stock back for an order that never shipped would invent inventory out of nothing,
which is the quiet way a refund feature corrupts a stock ledger.

**The payment.** Only refunds Shopify actually disbursed get a reversing Payment Entry. A
refund recorded without a successful transaction has not moved any money yet.
"""

from __future__ import annotations

from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import cstr, flt, get_datetime

from shopify_integration.api.client import ShopifyClient, load_query
from shopify_integration.catalogue.echo import inbound_write, mark
from shopify_integration.inbound.webhook import payload_of
from shopify_integration.utils.money import ZERO, from_document, quantize, to_float, totals_match
from shopify_integration.utils.taxes import (
	PRESENTMENT_MONEY,
	SHOP_MONEY,
	money_field,
	money_side_for,
)

#: restockType values that mean the units come back into inventory. Anything else -- in
#: practice NO_RESTOCK -- means the goods are gone even though the money is returned.
RESTOCKING_TYPES = frozenset({"RETURN", "CANCEL", "LEGACY_RESTOCK"})

#: A refund transaction only counts once the gateway has actually settled it.
SETTLED_STATUSES = frozenset({"SUCCESS", "PENDING"})


def on_refund_create(event_log: str):
	"""refunds/create -> Credit Note, optional return Delivery Note, optional Payment Entry."""
	log = frappe.get_doc("Shopify Event Log", event_log)
	try:
		store_doc = frappe.get_cached_doc("Shopify Store", log.store)
		if not store_doc.sync_refunds:
			log.mark_success()
			return {"skipped": "sync_refunds is off"}

		refund = fetch_refund(log.store, payload_of(event_log))
		if refund is None:
			log.mark_success()
			return {"skipped": "refund no longer exists"}

		result = create_credit_note(store_doc, refund)
		frappe.db.commit()
		log.mark_success(ref_doctype="Sales Invoice", ref_docname=result.get("credit_note"))
		return result
	except Exception:
		log.mark_error(frappe.get_traceback())
		raise


def _refund_gid(payload: dict) -> str | None:
	gid = payload.get("admin_graphql_api_id")
	if gid:
		return gid
	numeric = payload.get("id")
	return f"gid://shopify/Refund/{numeric}" if numeric else None


def fetch_refund(store: str, payload: dict) -> dict | None:
	gid = _refund_gid(payload)
	if not gid:
		frappe.throw(_("Webhook payload carried no refund id"))

	client = ShopifyClient.for_store(store)
	data = client.execute(load_query("refund_by_id"), {"id": gid}, cost_hint=15)
	return data.get("refund")


# --------------------------------------------------------------------------------------
# Credit note
# --------------------------------------------------------------------------------------


def create_credit_note(store_doc, refund: dict) -> dict:
	"""Build the credit note and whatever else this refund implies. Idempotent."""
	refund_gid = refund["id"]
	existing = frappe.db.get_value(
		"Sales Invoice",
		{"shopify_store": store_doc.name, "shopify_refund_gid": refund_gid, "docstatus": ["<", 2]},
		"name",
	)
	if existing:
		return {"credit_note": existing, "note": "already credited"}

	order_gid = (refund.get("order") or {}).get("id")

	# Cancelling an unpaid order in Shopify writes a zero-value refund as part of the
	# cancellation. There is nothing to credit -- the order was never invoiced, because it was
	# never paid -- so building a credit note for 0.00 is meaningless. Left alone this raised
	# on every cancelled unpaid order, and those errors bury the ones that matter.
	if _refunded_nothing(refund):
		return {"skipped": "refund is for zero, nothing to credit", "order": order_gid}

	invoice_name = _original_invoice(store_doc.name, order_gid)
	if not invoice_name:
		frappe.throw(
			_(
				"Refund {0} is for Shopify order {1}, which has no Sales Invoice in ERPNext. "
				"Enable invoice sync, or replay the order's paid webhook first."
			).format(refund_gid, order_gid)
		)

	quantities = refund_quantities(store_doc, refund, _side_for(invoice_name, store_doc.company))
	if not quantities:
		frappe.throw(_("Refund {0} names no line items this ERPNext site knows about.").format(refund_gid))

	side = _side_for(invoice_name, store_doc.company)

	credit_note = _build_return_invoice(store_doc, refund, invoice_name, quantities, side)
	delivery_return = _restock(store_doc, refund, order_gid, quantities)
	payment = _reverse_payment(store_doc, credit_note, refund, side)

	return {
		"credit_note": credit_note,
		"return_delivery_note": delivery_return,
		"payment_entry": payment,
	}


def _side_for(invoice_name: str, company: str) -> str:
	"""Which MoneyBag side to read, taken from the invoice this refund credits.

	Not from the refund payload. A credit note inherits its invoice's currency, and the refund
	carries a *different* currency field -- the order's ``currencyCode`` is the shop's, while
	``totalRefundedSet.presentmentMoney`` is the customer's. On a store selling in more than one
	currency those disagree, and reading the wrong one writes amounts in one currency onto a
	document denominated in another. Deriving it from the invoice makes the two agree by
	construction.
	"""
	currency = frappe.db.get_value("Sales Invoice", invoice_name, "currency")
	company_currency = frappe.get_cached_value("Company", company, "default_currency")
	return money_side_for({"currencyCode": currency}, company_currency)


def _original_invoice(store: str, order_gid: str | None) -> str | None:
	if not order_gid:
		return None
	return frappe.db.get_value(
		"Sales Invoice",
		{"shopify_store": store, "shopify_order_gid": order_gid, "is_return": 0, "docstatus": 1},
		"name",
	)


def refund_quantities(store_doc, refund: dict, side: str) -> dict[str, dict]:
	"""item_code -> {qty, restock, tax, amount} for the refunded lines.

	Quantities are summed per item because one ERPNext item can appear on several Shopify
	order lines, and because a refund can name the same line twice.
	"""
	from shopify_integration.shopify_integration.doctype.shopify_item_link.shopify_item_link import (
		get_link,
	)

	found: dict[str, dict] = {}

	for node in (refund.get("refundLineItems") or {}).get("nodes") or []:
		quantity = node.get("quantity") or 0
		if quantity <= 0:
			continue

		line = node.get("lineItem") or {}
		variant_gid = cstr((line.get("variant") or {}).get("id")) or None
		sku = cstr(line.get("sku")).strip() or None

		link = get_link(store_doc.name, variant_gid=variant_gid, sku=sku)
		item_code = link["item_code"] if link else (sku if sku and frappe.db.exists("Item", sku) else None)
		if not item_code:
			continue

		entry = found.setdefault(
			item_code,
			{"qty": 0, "restock": False, "tax": ZERO, "amount": ZERO, "location_gid": None},
		)
		entry["qty"] += quantity
		entry["tax"] += money_field(node, "totalTaxSet", side)
		entry["amount"] += money_field(node, "subtotalSet", side)
		if cstr(node.get("restockType")).upper() in RESTOCKING_TYPES:
			entry["restock"] = True
			entry["location_gid"] = (node.get("location") or {}).get("id") or entry["location_gid"]

	return found


def _build_return_invoice(store_doc, refund: dict, invoice_name: str, quantities: dict, side: str) -> str:
	"""A return Sales Invoice carrying only the refunded lines."""
	from erpnext.controllers.sales_and_purchase_return import make_return_doc

	with inbound_write():
		credit_note = make_return_doc("Sales Invoice", invoice_name)
		credit_note.shopify_store = store_doc.name
		credit_note.shopify_refund_gid = refund["id"]
		# Also record the order. Without it a credit note is findable only by its refund, so
		# cancelling an order never finds it -- and ERPNext then refuses to cancel the invoice
		# the credit note points at, leaving the whole order uncancellable.
		credit_note.shopify_order_gid = (refund.get("order") or {}).get("id")
		credit_note.set_posting_time = 1
		credit_note.posting_date = get_datetime(refund.get("createdAt")).date()
		credit_note.ignore_pricing_rule = 1
		# Stock is handled separately, by a return Delivery Note when the goods actually
		# shipped. Letting the credit note move stock as well would double the movement.
		credit_note.update_stock = 0
		if store_doc.credit_note_series:
			credit_note.naming_series = store_doc.credit_note_series

		# Drawn down per row: an item sitting on two invoice lines would otherwise be credited
		# its whole refunded quantity twice over.
		remaining = {code: entry["qty"] for code, entry in quantities.items()}
		kept = []
		for row in credit_note.items:
			wanted = remaining.get(row.item_code)
			if not wanted:
				continue
			# make_return_doc gives negative quantities for everything on the original
			# invoice; trim each line to the quantity Shopify actually refunded.
			taken = min(abs(row.qty), wanted)
			if taken <= 0:
				continue
			remaining[row.item_code] = wanted - taken
			row.qty = -taken
			row.stock_qty = row.qty * (row.conversion_factor or 1)
			kept.append(row)

		if not kept:
			frappe.throw(_("Refund {0} matched no lines on invoice {1}.").format(refund["id"], invoice_name))

		credit_note.set("items", [])
		for row in kept:
			credit_note.append("items", row.as_dict())

		_apply_refund_taxes(store_doc, credit_note, refund, quantities, side)

		mark(credit_note)
		credit_note.insert(ignore_permissions=True)
		credit_note.reload()
		_assert_credit_matches(credit_note, refund, side)
		credit_note.submit()

	return credit_note.name


def _refunded_nothing(refund: dict) -> bool:
	"""True when the refund moves no money and returns no goods.

	Shopify records one of these whenever an unpaid order is cancelled. It is bookkeeping, not
	a return: nothing was charged, so nothing is being given back.

	Both halves are checked. A zero total with line items is a restock worth recording; a
	refund with neither is the cancellation artefact.
	"""
	for key in ("totalRefundedSet", "totalRefunded"):
		bag = refund.get(key)
		if bag:
			for side in (SHOP_MONEY, PRESENTMENT_MONEY):
				if money_field(refund, key, side) != ZERO:
					return False

	if (refund.get("refundShippingLines") or {}).get("nodes"):
		return False

	# Line items alone do not make it a real refund. Shopify lists the cancelled order's lines
	# here with `restockType: NO_RESTOCK` -- nothing came back to the warehouse and no money
	# moved. A genuine zero-value refund, where the goods return but the money was settled
	# some other way, carries RETURN or CANCEL and is worth booking.
	for node in (refund.get("refundLineItems") or {}).get("nodes") or []:
		if cstr(node.get("restockType")).upper() not in ("", "NO_RESTOCK"):
			return False

	return True


def _apply_refund_taxes(store_doc, credit_note, refund: dict, quantities: dict, side: str) -> None:
	"""Replace the copied tax rows with the amounts Shopify actually refunded.

	A partial refund does not return a proportional share of every tax row -- Shopify has
	already worked out what tax it is giving back per line, and its figure is the one that
	has to reconcile against the money the customer receives.
	"""
	line_tax = sum((entry["tax"] for entry in quantities.values()), ZERO)
	shipping_tax = ZERO
	shipping_amount = ZERO

	for node in (refund.get("refundShippingLines") or {}).get("nodes") or []:
		shipping_amount += money_field(node, "subtotalAmountSet", side)
		shipping_tax += money_field(node, "taxAmountSet", side)

	total_tax = line_tax + shipping_tax
	rows = list(credit_note.get("taxes") or [])
	credit_note.set("taxes", [])

	if total_tax == ZERO and shipping_amount == ZERO:
		return

	# Which of the copied rows is freight and which is tax, asked of the store rather than
	# guessed from their order. Taking rows[0] as "the tax account" was the first version of
	# this, and _add_charges appends the shipping row *before* the tax rows -- so every refund
	# credited the tax to the freight account and the freight to the tax account, exactly
	# inverted, on any store that books shipping as a charge.
	shipping_accounts = {
		row.account_head
		for row in (store_doc.tax_map or [])
		if cstr(row.charge_type) == "shipping" and row.account_head
	}
	tax_rows = [row for row in rows if row.account_head not in shipping_accounts]
	shipping_rows = [row for row in rows if row.account_head in shipping_accounts]

	# A tax-inclusive invoice books its tax as a percentage row marked "included in print
	# rate", and every returned line carries its own Item Tax Template. Flattening that to an
	# Actual amount breaks it: `india_compliance` recomputes the per-item tax from the rows and
	# refuses the note --
	#     "Tax Amount -107.14 as computed for Item SAREE-001 is incorrect.
	#      Try setting the Charge Type to On Net Total"
	# -- so the whole refund was lost. Keep the shape the invoice used and let ERPNext work the
	# amounts out from the lines actually being returned, which is what makes a partial return
	# of one 5% saree out of a mixed-rate order come back at 5%.
	if _is_inclusive(tax_rows):
		for row in tax_rows:
			credit_note.append(
				"taxes",
				{
					"charge_type": row.charge_type,
					"account_head": row.account_head,
					"description": row.description or _("Refunded tax"),
					"rate": row.rate,
					"included_in_print_rate": 1,
				},
			)
		for account, amount in _split_like(shipping_rows or [], shipping_amount).items():
			credit_note.append(
				"taxes",
				{
					"charge_type": "Actual",
					"account_head": account,
					"description": _("Refunded shipping"),
					"tax_amount": -to_float(quantize(amount)),
				},
			)
		return

	# Split across the accounts the invoice actually used, in the proportions it used them.
	# One account is not good enough under GST: CGST and SGST are separate heads on separate
	# rows, and crediting the whole refund to CGST misstates both halves of the return.
	for account, amount in _split_like(tax_rows, total_tax).items():
		credit_note.append(
			"taxes",
			{
				"charge_type": "Actual",
				"account_head": account,
				"description": _("Refunded tax"),
				"tax_amount": -to_float(quantize(amount)),
			},
		)

	if shipping_amount != ZERO:
		freight = shipping_rows or tax_rows
		if not shipping_rows and tax_rows:
			frappe.log_error(
				title=f"Refund {refund.get('id')}: no shipping account mapped",
				message=(
					"Refunded shipping was credited to a tax account because the store has no "
					"Tax Map row with charge type 'shipping'. Add one so returns reverse the "
					"account the original charge used."
				),
			)
		for account, amount in _split_like(freight, shipping_amount).items():
			credit_note.append(
				"taxes",
				{
					"charge_type": "Actual",
					"account_head": account,
					"description": _("Refunded shipping"),
					"tax_amount": -to_float(quantize(amount)),
				},
			)


def _is_inclusive(rows: list) -> bool:
	"""True when the invoice booked its tax as inside the price rather than on top of it."""
	return bool(rows) and all(
		row.charge_type == "On Net Total" and row.included_in_print_rate for row in rows
	)


def _split_like(rows: list, total: Decimal) -> dict[str, Decimal]:
	"""Split ``total`` across the accounts in ``rows``, in the proportions they already carry.

	The last account absorbs the rounding remainder, so the parts sum to the total exactly --
	a credit note whose rows do not add up to what the customer was refunded is refused before
	it posts, and rightly.
	"""
	if total == ZERO or not rows:
		return {}

	weights: dict[str, Decimal] = {}
	for row in rows:
		amount = abs(from_document(row.get("tax_amount") or 0))
		weights[row.account_head] = weights.get(row.account_head, ZERO) + amount

	accounts = list(weights)
	base = sum(weights.values(), ZERO)
	if base == ZERO:
		# Every original row was zero -- a fully discounted order, say. There is no proportion
		# to preserve, so the first account takes it.
		return {accounts[0]: total}

	split: dict[str, Decimal] = {}
	running = ZERO
	for account in accounts[:-1]:
		share = quantize(total * weights[account] / base)
		split[account] = share
		running += share
	split[accounts[-1]] = total - running
	return split


# --------------------------------------------------------------------------------------
# Stock
# --------------------------------------------------------------------------------------


def _note_covering(delivery_notes: list[str], restocking: dict) -> str | None:
	"""The delivery note that shipped the most of what is being returned.

	A refund can span two notes, and ERPNext returns against one document at a time. Choosing
	the note carrying the most of the refunded items returns as much as one document can; the
	rest is reported rather than silently dropped.
	"""
	best, best_cover = None, 0
	for name in delivery_notes:
		shipped = frappe.get_all(
			"Delivery Note Item",
			filters={"parent": name, "parenttype": "Delivery Note"},
			fields=["item_code", "qty"],
		)
		cover = sum(
			min(row.qty, restocking[row.item_code]["qty"]) for row in shipped if row.item_code in restocking
		)
		if cover > best_cover:
			best, best_cover = name, cover

	if not best:
		frappe.log_error(
			title="Shopify refund: no delivery note carries the returned items",
			message=(
				f"Notes searched: {delivery_notes}. Items to restock: {sorted(restocking)}. "
				"Stock was not returned; restock it by hand if the goods came back."
			),
		)
	return best


def _restock(store_doc, refund: dict, order_gid: str | None, quantities: dict) -> str | None:
	"""Bring stock back, but only if it ever left (spec §8.6 point 4)."""
	restocking = {code: entry for code, entry in quantities.items() if entry["restock"]}
	if not restocking:
		return None

	# Every note this order shipped on, not just one. An order fulfilled in two parcels has
	# two, and picking whichever the database happened to return first meant a refund of an
	# item that went out on the second note matched no line at all -- the stock was silently
	# never returned, and nothing anywhere said so.
	delivery_notes = frappe.get_all(
		"Delivery Note",
		filters={
			"shopify_store": store_doc.name,
			"shopify_order_gid": order_gid,
			"docstatus": 1,
			"is_return": 0,
		},
		order_by="posting_date, creation",
		pluck="name",
	)
	if not delivery_notes:
		# Never shipped, so nothing physically left the warehouse. Creating a return here
		# would invent inventory that was never removed.
		return None

	delivery_note = _note_covering(delivery_notes, restocking)
	if not delivery_note:
		return None

	from erpnext.controllers.sales_and_purchase_return import make_return_doc

	with inbound_write():
		doc = make_return_doc("Delivery Note", delivery_note)
		doc.shopify_store = store_doc.name
		doc.shopify_order_gid = order_gid
		doc.set_posting_time = 1
		doc.posting_date = get_datetime(refund.get("createdAt")).date()

		# Drawn down per row, for the same reason as the credit note above: one item across two
		# delivery lines would otherwise be restocked twice, and ERPNext would refuse the whole
		# return for over-returning rather than restock the right amount.
		remaining = {code: entry["qty"] for code, entry in restocking.items()}
		kept = []
		for row in doc.items:
			entry = restocking.get(row.item_code)
			left = remaining.get(row.item_code, 0)
			if not entry or left <= 0:
				continue
			taken = min(abs(row.qty), left)
			if taken <= 0:
				continue
			remaining[row.item_code] = left - taken
			row.qty = -taken
			row.stock_qty = row.qty * (row.conversion_factor or 1)
			warehouse = _warehouse_for_location(store_doc, entry.get("location_gid"))
			if warehouse:
				row.warehouse = warehouse
			kept.append(row)

		if not kept:
			return None

		doc.set("items", [])
		for row in kept:
			doc.append("items", row.as_dict())

		mark(doc)
		doc.insert(ignore_permissions=True)
		doc.submit()

	return doc.name


def _warehouse_for_location(store_doc, location_gid: str | None) -> str | None:
	"""Where Shopify says the goods were restocked to, mapped to an ERPNext warehouse."""
	if not location_gid:
		return store_doc.default_warehouse
	for row in store_doc.location_map or []:
		if cstr(row.location_gid) == location_gid:
			return row.warehouse
	return store_doc.default_warehouse


# --------------------------------------------------------------------------------------
# Payment
# --------------------------------------------------------------------------------------


def refunded_amount(refund: dict, side: str) -> Decimal:
	"""What the gateway actually gave back, from the refund's own transactions.

	Taken from settled transactions rather than from ``totalRefundedSet``, because a refund
	can be recorded in Shopify without any money moving -- a store-credit exchange, say.
	"""
	total = ZERO
	for node in (refund.get("transactions") or {}).get("nodes") or []:
		if cstr(node.get("kind")).upper() != "REFUND":
			continue
		if cstr(node.get("status")).upper() not in SETTLED_STATUSES:
			continue
		total += money_field(node, "amountSet", side)
	return total


def _reverse_payment(store_doc, credit_note: str, refund: dict, side: str) -> str | None:
	"""A reversing Payment Entry, when money genuinely went back."""
	if not store_doc.cash_bank_account:
		return None

	amount = refunded_amount(refund, side)
	if amount <= ZERO:
		return None

	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	with inbound_write():
		entry = get_payment_entry("Sales Invoice", credit_note)
		entry.reference_no = cstr((refund.get("order") or {}).get("name")) or credit_note
		entry.reference_date = frappe.db.get_value("Sales Invoice", credit_note, "posting_date")
		entry.paid_from = store_doc.cash_bank_account

		# get_payment_entry sizes itself from the credit note's own outstanding, which is the
		# whole refund. Only part of it may have left the bank: a 5,000 refund settled as 2,000
		# to the card and 3,000 in store credit moves 2,000. Paying out the full amount
		# overstates the bank by the difference, and it will not reconcile against a statement.
		#
		# Scaled down from what get_payment_entry produced rather than rebuilt: a credit note's
		# outstanding is negative and its allocations follow that sign, and reconstructing those
		# conventions by hand trips ERPNext's own validation from two directions at once.
		# Scaling keeps them and only changes the magnitude.
		wanted = to_float(quantize(amount))
		default = entry.paid_amount or 0
		if default and wanted < default:
			ratio = wanted / default
			for reference in entry.references:
				reference.allocated_amount = flt(
					(reference.allocated_amount or 0) * ratio, reference.precision("allocated_amount")
				)
			entry.paid_amount = wanted
			entry.received_amount = wanted

		mark(entry)
		entry.insert(ignore_permissions=True)
		entry.submit()

	return entry.name


def _assert_credit_matches(credit_note, refund: dict, side: str) -> None:
	"""Refuse a credit note that does not add up to the refund it represents.

	Orders have been guarded this way since the beginning; refunds were not, and that is why
	every arithmetic mistake in this module was silent. A credit note is a document that takes
	money off a customer's account -- it deserves the same check as the one that put it on.

	Compared against Shopify's own total for the refund, negated: a return invoice carries
	negative amounts.
	"""
	expected = money_field(refund, "totalRefundedSet", side)
	if expected == ZERO:
		return

	computed = abs(from_document(credit_note.get("grand_total")))
	precision = frappe.get_precision("Sales Invoice", "grand_total") or 2

	if not totals_match(computed, expected, precision):
		frappe.throw(
			_(
				"Credit note for refund {0} totals {1}, but Shopify refunded {2} "
				"(difference {3}). Refusing to post a credit note that disagrees with the refund."
			).format(
				refund.get("id"),
				quantize(computed, precision),
				quantize(expected, precision),
				quantize(computed - expected, precision),
			)
		)
