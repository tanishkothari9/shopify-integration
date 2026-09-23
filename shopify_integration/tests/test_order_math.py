"""Tax, discount, shipping and currency maths (spec §13). No database needed."""

from decimal import Decimal

from shopify_integration.utils.taxes import (
	PRESENTMENT_MONEY,
	SHOP_MONEY,
	collect_tax_lines,
	money_field,
	money_side_for,
	shipping_titles,
	shipping_total,
)


def bag(shop, presentment=None):
	return {
		"shopMoney": {"amount": shop, "currencyCode": "USD"},
		"presentmentMoney": {"amount": presentment or shop, "currencyCode": "EUR"},
	}


def order(**overrides):
	base = {
		"id": "gid://shopify/Order/1",
		"name": "#1001",
		"currencyCode": "USD",
		"taxesIncluded": False,
		"totalPriceSet": bag("119.98"),
		"lineItems": {
			"nodes": [
				{
					"quantity": 2,
					"sku": "TEE",
					"originalUnitPriceSet": bag("50.00"),
					"discountedUnitPriceSet": bag("50.00"),
					"taxLines": [{"title": "State Tax", "rate": 0.1, "priceSet": bag("10.00")}],
				}
			],
			"pageInfo": {"hasNextPage": False},
		},
		"shippingLines": {
			"nodes": [
				{
					"title": "Standard",
					"originalPriceSet": bag("9.99"),
					"discountedPriceSet": bag("9.99"),
					"taxLines": [{"title": "State Tax", "rate": 0.1, "priceSet": bag("0.99")}],
				}
			]
		},
		"taxLines": [{"title": "State Tax", "rate": 0.1, "priceSet": bag("10.99")}],
	}
	base.update(overrides)
	return base


def test_money_field_reads_a_moneybag_as_decimal():
	assert money_field(order(), "totalPriceSet") == Decimal("119.98")
	assert isinstance(money_field(order(), "totalPriceSet"), Decimal)


def test_money_field_picks_the_requested_side():
	node = {"totalPriceSet": bag("100.00", "85.00")}
	assert money_field(node, "totalPriceSet", SHOP_MONEY) == Decimal("100.00")
	assert money_field(node, "totalPriceSet", PRESENTMENT_MONEY) == Decimal("85.00")


def test_missing_money_is_zero_not_an_error():
	assert money_field({}, "totalPriceSet") == Decimal("0")


def test_same_currency_uses_shop_money():
	"""No conversion needed, so the document carries the company's own figures."""
	assert money_side_for(order(currencyCode="USD"), "USD") == SHOP_MONEY


def test_foreign_currency_uses_presentment_money():
	"""ERPNext stores amounts in the document's currency and converts itself, so a EUR
	document must carry the EUR figures."""
	assert money_side_for(order(currencyCode="EUR"), "USD") == PRESENTMENT_MONEY


def test_tax_lines_are_collected_from_lines_and_shipping():
	collected = collect_tax_lines(order(), SHOP_MONEY)
	assert [e["amount"] for e in collected] == [Decimal("10.00"), Decimal("0.99")]


def test_order_level_tax_is_not_added_on_top_of_per_line_tax():
	"""The order-level taxLines are a summary of the per-line ones. Counting both doubles
	the tax -- a quiet way to produce wrong books."""
	collected = collect_tax_lines(order(), SHOP_MONEY)
	assert sum(e["amount"] for e in collected) == Decimal("10.99")


def test_order_level_tax_is_used_when_there_is_no_per_line_tax():
	stripped = order()
	stripped["lineItems"]["nodes"][0]["taxLines"] = []
	stripped["shippingLines"]["nodes"][0]["taxLines"] = []

	collected = collect_tax_lines(stripped, SHOP_MONEY)
	assert [e["amount"] for e in collected] == [Decimal("10.99")]


def test_no_tax_anywhere_is_an_empty_list():
	naked = order(taxLines=[], lineItems={"nodes": [], "pageInfo": {}}, shippingLines={"nodes": []})
	assert collect_tax_lines(naked, SHOP_MONEY) == []


