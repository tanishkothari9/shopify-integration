"""Exception taxonomy for Shopify interactions.

Shopify reports failure in three distinct places (spec §6.3) and conflating them is
the classic source of either infinite retries or silent data loss:

* transport  -- HTTP status: 429/5xx/network. Retryable.
* graphql    -- top-level ``errors[]``. ``THROTTLED`` is retryable; everything else,
                including ``MAX_COST_EXCEEDED``, is permanent.
* business   -- ``data.<mutation>.userErrors[]``. HTTP 200, no ``errors[]``, and the
                write silently did nothing. Never retryable.

Every exception here answers exactly one question: may the caller try again?
"""


class ShopifyError(Exception):
	"""Base for every Shopify failure. ``retryable`` drives the queue's backoff."""

	retryable = False


class ShopifyTransportError(ShopifyError):
	"""HTTP 5xx, connection reset, timeout. The request may never have been seen."""

	retryable = True

	def __init__(self, message: str, status_code: int | None = None):
		super().__init__(message)
		self.status_code = status_code


class ShopifyThrottled(ShopifyError):
	"""HTTP 429, or a top-level GraphQL error with code ``THROTTLED``."""

	retryable = True

	def __init__(self, message: str, retry_after: float | None = None):
		super().__init__(message)
		self.retry_after = retry_after


class ShopifyGraphQLError(ShopifyError):
	"""Top-level ``errors[]`` that is not a throttle: malformed query, MAX_COST_EXCEEDED,
	ACCESS_DENIED, SHOP_INACTIVE. Retrying an unchanged query cannot help."""

	retryable = False

	def __init__(self, message: str, errors: list | None = None):
		super().__init__(message)
		self.errors = errors or []


class ShopifyUserError(ShopifyError):
	"""``userErrors[]`` on a mutation: Shopify understood the write and refused it.

	This is the one teams miss. The HTTP status is 200 and ``errors[]`` is absent, so a
	client that only checks those reports success while nothing was written.
	"""

	retryable = False

	def __init__(self, message: str, user_errors: list | None = None):
		super().__init__(message)
		self.user_errors = user_errors or []


class ShopifyInventoryConflict(ShopifyUserError):
	"""Compare-and-set lost a race: the level moved between our read and our write.

	Retryable, unlike every other ``userErrors[]`` refusal, because retrying is the *only*
	correct response to optimistic concurrency losing. The handler re-reads current state on
	every attempt, so the next one compares against the value that is actually there.

	Without this the row failed permanently on the first conflict, with `attempts=0`. Three
	workers draining the same items turned twelve of them into dead rows needing a manual
	requeue, and the only thing that put Shopify right was the nightly reconciliation.
	"""

	retryable = True


class ShopifyConfigurationError(ShopifyError):
	"""The store is misconfigured: missing token, disabled, unknown shop domain."""

	retryable = False


class WebhookVerificationError(ShopifyError):
	"""HMAC did not verify, or the shop domain is unknown. Never process the payload."""

	retryable = False
