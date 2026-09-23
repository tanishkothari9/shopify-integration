"""Webhook topics have two spellings and the app must not confuse them (spec §8.2)."""

import pytest

from shopify_integration.api.webhooks import (
	REQUIRED_TOPICS,
	callback_url,
	to_graphql_topic,
	to_header_topic,
)


@pytest.mark.parametrize(
	("slash", "enum"),
	[
		("orders/create", "ORDERS_CREATE"),
		("orders/paid", "ORDERS_PAID"),
		("refunds/create", "REFUNDS_CREATE"),
		("products/delete", "PRODUCTS_DELETE"),
		("inventory_levels/update", "INVENTORY_LEVELS_UPDATE"),
		("orders/partially_fulfilled", "ORDERS_PARTIALLY_FULFILLED"),
	],
)
def test_topic_spellings_convert_both_ways(slash, enum):
	assert to_graphql_topic(slash) == enum
	assert to_header_topic(enum) == slash


def test_multiword_action_keeps_its_underscores():
	"""The regression this guards: a naive replace gives orders/partially/fulfilled."""
	assert to_header_topic("ORDERS_PARTIALLY_FULFILLED") == "orders/partially_fulfilled"
	assert to_header_topic("INVENTORY_LEVELS_UPDATE") == "inventory_levels/update"


def test_every_required_topic_round_trips():
	for topic in REQUIRED_TOPICS:
		assert to_header_topic(to_graphql_topic(topic)) == topic


def test_callback_url_is_the_receiver_endpoint():
	url = callback_url("https://erp.example.com/")
	assert url == "https://erp.example.com/api/method/shopify_integration.inbound.webhook.webhook"


def test_callback_url_tolerates_a_missing_trailing_slash():
	assert callback_url("https://erp.example.com") == callback_url("https://erp.example.com/")


def test_unknown_topic_falls_back_without_raising():
	"""Shopify adds topics between releases; an unrecognised one must not crash the
	subscription listing. The guess is display-only and never used for matching."""
	assert to_header_topic("SOMETHING_NEW") == "something/new"
	assert to_header_topic("BULKOPERATIONSFINISH") == "bulkoperationsfinish"


def test_reverse_lookup_beats_the_heuristic_for_known_topics():
	"""The canonical list wins wherever the string alone would be ambiguous."""
	assert to_header_topic("INVENTORY_LEVELS_UPDATE") == "inventory_levels/update"
	assert to_header_topic("ORDERS_PARTIALLY_FULFILLED") == "orders/partially_fulfilled"
