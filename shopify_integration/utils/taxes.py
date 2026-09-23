"""Tax, discount, shipping and currency handling for Shopify orders (spec §13).

This is where integrations quietly produce wrong books, so every decision here is made the
boring way: take Shopify's own computed figures rather than re-deriving them, keep everything
in Decimal, and refuse to post a document whose total disagrees with the order.

Three choices are worth stating outright:

* **Discounts are taken as Shopify allocated them**, from
  ``discountedUnitPriceAfterAllDiscountsSet``. The similarly named ``discountedUnitPriceSet``
  is *not* that figure: it nets off discounts applied to the line itself but ignores an
  order-level one -- a coupon code -- which Shopify records per line only under
  ``discountAllocations``. Re-deriving the allocation ourselves is how you end up a cent out
  on a percentage discount over an odd number of lines, so take Shopify's own field.
* **Tax-inclusive pricing uses ERPNext's own percentage mechanism.** Subtracting tax from
  the rate by hand cannot work: a 9.09 tax over 2 units needs a net rate of 45.455, which is
  not representable at two-decimal currency precision, so the line lands a cent out. The
  *gross* rate (50.00) always is representable, so tax-inclusive orders keep gross rates and
  book line tax as ``On Net Total`` with ``included_in_print_rate``, letting ERPNext do the
  division at full precision. Shipping tax stays Actual, with the shipping charge booked net,
  so the two halves still sum to the gross the customer paid.
* **Tax lines are consolidated by account head.** Two Shopify tax lines pointing at one
  ERPNext account become one row. Keeping them separate produces two rows on the same account
  that sum correctly but reconcile confusingly.
"""

from __future__ import annotations

from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import cstr

from shopify_integration.utils.money import ZERO, parse_money_field, quantize, to_float

#: Which side of a MoneyBag to read.
SHOP_MONEY = "shopMoney"
PRESENTMENT_MONEY = "presentmentMoney"


def money_field(node: dict | None, key: str, side: str = SHOP_MONEY) -> Decimal:
	"""Read ``node[key][side].amount`` as a Decimal, defaulting to zero."""
	return parse_money_field(node, key, side, "amount", field=key)


def presentment_currency(order: dict) -> str:
	return cstr(order.get("currencyCode")) or ""


def conversion_rate(order: dict, company_currency: str) -> tuple[str, Decimal]:
	"""Return the document currency and its conversion rate to the company currency.

	Derived from Shopify's own two views of the same total rather than from a rate table:
	whatever rate Shopify actually used is the rate that makes the books reconcile, and a
	table lookup on a different day would not.
	"""
	currency = presentment_currency(order) or company_currency
	if currency == company_currency:
		return currency, Decimal(1)

	presented = money_field(order, "totalPriceSet", PRESENTMENT_MONEY)
	base = money_field(order, "totalPriceSet", SHOP_MONEY)
	if presented and presented != ZERO:
		return currency, base / presented

	# A zero-total order in a foreign currency gives us nothing to divide. Rather than guess,
	# refuse: a wrong rate silently mis-states every figure on the document.
	frappe.throw(
		_("Cannot determine a conversion rate for order {0}: Shopify reported a zero total.").format(
			order.get("name") or order.get("id")
		)
	)
	return currency, Decimal(1)


def money_side_for(order: dict, company_currency: str) -> str:
	"""Which side of every MoneyBag the document's own amounts should come from.

	ERPNext stores amounts in the document's currency and converts to the company currency
	itself, so a document in the presentment currency must carry presentment amounts.
	"""
	return SHOP_MONEY if presentment_currency(order) == company_currency else PRESENTMENT_MONEY


# --------------------------------------------------------------------------------------
# Tax lines
# --------------------------------------------------------------------------------------


