"""Webhook subscription management (spec §8.2).

Two spellings, one topic
------------------------
Shopify names every webhook topic twice and the app sees both:

* the ``X-Shopify-Topic`` header on a delivered webhook is slash-form -- ``orders/create``
* the GraphQL ``WebhookSubscriptionTopic`` enum is screaming-snake -- ``ORDERS_CREATE``

The spec's §8.2 table lists only the slash form, which is correct for the receiver and
wrong for ``webhookSubscriptionCreate``. Rather than maintain two hand-written tables that
can drift apart, the conversion is derived, and the slash form is treated as canonical
throughout the app because that is what arrives at runtime.

Subscriptions are registered programmatically when a store is enabled and removed when it
is disabled. Users are never asked to configure webhooks by hand.
"""

from __future__ import annotations

from shopify_integration.api.client import ShopifyClient, load_query

#: Topics this app subscribes to, in slash form (spec §8.2).
#:
#: Phase 1 registers all of them but only logs what arrives -- the receiver answers 200 and
#: marks unrecognised topics Skipped, so subscribing ahead of the handlers is safe and means
#: no re-registration is needed as later phases land.
REQUIRED_TOPICS: tuple[str, ...] = (
	"orders/create",
	"orders/paid",
	"orders/fulfilled",
	"orders/partially_fulfilled",
	"orders/cancelled",
	"refunds/create",
	"products/create",
	"products/update",
	"products/delete",
	"inventory_levels/update",
	"customers/create",
	"customers/update",
)


def to_graphql_topic(topic: str) -> str:
	"""``orders/create`` -> ``ORDERS_CREATE``."""
	return topic.strip().replace("/", "_").replace("-", "_").upper()


#: Reverse lookup, derived from the canonical list above so the two cannot drift.
_ENUM_TO_SLASH: dict[str, str] = {to_graphql_topic(topic): topic for topic in REQUIRED_TOPICS}


def to_header_topic(enum_value: str) -> str:
	"""``ORDERS_CREATE`` -> ``orders/create``.

	This direction is a lookup rather than a derivation, because the enum spelling is
	genuinely ambiguous: the slash falls after the first underscore in
	``ORDERS_PARTIALLY_FULFILLED`` (a two-word *action*) but after the second in
	``INVENTORY_LEVELS_UPDATE`` (a two-word *resource*). No rule over the string alone gets
	both right, so the mapping is derived from REQUIRED_TOPICS, which is slash-form and
	authoritative.

	Unknown topics -- ones Shopify adds, or another app's subscriptions -- fall back to
	splitting at the first underscore. That is a guess, and it is only ever used for
	display, never for matching.
	"""
	normalised = enum_value.strip().upper()
	if normalised in _ENUM_TO_SLASH:
		return _ENUM_TO_SLASH[normalised]

	lowered = normalised.lower()
	return lowered.replace("_", "/", 1) if "_" in lowered else lowered


def callback_url(site_url: str) -> str:
	"""The public endpoint Shopify posts to."""
	method = "shopify_integration.inbound.webhook.webhook"
	return f"{site_url.rstrip('/')}/api/method/{method}"


def existing_subscriptions(client: ShopifyClient) -> dict[str, list[dict]]:
	"""Map slash-form topic -> every subscription this app owns for it.

	A list, not a single node, because one topic can genuinely carry more than one.
	``webhookSubscriptionCreate`` takes no idempotency key and the client retries, so a create
	Shopify accepted but whose reply never arrived leaves a second subscription behind. Keeping
	one node per topic hid those twins completely: ``register`` tidied the one it could see and
	``unregister`` left the other posting at a dead endpoint, and neither could ever heal it.
	Two subscriptions mean two deliveries with two webhook ids, which the event log's unique
	``webhook_id`` does not collapse -- that only catches Shopify resending one delivery.

	Shopify only ever returns the subscriptions belonging to the authenticated app, so this
	cannot see or disturb another app's webhooks on the same shop.
	"""
	found: dict[str, list[dict]] = {}
	for node in client.paginate(load_query("webhook_subscriptions"), {}, "webhookSubscriptions"):
		found.setdefault(to_header_topic(node.get("topic", "")), []).append(node)
	return found


def register(client: ShopifyClient, site_url: str, topics: tuple[str, ...] = REQUIRED_TOPICS) -> dict:
	"""Ensure a subscription exists for every required topic, pointing at this site.

	Reconciles rather than blindly creating: one subscription already pointing at the right
	URL is kept, and everything else on that topic -- a stale URL from a site that moved or a
	tunnel that was recreated, and any duplicate left by a retried create -- is deleted.
	Enabling a store is therefore both a no-op when all is well and the cure when it is not.
	"""
	from shopify_integration.exceptions import ShopifyUserError

	target = callback_url(site_url)
	existing = existing_subscriptions(client)
	created, replaced, unchanged, removed, failed = [], [], [], [], []

	for topic in topics:
		try:
			current = existing.get(topic) or []
			keep = next(
				(node for node in current if (node.get("endpoint") or {}).get("callbackUrl") == target),
				None,
			)
			for extra in current:
				if extra is keep:
					continue
				client.execute(load_query("webhook_subscription_delete"), {"id": extra["id"]}, cost_hint=10)
				removed.append(topic)

			if keep:
				unchanged.append(topic)
				continue

			client.execute(
				load_query("webhook_subscription_create"),
				{"topic": to_graphql_topic(topic), "webhookSubscription": {"uri": target}},
				cost_hint=10,
			)
			(replaced if current else created).append(topic)
		except ShopifyUserError as exc:
			# One topic Shopify refuses must not cost us the eleven it would accept. Order and
			# customer topics need Protected Customer Data approval, which a new app does not
			# have; products and inventory need no approval and should still work meanwhile.
			failed.append({"topic": topic, "reason": str(exc)})

	return {
		"created": created,
		"replaced": replaced,
		"unchanged": unchanged,
		"removed": removed,
		"failed": failed,
		"callback_url": target,
	}


def unregister(client: ShopifyClient, topics: tuple[str, ...] = REQUIRED_TOPICS) -> dict:
	"""Remove this app's subscriptions for the given topics. Used when a store is disabled."""
	existing = existing_subscriptions(client)
	removed = []
	for topic in topics:
		for node in existing.get(topic) or []:
			client.execute(load_query("webhook_subscription_delete"), {"id": node["id"]}, cost_hint=10)
			removed.append(topic)
	return {"removed": removed}