def test_free_shipping_promotion_is_actually_free():
	"""A discounted price of zero must be honoured, not mistaken for a missing value.
	Falling back to the original price here bills the customer for shipping they were
	explicitly not charged for."""
	promo = order()
	promo["shippingLines"]["nodes"][0]["discountedPriceSet"] = bag("0.00")
	assert shipping_total(promo, SHOP_MONEY) == Decimal("0")


def test_discounted_shipping_price_is_preferred_over_the_original():
	half_price = order()
	half_price["shippingLines"]["nodes"][0]["discountedPriceSet"] = bag("4.99")
	assert shipping_total(half_price, SHOP_MONEY) == Decimal("4.99")


def test_shipping_falls_back_to_original_when_no_discount_field_is_present():
	no_discount = order()
	del no_discount["shippingLines"]["nodes"][0]["discountedPriceSet"]
	assert shipping_total(no_discount, SHOP_MONEY) == Decimal("9.99")


def test_shipping_total_sums_multiple_lines():
	multi = order()
	multi["shippingLines"]["nodes"].append(
		{"title": "Express", "originalPriceSet": bag("5.00"), "discountedPriceSet": bag("5.00")}
	)
	assert shipping_total(multi, SHOP_MONEY) == Decimal("14.99")


def test_no_shipping_is_zero():
	assert shipping_total(order(shippingLines={"nodes": []}), SHOP_MONEY) == Decimal("0")


def test_shipping_titles_are_joined_for_the_description():
	multi = order()
	multi["shippingLines"]["nodes"].append({"title": "Express", "originalPriceSet": bag("5.00")})
	assert shipping_titles(multi) == "Standard, Express"


def test_shipping_titles_fall_back_when_unnamed():
	assert shipping_titles(order(shippingLines={"nodes": []})) == "Shipping"


def test_line_totals_stay_exact_in_decimal():
	"""Two lines at 0.1 and 0.2 must sum to 0.3, which is the whole reason for Decimal."""
	from shopify_integration.utils.money import parse_money

	total = parse_money("0.10") + parse_money("0.20")
	assert total == Decimal("0.30")


# --------------------------------------------------------------------------------------
# Per-line GST rates (spec §13)
# --------------------------------------------------------------------------------------


class _Row:
	def __init__(self, title, account):
		self.shopify_tax_title = title
		self.account_head = account
		self.charge_type = "sales_tax"


class _Store:
	"""Enough of a Shopify Store to resolve tax titles to accounts."""

	name = "Test"
	company = "Test Co"
	shipping_item = None

	def __init__(self):
		self.tax_map = [
			_Row("CGST", "Output Tax CGST - TC"),
			_Row("SGST", "Output Tax SGST - TC"),
		]


def gst_line(net, cgst_rate, cgst_amount, sgst_rate=None, sgst_amount=None):
	"""A Shopify line with CGST and SGST stated both as rates and as amounts."""
	return {
		"quantity": 1,
		"discountedTotalSet": bag(net),
		"discountedUnitPriceSet": bag(net),
		"taxLines": [
			{"title": "CGST", "rate": cgst_rate, "priceSet": bag(cgst_amount)},
			{"title": "SGST", "rate": sgst_rate or cgst_rate, "priceSet": bag(sgst_amount or cgst_amount)},
		],
	}


def test_each_line_gets_its_own_gst_rate():
	"""A saree at 5% and a kurti at 18% in one order must not be blended.

	One percentage per account across the whole order is the obvious implementation and it is
	wrong: it bills both lines at an average that is not a GST rate at all, and misstates the
	taxable value reported against each HSN code.
	"""
	from shopify_integration.utils.taxes import line_tax_rates

	order = {"taxesIncluded": False}
	saree = gst_line("1000.00", 0.025, "25.00")
	kurti = gst_line("1000.00", 0.09, "90.00")

	assert line_tax_rates(_Store(), saree, order, SHOP_MONEY) == {
		"Output Tax CGST - TC": Decimal("2.5"),
		"Output Tax SGST - TC": Decimal("2.5"),
	}
	assert line_tax_rates(_Store(), kurti, order, SHOP_MONEY) == {
		"Output Tax CGST - TC": Decimal("9"),
		"Output Tax SGST - TC": Decimal("9"),
	}