def collect_tax_lines(order: dict, side: str) -> list[dict]:
	"""Every tax line on the order, from line items and shipping lines alike.

	Order-level ``taxLines`` are a summary of the per-line ones; taking both would double the
	tax. The per-line lines are used when present because they are what actually sums to the
	order's tax total, and they survive partial refunds intact.
	"""
	collected: list[dict] = []

	for line in (order.get("lineItems") or {}).get("nodes") or []:
		for tax in line.get("taxLines") or []:
			collected.append(_tax_entry(tax, side))

	for shipping in (order.get("shippingLines") or {}).get("nodes") or []:
		for tax in shipping.get("taxLines") or []:
			collected.append(_tax_entry(tax, side))

	if collected:
		return collected

	# Some orders carry tax only at order level (older orders, or tax services that do not
	# break it down). Fall back rather than booking an order with no tax at all.
	return [_tax_entry(tax, side) for tax in order.get("taxLines") or []]


def _tax_entry(tax: dict, side: str) -> dict:
	return {
		"title": cstr(tax.get("title")).strip() or "Tax",
		"rate": tax.get("rate"),
		"amount": money_field(tax, "priceSet", side),
	}


def _tax_accounts(store_doc) -> dict[str, str]:
	return {
		cstr(row.shopify_tax_title).strip().lower(): row.account_head
		for row in (store_doc.tax_map or [])
		if cstr(row.charge_type or "sales_tax") == "sales_tax"
	}


def _account_for(store_doc, title: str, order: dict) -> str:
	account = _tax_accounts(store_doc).get(title.lower())
	if not account:
		frappe.throw(
			_(
				"Shopify tax '{0}' on order {1} is not mapped to an account head for store "
				"{2}. Add it to the store's Tax Map and retry."
			).format(title, order.get("name") or order.get("id"), store_doc.name)
		)
	return account


def _group_by_account(store_doc, entries: list[dict], order: dict) -> tuple[dict, dict]:
	by_account: dict[str, Decimal] = {}
	titles: dict[str, set[str]] = {}
	for entry in entries:
		if entry["amount"] == ZERO:
			continue
		account = _account_for(store_doc, entry["title"], order)
		by_account[account] = by_account.get(account, ZERO) + entry["amount"]
		titles.setdefault(account, set()).add(entry["title"])
	return by_account, titles


def line_gross(line: dict, side: str) -> Decimal:
	"""What the customer actually pays for one line, after every discount.

	Falls back to the pre-``discountedUnitPriceAfterAllDiscountsSet`` reading only when the
	field is absent, which no live payload is -- the order query asks for it. Presence is
	tested rather than truth, because a line given away free is legitimately zero.
	"""
	quantity = Decimal(line.get("quantity") or 0)
	if line.get("discountedUnitPriceAfterAllDiscountsSet") is not None:
		return money_field(line, "discountedUnitPriceAfterAllDiscountsSet", side) * quantity
	return money_field(line, "discountedTotalSet", side) or (
		money_field(line, "discountedUnitPriceSet", side) * quantity
	)


def line_net(line: dict, side: str, taxes_included: bool) -> Decimal:
	"""One line's value excluding tax, whichever way Shopify quoted it."""
	gross = line_gross(line, side)
	return gross - line_tax_total(line, side) if taxes_included else gross


def line_tax_rates(store_doc, line: dict, order: dict, side: str) -> dict[str, Decimal]:
	"""Percentage rate per account head for a single line.

	Shopify states the rate on every tax line, which is what is used: deriving it from the
	amount re-introduces the rounding the amount already suffered. The amount is only a
	fallback for the rare line that carries none.
	"""
	rates: dict[str, Decimal] = {}
	net = line_net(line, side, bool(order.get("taxesIncluded")))

	for tax in line.get("taxLines") or []:
		title = cstr(tax.get("title")).strip() or "Tax"
		account = _account_for(store_doc, title, order)
		rates[account] = rates.get(account, ZERO) + _percent_for(tax, net, side)

	return {account: percent for account, percent in rates.items() if percent != ZERO}


