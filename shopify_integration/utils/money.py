"""Decimal money handling (spec §13.1, non-negotiable #5).

Shopify sends money as decimal *strings* (``"19.99"``). Passing one through ``float()``
introduces binary representation error that compounds across line items, taxes and
discounts, and surfaces as a document whose total is a cent away from the order it came
from. Books that are quietly a cent wrong are worse than an integration that refuses to
post, so every amount enters the app through ``parse_money`` and stays a ``Decimal`` until
it is written to a Frappe Currency field.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

ZERO = Decimal("0")


def parse_money(value, field: str = "amount") -> Decimal:
	"""Convert a Shopify money value to Decimal.

	``float`` input is rejected outright rather than coerced: accepting it would let a
	float slip in from a caller that had already lost precision upstream, and the whole
	point of this module is that such a caller is a bug to be found, not tolerated.
	"""
	if value is None:
		return ZERO
	if isinstance(value, Decimal):
		return value
	if isinstance(value, float):
		raise TypeError(
			f"{field} was passed as float ({value!r}); parse Shopify money from its string "
			"form to avoid representation error"
		)
	if isinstance(value, int):
		return Decimal(value)
	try:
		return Decimal(str(value).strip())
	except (InvalidOperation, ValueError):
		raise ValueError(f"Could not parse {field} as a decimal amount: {value!r}")


def parse_money_field(payload: dict | None, *keys: str, field: str = "amount") -> Decimal:
	"""Parse a nested money value, e.g. ``{"shopMoney": {"amount": "19.99"}}``."""
	node = payload or {}
	for key in keys:
		if not isinstance(node, dict):
			return ZERO
		node = node.get(key)
		if node is None:
			return ZERO
	return parse_money(node, field=field)


def quantize(value: Decimal, precision: int = 2) -> Decimal:
	"""Round to a currency precision, half-up.

	Half-up rather than Python's default banker's rounding, because that is what ERPNext
	and Shopify both do; matching them is what keeps totals reconcilable.
	"""
	exponent = Decimal(1).scaleb(-precision)
	return value.quantize(exponent, rounding=ROUND_HALF_UP)


def totals_match(computed: Decimal, expected: Decimal, precision: int = 2) -> bool:
	"""True when two totals agree within one smallest unit of the currency.

	One unit of tolerance, not zero: Shopify allocates rounding across lines in its own
	order, so a correctly built document can legitimately land a cent away. Anything larger
	is a real discrepancy and must fail loudly rather than post.
	"""
	tolerance = Decimal(1).scaleb(-precision)
	return abs(quantize(computed, precision) - quantize(expected, precision)) <= tolerance


def from_document(value) -> Decimal:
	"""Read a value back out of an ERPNext document as a Decimal.

	The one legitimate float-to-Decimal path in the app, and it must go via ``str``:
	``Decimal(119.99)`` is 119.98999999999999488..., which would reintroduce exactly the
	error this module exists to prevent, whereas ``Decimal("119.99")`` is exact.

	Frappe stores Currency fields as floats, so reading one back is unavoidable. Converting
	here, at the boundary, keeps every calculation above it exact.
	"""
	if value is None:
		return ZERO
	if isinstance(value, Decimal):
		return value
	return Decimal(str(value))


def to_float(value: Decimal) -> float:
	"""Final conversion at the document boundary, where Frappe requires a float.

	Call this once, as late as possible -- never mid-calculation.
	"""
	return float(value)