def test_artificial_jewellery_sits_at_its_own_three_percent():
	from shopify_integration.utils.taxes import line_tax_rates

	jewellery = gst_line("2000.00", 0.015, "30.00")
	rates = line_tax_rates(_Store(), jewellery, {"taxesIncluded": False}, SHOP_MONEY)

	assert sum(rates.values()) == Decimal("3")


def test_a_tax_inclusive_line_is_rated_on_its_net_not_its_gross():
	"""1050 gross at 5% is 1000 net plus 50 tax -- not 1050 x 5%."""
	from shopify_integration.utils.taxes import line_tax_rates

	line = gst_line("1050.00", 0.025, "25.00")
	rates = line_tax_rates(_Store(), line, {"taxesIncluded": True}, SHOP_MONEY)

	assert sum(rates.values()) == Decimal("5")


def test_the_stated_rate_is_used_when_it_reconciles():
	"""A clean 18% belongs on the invoice, not 17.9999%."""
	from shopify_integration.utils.taxes import _percent_for

	tax = {"rate": 0.09, "priceSet": bag("90.00")}
	assert _percent_for(tax, Decimal("1000.00"), SHOP_MONEY) == Decimal("9")


def test_the_amount_wins_when_the_stated_rate_disagrees():
	"""The customer paid the amount, so the document has to total to it.

	Shopify rounds the rate for display, and some tax services state one against a base that is
	not this line's net. Trusting the rate there leaves the document short and it is refused.
	"""
	from shopify_integration.utils.taxes import _percent_for

	tax = {"rate": 0.1, "priceSet": bag("10.00")}
	assert _percent_for(tax, Decimal("90.00"), SHOP_MONEY) == Decimal("10.00") / Decimal("90.00") * 100


def test_a_line_with_no_stated_rate_falls_back_to_the_amount():
	from shopify_integration.utils.taxes import _percent_for

	tax = {"priceSet": bag("50.00")}
	assert _percent_for(tax, Decimal("1000.00"), SHOP_MONEY) == Decimal("5")


def test_an_untaxed_line_asks_for_no_template():
	from shopify_integration.utils.taxes import line_tax_rates

	line = {"quantity": 1, "discountedTotalSet": bag("500.00"), "taxLines": []}
	assert line_tax_rates(_Store(), line, {"taxesIncluded": False}, SHOP_MONEY) == {}


def test_an_untaxed_line_asks_for_a_zero_rate_template():
	"""A line Shopify charged no tax on still needs a template, and it has to name the same
	accounts at zero.

	Leaving it without one is silently wrong rather than loudly wrong: ERPNext falls back to a
	tax row's own rate for any account the line's map does not mention, so a book sold beside
	an 18% kurti is charged 18%. On a tax-inclusive order the grand total still reconciles, so
	nothing is refused -- the exempt line just reports a smaller taxable value and a tax the
	customer never paid.
	"""
	from unittest.mock import patch

	from shopify_integration.utils import taxes

	order = {
		"taxesIncluded": True,
		"lineItems": {
			"nodes": [
				gst_line("1180.00", 0.09, "90.00"),
				{"quantity": 1, "discountedTotalSet": bag("500.00"), "taxLines": []},
			]
		},
	}
	exempt = order["lineItems"]["nodes"][1]

	with patch.object(taxes, "ensure_item_tax_template", return_value="Zero") as ensure:
		taxes.item_tax_template_for(_Store(), exempt, order, SHOP_MONEY)

	rates, kwargs = ensure.call_args.args[1], ensure.call_args.kwargs
	assert set(rates) == {"Output Tax CGST - TC", "Output Tax SGST - TC"}, (
		"the zero template must name every account the order taxes to, or ERPNext falls back "
		"to the row rate for the ones it omits"
	)
	assert all(v == Decimal("0") for v in rates.values())
	assert kwargs.get("taxable") is False


