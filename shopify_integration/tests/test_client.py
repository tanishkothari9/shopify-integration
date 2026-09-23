"""GraphQL client error taxonomy (spec §6.3) -- three failure layers, three behaviours."""

import pytest
import requests

from shopify_integration.api.client import ShopifyClient, StoreCredentials
from shopify_integration.api.throttle import AdaptiveThrottle, InMemoryCache
from shopify_integration.exceptions import (
	ShopifyGraphQLError,
	ShopifyThrottled,
	ShopifyTransportError,
	ShopifyUserError,
)
from shopify_integration.tests.conftest import (
	FakeClock,
	FakeResponse,
	FakeSession,
	throttle_extensions,
)

CREDS = StoreCredentials(
	store="test-store",
	shop_domain="my-shop.myshopify.com",
	access_token="shpat_EXAMPLE_NOT_A_REAL_TOKEN",
	api_version="2026-01",
)


def make_client(responses):
	clock = FakeClock()
	throttle = AdaptiveThrottle("test-store", InMemoryCache(), clock=clock, sleeper=clock.sleep)
	session = FakeSession(responses)
	client = ShopifyClient(CREDS, throttle=throttle, session=session, sleeper=clock.sleep)
	return client, session, clock


def test_endpoint_and_auth_header_match_the_pinned_version():
	client, session, _ = make_client([FakeResponse({"data": {"shop": {"name": "Acme"}}})])
	client.execute("query { shop { name } }")

	request = session.requests[0]
	assert request["url"] == "https://my-shop.myshopify.com/admin/api/2026-01/graphql.json"
	assert request["headers"]["X-Shopify-Access-Token"] == "shpat_EXAMPLE_NOT_A_REAL_TOKEN"


def test_returns_data_on_success():
	client, _, _ = make_client([FakeResponse({"data": {"shop": {"name": "Acme"}}})])
	assert client.execute("query { shop { name } }") == {"shop": {"name": "Acme"}}


def test_credentials_repr_never_leaks_the_token():
	"""Tokens must not reach a log line or a traceback frame (spec §15)."""
	assert "shpat_EXAMPLE_NOT_A_REAL_TOKEN" not in repr(CREDS)
	assert "***" in repr(CREDS)


# --- layer 3: userErrors. HTTP 200, no errors[], and the write silently did nothing ---


def test_user_errors_raise_even_though_the_response_is_a_200():
	payload = {
		"data": {
			"inventorySetQuantities": {
				"inventoryAdjustmentGroup": None,
				"userErrors": [{"field": ["input", "quantities"], "message": "Location not found"}],
			}
		}
	}
	client, _, _ = make_client([FakeResponse(payload)])
	with pytest.raises(ShopifyUserError, match="Location not found"):
		client.execute("mutation { ... }")


def test_user_error_is_permanent_so_the_queue_will_not_retry_it():
	payload = {"data": {"productCreate": {"userErrors": [{"field": ["title"], "message": "blank"}]}}}
	client, _, _ = make_client([FakeResponse(payload)])
	with pytest.raises(ShopifyUserError) as exc:
		client.execute("mutation { ... }")
	assert exc.value.retryable is False


def test_user_error_message_names_the_field():
	payload = {"data": {"productCreate": {"userErrors": [{"field": ["title"], "message": "blank"}]}}}
	client, _, _ = make_client([FakeResponse(payload)])
	with pytest.raises(ShopifyUserError, match="title: blank"):
		client.execute("mutation { ... }")


def test_empty_user_errors_is_success():
	payload = {"data": {"productCreate": {"product": {"id": "gid://1"}, "userErrors": []}}}
	client, _, _ = make_client([FakeResponse(payload)])
	assert client.execute("mutation { ... }")["productCreate"]["product"]["id"] == "gid://1"


# --- layer 2: top-level errors[] ---


def test_throttled_graphql_error_retries_and_then_succeeds():
	throttled = FakeResponse(
		{
			"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
			"extensions": throttle_extensions(currently_available=0.0),
		}
	)
	ok = FakeResponse({"data": {"shop": {"name": "Acme"}}, "extensions": throttle_extensions()})
	client, _, _ = make_client([throttled, ok])
	assert client.execute("query { shop { name } }") == {"shop": {"name": "Acme"}}


def test_max_cost_exceeded_is_permanent_not_a_throttle():
	"""The spec's §6.3 table misses this: it looks like THROTTLED but retrying loops forever,
	because the query is larger than the entire bucket and always will be."""
	payload = {"errors": [{"message": "Query cost is too high", "extensions": {"code": "MAX_COST_EXCEEDED"}}]}
	client, session, _ = make_client([payload and FakeResponse(payload)])
	with pytest.raises(ShopifyGraphQLError) as exc:
		client.execute("query { everything }")
	assert exc.value.retryable is False
	assert len(session.requests) == 1  # tried exactly once