def _percent_for(tax: dict, net: Decimal, side: str) -> Decimal:
	"""The rate to bill a tax line at: Shopify's stated one when it reconciles, else derived.

	The amount is what the customer was actually charged, so it is the figure that has to come
	out whole -- a document that does not total what was paid is refused before it is booked.
	The stated rate is preferred only when it reproduces that amount, because a clean 18% reads
	correctly on an invoice and is what a GST return expects to see.

	They disagree more often than seems reasonable: Shopify rounds the rate for display, and
	some tax services state a rate against a base that is not this line's net.
	"""
	amount = money_field(tax, "priceSet", side)
	derived = (amount / net * Decimal(100)) if net else ZERO

	stated = tax.get("rate")
	if stated is None:
		return derived

	percent = Decimal(str(stated)) * Decimal(100)
	if net and abs(net * percent / Decimal(100) - amount) <= Decimal("0.01"):
		return percent
	return derived


def _template_signature(rates: dict[str, Decimal]) -> tuple[tuple[str, float], ...]:
	"""Account/rate pairs at two decimals.

	Two, not more: `india_compliance` cross-checks each component row against half the headline
	rate rounded to two places, so a rate carrying a third decimal fails its consistency check
	and the template is refused -- which silently drops the order onto the aggregate fallback.
	"""
	return tuple(sorted((account, float(quantize(pct, 2))) for account, pct in rates.items()))


def ensure_item_tax_template(company: str, rates: dict[str, Decimal], taxable: bool = True) -> str | None:
	"""An Item Tax Template for exactly this set of account/rate pairs, created on demand.

	This is what makes a mixed-rate order correct. ERPNext applies a tax row's percentage to
	every line equally, so an order holding a 5% saree and an 18% kurti would otherwise be
	booked at neither rate -- a blend of the two, which is not a rate that exists in law and
	which misstates the taxable value against each HSN code on the return.

	A template per rate set, assigned per line, makes ERPNext compute each line at its own
	rate, and `item_wise_tax_detail` then carries the per-item breakdown that GST reporting
	reads.
	"""
	if not rates:
		return None

	signature = _template_signature(rates)
	if not signature:
		return None
	title = "Shopify " + " + ".join(
		f"{account.split(' - ')[0]} {percent:g}%" for account, percent in signature
	)

	existing = frappe.db.get_value("Item Tax Template", {"title": title, "company": company}, "name")
	if existing:
		return existing

	doc = frappe.new_doc("Item Tax Template")
	doc.title = title
	doc.company = company
	for account, percent in signature:
		doc.append("taxes", {"tax_type": account, "tax_rate": percent})

	if doc.meta.has_field("gst_treatment") and not taxable:
		# india_compliance refuses a "Taxable" template whose headline rate is zero, and it is
		# right to: a taxable line at 0% is a contradiction. Nil-Rated is what a line carrying
		# no GST actually is.
		doc.gst_treatment = "Nil-Rated"

	if doc.meta.has_field("gst_rate"):
		# india_compliance carries the headline GST rate separately from the component rows and
		# refuses a taxable template that leaves it at zero. It is the sum of the components:
		# CGST 2.5 + SGST 2.5 is a 5% item, and IGST 18 is an 18% one.
		# Summed from the already-rounded components, so the two agree exactly. Deriving it
		# from the unrounded rates instead leaves the halves failing their own consistency check.
		doc.gst_rate = float(sum(percent for _, percent in signature))

	# Inside a savepoint so a failed insert does not poison the surrounding transaction.
	# On MariaDB it merely tidies up; on Postgres an integrity error without one aborts
	# everything after it, and the order would fail rather than degrade.
	#
	# `frappe.db.savepoint(name)` issues SAVEPOINT and returns None -- it is not a context
	# manager. Frappe also exports a *module-level* `savepoint(catch=...)` that is one, and
	# writing `with frappe.db.savepoint("name"):` silently picks the wrong one: it raises
	# TypeError, the except below swallows it, and this function returns None for every
	# template it was ever asked to create. Existing templates masked it, so mixed-rate
	# orders kept working until a rate turned up that nobody had booked before.
	save_point = "shopify_item_tax_template"
	frappe.db.savepoint(save_point)
	try:
		doc.insert(ignore_permissions=True)
	except Exception:
		frappe.db.rollback(save_point=save_point)
		# Two workers can reach this for the same rate at the same time -- two 18% orders
		# draining together -- and one of them loses the race. The row it wanted now exists,
		# so look again before giving up: returning None here would drop the whole order onto
		# the blended-rate fallback, which is the exact behaviour this function exists to
		# avoid, and the only trace would be an Error Log nobody reads.
		raced = frappe.db.get_value("Item Tax Template", {"title": title, "company": company}, "name")
		if raced:
			return raced

		# A genuine refusal. An Item Tax Template insists its account be of type Tax, Income,
		# Expense or Chargeable, and a store is free to map Shopify's tax to something else.
		# Rather than refuse the order, fall back to the aggregate rows, which still total
		# correctly even though they cannot attribute tax per line.
		frappe.log_error(
			title=f"Could not create Item Tax Template for {company}",
			message=frappe.get_traceback(),
		)
		return None
	else:
		frappe.db.release_savepoint(save_point)
		return doc.name


