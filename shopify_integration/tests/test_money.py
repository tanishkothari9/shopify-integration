"""Money must never be a float (spec §13.1, non-negotiable #5)."""

from decimal import Decimal

import pytest

from shopify_integration.utils.money import (
	parse_money,
	parse_money_field,
	quantize,
	totals_match,
)


def test_parses_shopify_decimal_strings_exactly():
	assert parse_money("19.99") == Decimal("19.99")
	assert parse_money("0.01") == Decimal("0.01")
	assert parse_money("1234567.89") == Decimal("1234567.89")


def test_none_is_zero():
	assert parse_money(None) == Decimal("0")


def test_rejects_float_input():
	"""The whole point: a float has already lost precision by the time we see it."""
	with pytest.raises(TypeError, match="float"):
		parse_money(19.99, field="total_price")


def test_float_error_names_the_field():
	with pytest.raises(TypeError, match="total_price"):
		parse_money(19.99, field="total_price")


def test_rejects_garbage():
	with pytest.raises(ValueError, match="line_price"):
		parse_money("not-a-number", field="line_price")


def test_summing_strings_stays_exact_where_float_would_drift():
	"""0.1 + 0.2 != 0.3 in binary floating point. It must here."""
	total = parse_money("0.10") + parse_money("0.20")
	assert total == Decimal("0.30")
	assert 0.1 + 0.2 != 0.3  # the bug this module exists to prevent


def test_hundred_cents_sum_to_one_unit():
	total = sum((parse_money("0.01") for _ in range(100)), Decimal("0"))
	assert total == Decimal("1.00")


def test_parses_nested_money_bag():
	payload = {"totalPriceSet": {"shopMoney": {"amount": "42.50", "currencyCode": "USD"}}}
	assert parse_money_field(payload, "totalPriceSet", "shopMoney", "amount") == Decimal("42.50")


def test_missing_nested_key_is_zero_not_an_error():
	assert parse_money_field({}, "totalPriceSet", "shopMoney", "amount") == Decimal("0")


def test_quantize_rounds_half_up_not_bankers():
	"""Python rounds 2.675 to 2.67 by default; ERPNext and Shopify both round it up."""
	assert quantize(Decimal("2.675")) == Decimal("2.68")
	assert quantize(Decimal("0.125")) == Decimal("0.13")


def test_totals_match_tolerates_one_cent_of_allocation_rounding():
	assert totals_match(Decimal("100.00"), Decimal("100.01"))
	assert totals_match(Decimal("100.01"), Decimal("100.00"))


def test_totals_match_rejects_a_real_discrepancy():
	assert not totals_match(Decimal("100.00"), Decimal("100.02"))
	assert not totals_match(Decimal("100.00"), Decimal("110.00"))


def test_from_document_goes_via_string_not_binary_float():
	"""Decimal(119.99) is 119.98999999999999488...; Decimal("119.99") is exact. Reading a
	Frappe Currency field back must take the second route."""
	from shopify_integration.utils.money import from_document

	assert from_document(119.99) == Decimal("119.99")
	assert from_document(119.99) != Decimal(119.99)  # noqa: RUF032 -- the point of the test


def test_from_document_handles_none_and_decimal():
	from shopify_integration.utils.money import from_document

	assert from_document(None) == Decimal("0")
	assert from_document(Decimal("5.50")) == Decimal("5.50")


def test_from_document_and_parse_money_agree():
	from shopify_integration.utils.money import from_document

	assert from_document(0.1) + from_document(0.2) == parse_money("0.30")