def test_access_denied_is_permanent():
	payload = {"errors": [{"message": "Access denied", "extensions": {"code": "ACCESS_DENIED"}}]}
	client, session, _ = make_client([FakeResponse(payload)])
	with pytest.raises(ShopifyGraphQLError):
		client.execute("query { shop { name } }")
	assert len(session.requests) == 1


def test_malformed_query_is_permanent():
	payload = {"errors": [{"message": "Field 'nope' doesn't exist"}]}
	client, session, _ = make_client([FakeResponse(payload)])
	with pytest.raises(ShopifyGraphQLError, match="doesn't exist"):
		client.execute("query { nope }")
	assert len(session.requests) == 1


# --- layer 1: transport ---


def test_http_429_is_retryable_and_penalises_the_throttle():
	client, _, _ = make_client([FakeResponse(None, status_code=429, headers={"Retry-After": "2"}, text="")])
	with pytest.raises(ShopifyThrottled) as exc:
		client.execute("query { shop { name } }")
	assert exc.value.retryable is True
	assert client.throttle.read_state().currently_available == 0.0


def test_http_500_retries_up_to_the_attempt_limit():
	client, session, _ = make_client([FakeResponse(None, status_code=500, text="boom")])
	with pytest.raises(ShopifyTransportError):
		client.execute("query { shop { name } }")
	assert len(session.requests) == 3  # DEFAULT_MAX_ATTEMPTS


def test_transient_500_then_success():
	client, session, _ = make_client(
		[FakeResponse(None, status_code=500, text="boom"), FakeResponse({"data": {"shop": {"id": "1"}}})]
	)
	assert client.execute("query { shop { id } }") == {"shop": {"id": "1"}}
	assert len(session.requests) == 2


def test_network_failure_is_a_transport_error():
	client, _, _ = make_client([requests.ConnectionError("connection reset")])
	with pytest.raises(ShopifyTransportError, match="connection reset"):
		client.execute("query { shop { name } }")


def test_http_400_is_permanent_and_not_retried():
	client, session, _ = make_client([FakeResponse(None, status_code=400, text="Bad Request")])
	with pytest.raises(ShopifyGraphQLError):
		client.execute("query { shop { name } }")
	assert len(session.requests) == 1


def test_non_json_body_is_a_transport_error():
	client, _, _ = make_client([FakeResponse(None, status_code=200, text="<html>502</html>")])
	with pytest.raises(ShopifyTransportError, match="non-JSON"):
		client.execute("query { shop { name } }")


# --- throttle integration and pagination ---


def test_every_response_teaches_the_throttle():
	client, _, _ = make_client(
		[FakeResponse({"data": {"shop": {}}, "extensions": throttle_extensions(maximum=2000.0)})]
	)
	client.execute("query { shop { name } }")
	assert client.throttle.read_state().maximum_available == 2000.0


def test_reservation_is_released_even_when_the_call_fails():
	"""A leaked reservation would shrink the bucket permanently."""
	client, _, _ = make_client([FakeResponse(None, status_code=400, text="Bad Request")])
	before = client.throttle.headroom()
	with pytest.raises(ShopifyGraphQLError):
		client.execute("query { bad }")
	assert client.throttle.headroom() == before


def test_paginate_follows_cursors_across_pages():
	page_1 = FakeResponse(
		{
			"data": {
				"products": {
					"edges": [{"node": {"id": "1"}}, {"node": {"id": "2"}}],
					"pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
				}
			}
		}
	)
	page_2 = FakeResponse(
		{
			"data": {
				"products": {
					"edges": [{"node": {"id": "3"}}],
					"pageInfo": {"hasNextPage": False, "endCursor": None},
				}
			}
		}
	)
	client, session, _ = make_client([page_1, page_2])
	assert [n["id"] for n in client.paginate("query", {}, "products")] == ["1", "2", "3"]
	assert session.requests[1]["body"]["variables"]["cursor"] == "cursor-1"


def test_paginate_stops_rather_than_looping_when_a_cursor_is_missing():
	"""hasNextPage with no endCursor would otherwise re-request page one forever."""
	broken = FakeResponse(
		{
			"data": {
				"products": {
					"edges": [{"node": {"id": "1"}}],
					"pageInfo": {"hasNextPage": True, "endCursor": None},
				}
			}
		}
	)
	client, _, _ = make_client([broken])
	assert [n["id"] for n in client.paginate("query", {}, "products")] == ["1"]