def item_tax_template_for(store_doc, line: dict, order: dict, side: str) -> str | None:
	"""The Item Tax Template a line should carry."""
	rates = line_tax_rates(store_doc, line, order, side)
	if rates:
		return ensure_item_tax_template(store_doc.company, rates)
	return zero_rate_template(store_doc, order, side)


def zero_rate_template(store_doc, order: dict, side: str) -> str | None:
	"""A 0% template, for a line Shopify charged no tax on.

	Leaving such a line without a template is not the same thing, and the difference is not
	visible anywhere: ERPNext falls back to a tax row's own rate for every account the line's
	map does not mention, so an exempt line is charged at whatever rate the rest of the order
	carries. On a tax-inclusive order the grand total still comes out right, so nothing is
	refused -- the exempt line simply reports a smaller taxable value and a tax nobody paid.

	A book sold beside a kurti, unstitched fabric, a gift card: all silently taxed at 18%.
	"""
	accounts = _accounts_used(store_doc, order, side)
	if not accounts:
		return None
	return ensure_item_tax_template(store_doc.company, dict.fromkeys(accounts, ZERO), taxable=False)


def _accounts_used(store_doc, order: dict, side: str) -> list[str]:
	"""Every account head any line of this order is taxed to."""
	accounts: dict[str, None] = {}
	for line in (order.get("lineItems") or {}).get("nodes") or []:
		for account in line_tax_rates(store_doc, line, order, side):
			accounts[account] = None
	return list(accounts)


def _every_taxed_line_has_a_template(store_doc, order: dict, side: str) -> bool:
	for line in (order.get("lineItems") or {}).get("nodes") or []:
		rates = line_tax_rates(store_doc, line, order, side)
		if rates and not ensure_item_tax_template(store_doc.company, rates):
			return False
	return True


def _aggregate_tax_rows(store_doc, order: dict, side: str) -> list[dict]:
	"""One row per account head for the whole order, totalling exactly.

	Correct for an order taxed at a single rate, which is most of them. An order mixing rates
	lands every line on the same blended figure -- wrong for GST reporting, which is why the
	per-line path above exists and why this one is only a fallback.
	"""
	if not order.get("taxesIncluded"):
		by_account, titles = _group_by_account(store_doc, collect_tax_lines(order, side), order)
		return [
			{
				"charge_type": "Actual",
				"account_head": account,
				"description": ", ".join(sorted(titles[account])),
				"tax_amount": to_float(quantize(amount)),
				"included_in_print_rate": 0,
			}
			for account, amount in by_account.items()
		]

	line_entries = []
	for line in (order.get("lineItems") or {}).get("nodes") or []:
		for tax in line.get("taxLines") or []:
			line_entries.append(_tax_entry(tax, side))
	if not line_entries:
		line_entries = [_tax_entry(tax, side) for tax in order.get("taxLines") or []]

	gross_items = _gross_line_total(order, side)
	net_items = gross_items - sum((e["amount"] for e in line_entries), ZERO)

	rows = []
	by_account, titles = _group_by_account(store_doc, line_entries, order)
	for account, amount in by_account.items():
		if net_items <= ZERO:
			# Nothing to take a percentage of -- a fully discounted order where only tax was
			# charged. Skipping the row was silently wrong: the grand total still matched
			# (inclusive rates are gross), so nothing was refused, and the tax was quietly
			# booked to income instead of the tax account. An exact amount says the same thing
			# without needing a base to compute from.
			rows.append(
				{
					"charge_type": "Actual",
					"account_head": account,
					"description": ", ".join(sorted(titles[account])),
					"tax_amount": to_float(quantize(amount)),
					"included_in_print_rate": 0,
				}
			)
			continue

		rows.append(
			{
				"charge_type": "On Net Total",
				"account_head": account,
				"description": ", ".join(sorted(titles[account])),
				"rate": to_float(amount / net_items * Decimal(100)),
				"included_in_print_rate": 1,
			}
		)

	rows.extend(_shipping_tax_rows(store_doc, order, side, True))
	return rows


