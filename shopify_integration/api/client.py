"""GraphQL Admin API client (spec §6).

GraphQL only -- the REST Admin API went legacy on 2024-10-01 and is not used anywhere in
this app. One client instance per store, each with its own throttle, so a shop that is
being rate-limited cannot stall a shop that is idle.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import requests

from shopify_integration.api.throttle import AdaptiveThrottle, actual_query_cost
from shopify_integration.exceptions import (
	ShopifyConfigurationError,
	ShopifyGraphQLError,
	ShopifyThrottled,
	ShopifyTransportError,
	ShopifyUserError,
)

QUERY_DIR = Path(__file__).parent / "queries"

#: Top-level GraphQL error codes that are permanent. Retrying an unchanged document with
#: an unchanged token cannot turn any of these into a success.
#:
#: MAX_COST_EXCEEDED is the subtle one and is missing from the spec's §6.3 table: it
#: arrives as HTTP 200 with errors[], looks superficially like THROTTLED, but means the
#: single query is larger than the entire bucket. Backing off and retrying loops forever.
PERMANENT_GRAPHQL_CODES = frozenset(
	{"MAX_COST_EXCEEDED", "ACCESS_DENIED", "SHOP_INACTIVE", "UNAUTHORIZED", "FORBIDDEN"}
)

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_ATTEMPTS = 3
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


@cache
def load_query(name: str) -> str:
	"""Read a ``.graphql`` document from ``api/queries``.

	Documents are files, not inline strings, because they are versioned artifacts: when a
	store's pinned API version moves, the diff on these files *is* the review.
	"""
	path = QUERY_DIR / f"{name}.graphql"
	if not path.exists():
		raise FileNotFoundError(f"GraphQL document not found: {path}")
	return path.read_text()


@dataclass(frozen=True)
class StoreCredentials:
	"""Everything the client needs, decoupled from the Frappe document.

	Keeping this separate is what lets the whole client be unit-tested with no database:
	tests construct credentials directly, production reads them from ``Shopify Store``.
	"""

	store: str
	shop_domain: str
	access_token: str
	api_version: str

	@property
	def endpoint(self) -> str:
		return f"https://{self.shop_domain}/admin/api/{self.api_version}/graphql.json"

	def __repr__(self) -> str:
		# Never let the token reach a log line or a traceback frame (spec §15).
		return (
			f"StoreCredentials(store={self.store!r}, shop_domain={self.shop_domain!r}, "
			f"api_version={self.api_version!r}, access_token='***')"
		)


def credentials_for_store(store: str) -> StoreCredentials:
	"""Load credentials from the ``Shopify Store`` document. Requires a Frappe context."""
	import frappe

	doc = frappe.get_cached_doc("Shopify Store", store)
	if not doc.enabled:
		raise ShopifyConfigurationError(f"Shopify Store '{store}' is disabled")

	token = doc.get_password("admin_api_token", raise_exception=False)
	if not token:
		raise ShopifyConfigurationError(
			f"Shopify Store '{store}' has no Admin API token. Install the app on the shop first "
			"-- open the store and use Connect to Shopify -- which completes OAuth and stores it."
		)

	return StoreCredentials(
		store=store,
		shop_domain=doc.shop_domain,
		access_token=token,
		api_version=doc.api_version,
	)


class ShopifyClient:
	"""Executes GraphQL documents against one store, with adaptive throttling and retries.

	The retry budget here is deliberately small (3 attempts). It exists to absorb a blip,
	not to be the app's durability story -- that is the sync queue's job, which can wait
	minutes and survive a worker restart. A client that retried for minutes would hold a
	worker hostage and hide failures from the queue's observability.
	"""

	def __init__(
		self,
		credentials: StoreCredentials,
		*,
		throttle: AdaptiveThrottle | None = None,
		session: requests.Session | None = None,
		timeout: int = DEFAULT_TIMEOUT,
		max_attempts: int = DEFAULT_MAX_ATTEMPTS,
		sleeper=time.sleep,
	) -> None:
		self.credentials = credentials
		self.throttle = throttle if throttle is not None else AdaptiveThrottle(credentials.store)
		self.session = session if session is not None else requests.Session()
		self.timeout = timeout
		self.max_attempts = max_attempts
		self._sleep = sleeper

	@classmethod
	def for_store(cls, store: str, **kwargs) -> ShopifyClient:
		return cls(credentials_for_store(store), **kwargs)

	@property
	def headers(self) -> dict[str, str]:
		return {
			"X-Shopify-Access-Token": self.credentials.access_token,
			"Content-Type": "application/json",
			"Accept": "application/json",
		}

	def execute(self, query: str, variables: dict | None = None, *, cost_hint: float = 10) -> dict:
		"""Run a document and return its ``data``. Blocks on the throttle if needed.

		Raises ShopifyThrottled / ShopifyTransportError (retryable) or
		ShopifyGraphQLError / ShopifyUserError (permanent).
		"""
		last_error: Exception | None = None

		for attempt in range(1, self.max_attempts + 1):
			self.throttle.acquire(cost_hint)
			try:
				payload = self._post(query, variables)
			except (ShopifyThrottled, ShopifyTransportError) as exc:
				last_error = exc
				if attempt == self.max_attempts:
					raise
				self._sleep(self._backoff(attempt, getattr(exc, "retry_after", None)))
				continue
			finally:
				self.throttle.release(cost_hint)

			# Teach the throttle before interpreting errors: a THROTTLED response still
			# carries the telemetry that tells us how long to wait.
			self.throttle.observe(payload)

			try:
				return self._interpret(payload)
			except ShopifyThrottled as exc:
				last_error = exc
				if attempt == self.max_attempts:
					raise
				self._sleep(self._backoff(attempt, exc.retry_after))

		raise last_error or ShopifyTransportError("Request failed with no recorded error")

	def _post(self, query: str, variables: dict | None) -> dict:
		"""One HTTP round trip. Translates transport-layer failure into our taxonomy."""
		body = {"query": query, "variables": variables or {}}
		try:
			response = self.session.post(
				self.credentials.endpoint,
				headers=self.headers,
				data=json.dumps(body),
				timeout=self.timeout,
			)
		except requests.RequestException as exc:
			raise ShopifyTransportError(f"Request to Shopify failed: {exc}")

		if response.status_code == 429:
			self.throttle.penalise(_retry_after(response))
			raise ShopifyThrottled("Shopify returned HTTP 429", retry_after=_retry_after(response))

		if response.status_code in RETRYABLE_STATUS:
			raise ShopifyTransportError(
				f"Shopify returned HTTP {response.status_code}", status_code=response.status_code
			)

		if response.status_code >= 400:
			# 4xx other than 429: our request is wrong. Retrying it unchanged is pointless.
			raise ShopifyGraphQLError(f"Shopify returned HTTP {response.status_code}: {response.text[:500]}")

		try:
			return response.json()
		except ValueError:
			raise ShopifyTransportError(f"Shopify returned non-JSON body: {response.text[:500]}")

	def _interpret(self, payload: dict) -> dict:
		"""Apply the three-layer error taxonomy to a 200 response (spec §6.3)."""
		errors = payload.get("errors")
		if errors:
			codes = {
				str((err.get("extensions") or {}).get("code", "")).upper()
				for err in errors
				if isinstance(err, dict)
			}
			messages = "; ".join(str(err.get("message", err)) for err in errors)

			if codes & PERMANENT_GRAPHQL_CODES:
				raise ShopifyGraphQLError(messages, errors=errors)
			if "THROTTLED" in codes:
				raise ShopifyThrottled(messages)
			raise ShopifyGraphQLError(messages, errors=errors)

		data = payload.get("data")
		if data is None:
			raise ShopifyGraphQLError("Shopify response contained neither data nor errors")

		_raise_for_user_errors(data)
		return data

	def _backoff(self, attempt: int, retry_after: float | None = None) -> float:
		"""Exponential backoff with full jitter. Honours Retry-After when Shopify sends one.

		Jitter is not decoration: without it, every worker that was throttled at the same
		instant retries at the same instant and re-throttles the shop as a group.
		"""
		if retry_after:
			return float(retry_after)
		return random.uniform(0, min(8.0, 2**attempt))

	def paginate(self, query: str, variables: dict, connection_path: str, *, cost_hint: float = 20):
		"""Yield nodes across cursor pages, following ``pageInfo.hasNextPage``.

		``connection_path`` is dotted, relative to ``data`` -- e.g. ``"products"`` or
		``"shop.metafields"``.
		"""
		cursor = None
		while True:
			page_vars = dict(variables)
			page_vars["cursor"] = cursor
			data = self.execute(query, page_vars, cost_hint=cost_hint)

			connection = _dig(data, connection_path)
			if connection is None:
				raise ShopifyGraphQLError(f"No connection found at path '{connection_path}'")

			for edge in connection.get("edges") or []:
				node = edge.get("node")
				if node is not None:
					yield node

			page_info = connection.get("pageInfo") or {}
			if not page_info.get("hasNextPage"):
				return
			cursor = page_info.get("endCursor")
			if not cursor:
				# hasNextPage without a cursor would loop forever on the same page.
				return

	def last_actual_cost(self, payload: dict) -> float | None:
		return actual_query_cost(payload)


def _raise_for_user_errors(data: dict) -> None:
	"""Scan a mutation response for a non-empty ``userErrors``.

	Checked on *every* response, not only where we remember to, because the failure mode is
	silent: HTTP 200, no errors[], and the write did nothing at all.
	"""
	for field, payload in (data or {}).items():
		if not isinstance(payload, dict):
			continue
		user_errors = payload.get("userErrors")
		if user_errors:
			message = "; ".join(
				f"{'.'.join(str(f) for f in (err.get('field') or []))}: {err.get('message')}".lstrip(": ")
				for err in user_errors
				if isinstance(err, dict)
			)
			raise ShopifyUserError(f"{field} rejected the write: {message}", user_errors=user_errors)


def _dig(data: dict, path: str):
	node = data
	for part in path.split("."):
		if not isinstance(node, dict):
			return None
		node = node.get(part)
		if node is None:
			return None
	return node


def _retry_after(response) -> float | None:
	try:
		value = response.headers.get("Retry-After")
		return float(value) if value else None
	except (ValueError, TypeError, AttributeError):
		return None
