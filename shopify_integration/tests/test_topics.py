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


# --------------------------------------------------------------------------------------
# Duplicate subscriptions
#
# webhookSubscriptionCreate takes no idempotency key and the client retries, so a create
# Shopify accepted but whose reply never arrived leaves a second subscription on the topic.
# Both then deliver, each with its own webhook id, which the event log's unique webhook_id
# does not collapse. A live shop was found carrying two CUSTOMERS_UPDATE and two
# INVENTORY_LEVELS_UPDATE subscriptions, created seconds apart in one registration run.
# --------------------------------------------------------------------------------------

from unittest.mock import patch

from shopify_integration.api import webhooks


TARGET = "https://site.example.com/api/method/shopify_integration.inbound.webhook.webhook"


def sub(sub_id, topic, url=TARGET):
	return {"id": f"gid://shopify/WebhookSubscription/{sub_id}", "topic": topic,
	        "endpoint": {"callbackUrl": url}}


class _Client:
	"""Records the mutations register/unregister ask for."""

	def __init__(self, nodes):
		self.nodes = nodes
		self.deleted, self.created = [], []

	def paginate(self, query, variables, key):
		return list(self.nodes)

	def execute(self, query, variables, cost_hint=0):
		if "id" in variables:
			self.deleted.append(variables["id"])
		else:
			self.created.append(variables.get("topic"))
		return {}


def test_every_subscription_on_a_topic_is_seen_not_just_the_last():
	client = _Client([sub(1, "CUSTOMERS_UPDATE"), sub(2, "CUSTOMERS_UPDATE")])
	with patch.object(webhooks, "load_query", return_value="q"):
		found = webhooks.existing_subscriptions(client)
	assert len(found["customers/update"]) == 2


def test_a_duplicate_subscription_is_deleted_and_the_good_one_kept():
	"""The regression: two live subscriptions on one topic, both delivering."""
	client = _Client([sub(1, "CUSTOMERS_UPDATE"), sub(2, "CUSTOMERS_UPDATE")])
	with patch.object(webhooks, "load_query", return_value="q"):
		result = webhooks.register(client, "https://site.example.com", ("customers/update",))

	assert len(client.deleted) == 1
	assert client.created == []
	assert result["removed"] == ["customers/update"]
	assert result["unchanged"] == ["customers/update"]


def test_a_stale_duplicate_is_cleared_and_one_good_subscription_created():
	client = _Client([sub(1, "ORDERS_CREATE", "https://old-tunnel.example.com/hook"),
	                  sub(2, "ORDERS_CREATE", "https://older-tunnel.example.com/hook")])
	with patch.object(webhooks, "load_query", return_value="q"):
		result = webhooks.register(client, "https://site.example.com", ("orders/create",))

	assert len(client.deleted) == 2
	assert client.created == ["ORDERS_CREATE"]
	assert result["replaced"] == ["orders/create"]


def test_a_single_correct_subscription_is_left_completely_alone():
	client = _Client([sub(1, "ORDERS_CREATE")])
	with patch.object(webhooks, "load_query", return_value="q"):
		result = webhooks.register(client, "https://site.example.com", ("orders/create",))

	assert client.deleted == [] and client.created == []
	assert result["unchanged"] == ["orders/create"] and result["removed"] == []


def test_disabling_a_store_removes_every_copy():
	"""Deleting one of two left the other posting at an endpoint that no longer answers."""
	client = _Client([sub(1, "CUSTOMERS_UPDATE"), sub(2, "CUSTOMERS_UPDATE")])
	with patch.object(webhooks, "load_query", return_value="q"):
		result = webhooks.unregister(client, ("customers/update",))

	assert len(client.deleted) == 2
	assert result["removed"] == ["customers/update", "customers/update"]