def build_tax_rows(store_doc, order: dict, side: str) -> list[dict]:
	"""Map Shopify tax lines onto ERPNext tax rows, one per account head.

	Every row is a percentage, never ``Actual``. The per-line rate comes from that line's
	Item Tax Template, so the row's own rate is only a fallback for a line without one --
	shipping booked as a charge, for instance.

	``Actual`` was the obvious choice, since Shopify hands us exact amounts, and it is wrong
	twice over: ERPNext spreads an Actual amount across lines in proportion to their value
	rather than their tax rate, and `india_compliance` refuses the row outright because it
	yields no per-item tax at all.
	"""
	accounts = _accounts_and_default_rates(store_doc, order, side)
	if not accounts:
		return []

	if not _every_taxed_line_has_a_template(store_doc, order, side):
		# Without a template on every taxed line, a percentage row would apply one rate to
		# lines taxed at another and the document would no longer total what the customer
		# paid. The aggregate rows are less informative but exact, which matters more.
		return _aggregate_tax_rows(store_doc, order, side)

	inclusive = bool(order.get("taxesIncluded"))
	rows = [
		{
			"charge_type": "On Net Total",
			"account_head": account,
			"description": ", ".join(sorted(titles)),
			"rate": to_float(quantize(rate, 3)),
			"included_in_print_rate": 1 if inclusive else 0,
		}
		for account, (rate, titles) in accounts.items()
	]

	rows.extend(_shipping_tax_rows(store_doc, order, side, inclusive))
	return rows


def _accounts_and_default_rates(store_doc, order: dict, side: str) -> dict:
	"""Per account head: a fallback rate and the Shopify tax titles that fed it.

	The fallback is the rate most of the order's value is taxed at, which is the least
	surprising thing to show on a row whose lines all override it anyway.
	"""
	weighted: dict[str, dict[Decimal, Decimal]] = {}
	titles: dict[str, set[str]] = {}
	inclusive = bool(order.get("taxesIncluded"))

	for line in (order.get("lineItems") or {}).get("nodes") or []:
		net = line_net(line, side, inclusive)
		for account, percent in line_tax_rates(store_doc, line, order, side).items():
			weighted.setdefault(account, {})
			weighted[account][percent] = weighted[account].get(percent, ZERO) + net
		for tax in line.get("taxLines") or []:
			title = cstr(tax.get("title")).strip() or "Tax"
			titles.setdefault(_account_for(store_doc, title, order), set()).add(title)

	if not weighted:
		# Order-level tax only: older orders, and tax services that do not break tax down per
		# line. There is nothing to attribute per item, so the order's own rate is the rate.
		return _order_level_accounts(store_doc, order, side)

	return {
		account: (max(by_rate, key=lambda r: by_rate[r]), titles.get(account, {"Tax"}))
		for account, by_rate in weighted.items()
	}