def test_an_order_with_no_tax_at_all_needs_no_template():
	"""Nothing to be exempt from. A store selling untaxed goods everywhere gets plain rows."""
	from shopify_integration.utils import taxes

	order = {
		"taxesIncluded": False,
		"lineItems": {"nodes": [{"quantity": 1, "discountedTotalSet": bag("500.00"), "taxLines": []}]},
	}
	line = order["lineItems"]["nodes"][0]

	assert taxes.item_tax_template_for(_Store(), line, order, SHOP_MONEY) is None


# ---------------------------------------------------------------------------------------
# Freight carries its own GST rate
# ---------------------------------------------------------------------------------------


def _shipped_order(ship_net, ship_rate, ship_amount):
	"""An order whose goods are 5% and whose freight is quoted at a different rate."""
	return order(
		lineItems={"nodes": [gst_line("10000.00", 0.025, "250.00")]},
		shippingLines={
			"nodes": [
				{
					"title": "Standard Shipping",
					"originalPriceSet": bag(ship_net),
					"discountedPriceSet": bag(ship_net),
					"taxLines": [
						{"title": "CGST", "rate": ship_rate, "priceSet": bag(ship_amount)},
						{"title": "SGST", "rate": ship_rate, "priceSet": bag(ship_amount)},
					],
				}
			]
		},
	)


def test_freight_is_taxed_at_its_own_rate_not_the_goods_rate():
	"""The regression.

	Freight booked as a line item carried no Item Tax Template at all, so ERPNext fell back
	to the order's tax rows and billed 200.00 of 18% shipping at 5%. On a live Indian order
	that made the Sales Order 26.00 short of what the customer paid.
	"""
	from shopify_integration.utils.taxes import shipping_tax_rates

	rates = shipping_tax_rates(_Store(), _shipped_order("200.00", 0.09, "18.00"), SHOP_MONEY)

	assert rates["Output Tax CGST - TC"] == Decimal("9")
	assert rates["Output Tax SGST - TC"] == Decimal("9")


def test_freight_rate_is_independent_of_the_goods_rate():
	"""Same order, cheaper freight slab: the goods must not drag it along."""
	from shopify_integration.utils.taxes import shipping_tax_rates

	rates = shipping_tax_rates(_Store(), _shipped_order("200.00", 0.025, "5.00"), SHOP_MONEY)

	assert rates["Output Tax CGST - TC"] == Decimal("2.5")


def test_untaxed_freight_yields_no_rates():
	"""Free or exempt shipping must not invent a rate; the caller falls back to a 0% template."""
	from shopify_integration.utils.taxes import shipping_tax_rates

	o = order(
		lineItems={"nodes": [gst_line("10000.00", 0.025, "250.00")]},
		shippingLines={
			"nodes": [
				{
					"title": "Free",
					"originalPriceSet": bag("0.00"),
					"discountedPriceSet": bag("0.00"),
					"taxLines": [],
				}
			]
		},
	)
	assert shipping_tax_rates(_Store(), o, SHOP_MONEY) == {}


def test_two_shipping_lines_are_measured_against_the_combined_net():
	"""Shipping reaches ERPNext as one row, so the rate has to describe the whole of it.

	Two 100.00 lines, one at 18% and one at 5%: the row is 200.00 carrying 23.00 of tax,
	which is 11.5%, not 23%.
	"""
	from shopify_integration.utils.taxes import shipping_tax_rates

	def leg(rate, amount):
		return {
			"title": "Leg",
			"originalPriceSet": bag("100.00"),
			"discountedPriceSet": bag("100.00"),
			"taxLines": [{"title": "CGST", "rate": rate, "priceSet": bag(amount)}],
		}

	o = order(
		lineItems={"nodes": [gst_line("10000.00", 0.025, "250.00")]},
		shippingLines={"nodes": [leg(0.09, "9.00"), leg(0.025, "2.50")]},
	)
	assert shipping_tax_rates(_Store(), o, SHOP_MONEY) == {"Output Tax CGST - TC": Decimal("5.75")}