def _order_level_accounts(store_doc, order: dict, side: str) -> dict:
	net = _gross_line_total(order, side)
	if bool(order.get("taxesIncluded")):
		net -= sum((money_field(tax, "priceSet", side) for tax in order.get("taxLines") or []), ZERO)

	accounts: dict = {}
	for tax in order.get("taxLines") or []:
		title = cstr(tax.get("title")).strip() or "Tax"
		account = _account_for(store_doc, title, order)
		rate = tax.get("rate")
		percent = (
			Decimal(str(rate)) * Decimal(100)
			if rate is not None
			else (money_field(tax, "priceSet", side) / net * Decimal(100) if net else ZERO)
		)
		current, seen = accounts.get(account, (ZERO, set()))
		accounts[account] = (current + percent, seen | {title})
	return accounts


def _shipping_tax_rows(store_doc, order: dict, side: str, inclusive: bool) -> list[dict]:
	"""Tax on shipping, when shipping is booked as a charge rather than a line item.

	Still ``Actual``: a percentage row would apply to the goods as well, which would tax them
	twice. A store on `india_compliance` should book shipping as a line item instead -- set a
	Shipping Item on the store -- so that it carries its own tax template like anything else.
	"""
	if store_doc.shipping_item:
		return []

	entries = []
	for shipping in (order.get("shippingLines") or {}).get("nodes") or []:
		for tax in shipping.get("taxLines") or []:
			entries.append(_tax_entry(tax, side))

	by_account, titles = _group_by_account(store_doc, entries, order)
	return [
		{
			"charge_type": "Actual",
			"account_head": account,
			"description": ", ".join(sorted(titles[account])) + " (shipping)",
			"tax_amount": to_float(quantize(amount)),
			"included_in_print_rate": 0,
		}
		for account, amount in by_account.items()
	]


def _gross_line_total(order: dict, side: str) -> Decimal:
	total = ZERO
	for line in (order.get("lineItems") or {}).get("nodes") or []:
		total += line_gross(line, side)
	return total


def line_tax_total(line: dict, side: str) -> Decimal:
	"""Tax charged on one order line."""
	return sum(
		(money_field(tax, "priceSet", side) for tax in line.get("taxLines") or []),
		ZERO,
	)


def unit_rate(line: dict, side: str) -> Decimal:
	"""The unit price to book: always Shopify's gross figure.

	Gross even on tax-inclusive orders, because ERPNext removes the included tax itself via
	``included_in_print_rate``. Doing that subtraction here instead loses precision -- see the
	module docstring.
	"""
	if line.get("discountedUnitPriceAfterAllDiscountsSet") is not None:
		return money_field(line, "discountedUnitPriceAfterAllDiscountsSet", side)
	return money_field(line, "discountedUnitPriceSet", side)


def list_rate(line: dict, side: str) -> Decimal:
	"""The pre-discount unit price, on the same basis as ``unit_rate``."""
	return money_field(line, "originalUnitPriceSet", side)


# --------------------------------------------------------------------------------------
# Shipping
# --------------------------------------------------------------------------------------


def shipping_tax_total(order: dict, side: str) -> Decimal:
	"""Tax charged on shipping across all shipping lines."""
	total = ZERO
	for shipping in (order.get("shippingLines") or {}).get("nodes") or []:
		for tax in shipping.get("taxLines") or []:
			total += money_field(tax, "priceSet", side)
	return total


def net_shipping_total(order: dict, side: str, taxes_included: bool) -> Decimal:
	"""Shipping exclusive of tax, for the same reason as net_unit_rate."""
	gross = shipping_total(order, side)
	if not taxes_included:
		return gross
	return gross - shipping_tax_total(order, side)


def shipping_total(order: dict, side: str) -> Decimal:
	"""What the customer actually paid for shipping, after any shipping discount.

	The discounted price is used whenever Shopify supplies one, including when it is zero.
	Treating a zero as "missing" and falling back to the original price would bill the
	customer for a free-shipping promotion they were never charged for.
	"""
	total = ZERO
	for shipping in (order.get("shippingLines") or {}).get("nodes") or []:
		if shipping.get("discountedPriceSet") is not None:
			total += money_field(shipping, "discountedPriceSet", side)
		else:
			total += money_field(shipping, "originalPriceSet", side)
	return total


def shipping_titles(order: dict) -> str:
	names = [
		cstr(s.get("title")).strip()
		for s in ((order.get("shippingLines") or {}).get("nodes") or [])
		if cstr(s.get("title")).strip()
	]
	return ", ".join(names) or "Shipping"


def shipping_tax_rates(store_doc, order: dict, side: str) -> dict[str, Decimal]:
	"""Percentage rate per account head for the order's freight, taken together.

	Shipping reaches ERPNext as a single row however many shipping lines Shopify sent, so
	every rate is measured against the combined net. Measuring each line against its own net
	and then adding the percentages would over-state the total whenever two shipping lines
	carry different rates.
	"""
	net = net_shipping_total(order, side, bool(order.get("taxesIncluded")))
	rates: dict[str, Decimal] = {}

	for shipping in (order.get("shippingLines") or {}).get("nodes") or []:
		for tax in shipping.get("taxLines") or []:
			title = cstr(tax.get("title")).strip() or "Tax"
			account = _account_for(store_doc, title, order)
			rates[account] = rates.get(account, ZERO) + _percent_for(tax, net, side)

	return {account: percent for account, percent in rates.items() if percent != ZERO}


def shipping_tax_template(store_doc, order: dict, side: str) -> str | None:
	"""The Item Tax Template the freight row should carry.

	Freight booked as a line item used to carry no template at all. ERPNext then falls back
	to each tax row's own rate for every account the line does not mention, so freight quoted
	by Shopify at 18% was billed at whatever rate the order's tax rows happened to state.

	On a real order -- 5% sarees, 18% kurtis, 3% jewellery and 200.00 of shipping -- the
	freight was charged 5% instead of 18%, and the Sales Order came out 26.00 short. The only
	reason anyone found out is that `assert_total_matches` refuses a document that does not
	total what the customer paid.
	"""
	rates = shipping_tax_rates(store_doc, order, side)
	if rates:
		return ensure_item_tax_template(store_doc.company, rates)
	return zero_rate_template(store_doc, order, side)


def build_shipping_row(store_doc, order: dict, side: str) -> dict | None:
	"""Shipping as an ERPNext charge row, when the store does not book it as a line item."""
	amount = net_shipping_total(order, side, bool(order.get("taxesIncluded")))
	if amount == ZERO:
		return None

	account = None
	for row in store_doc.tax_map or []:
		if cstr(row.charge_type) == "shipping":
			account = row.account_head
			break

	if not account:
		frappe.throw(
			_(
				"Order {0} has shipping of {1} but store {2} has no shipping account mapped. "
				"Add a Tax Map row with charge type 'shipping', or set a Shipping Item on the "
				"store to book shipping as a line instead."
			).format(order.get("name") or order.get("id"), amount, store_doc.name)
		)

	return {
		"charge_type": "Actual",
		"account_head": account,
		"description": shipping_titles(order),
		"tax_amount": to_float(quantize(amount)),
	}


# --------------------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------------------


def assert_total_matches(doc, order: dict, side: str) -> None:
	"""Refuse to post a document whose total disagrees with Shopify's (spec §13.1).

	The failure names both figures, because the only useful version of this error is the one
	that tells you how far out you are and in which direction.
	"""
	from shopify_integration.utils.money import from_document, totals_match

	expected = money_field(order, "totalPriceSet", side)
	computed = from_document(doc.get("grand_total"))
	precision = frappe.get_precision(doc.doctype, "grand_total") or 2

	if not totals_match(computed, expected, precision):
		frappe.throw(
			_(
				"{0} total {1} does not match Shopify order {2} total {3} (difference {4}). "
				"Refusing to submit a document whose total disagrees with the order."
			).format(
				doc.doctype,
				quantize(computed, precision),
				order.get("name") or order.get("id"),
				quantize(expected, precision),
				quantize(computed - expected, precision),
			)
		)
