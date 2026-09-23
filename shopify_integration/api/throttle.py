"""Adaptive token-bucket throttle driven by Shopify's own cost telemetry (spec §6.2).

Shopify returns the live state of the leaky bucket on *every* GraphQL response::

    {"extensions": {"cost": {
        "requestedQueryCost": 12,
        "actualQueryCost": 8,
        "throttleStatus": {
            "maximumAvailable": 1000.0,
            "currentlyAvailable": 940.0,
            "restoreRate": 50.0}}}}

So we never have to know or configure the shop's plan: the first response teaches us
the bucket size, and every subsequent one corrects our projection. That is what makes
the app safe to install on a store whose plan we cannot see.

Concurrency
-----------
State lives in Redis, shared across workers. Two workers reading the same snapshot would
otherwise both conclude there is headroom and both spend it, so a reservation is added to
an ``in_flight`` counter *before* the call and released after. Redis ``INCRBYFLOAT`` is
atomic, which makes the reservation safe without a lock. ``in_flight`` carries a short TTL
so a worker killed mid-flight cannot strand capacity forever.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Protocol

#: Never spend the bucket below this fraction of its maximum. Bulk backfills otherwise
#: starve interactive work — a POS inventory push should not queue behind a 50k import.
DEFAULT_FLOOR_RATIO = 0.10

#: Longest single sleep. Longer waits loop instead, so a worker stays interruptible.
MAX_SLEEP_SECONDS = 5.0

#: Total time acquire() will wait before giving up and letting the queue back off.
MAX_TOTAL_WAIT_SECONDS = 60.0

#: If a worker dies between reserving and releasing, the reservation expires with the key.
IN_FLIGHT_TTL_SECONDS = 60

#: Assumed bucket before the first response teaches us the real one. Deliberately the
#: *smallest* plan: guessing low costs one throttle round-trip, guessing high causes a 429.
ASSUMED_MAXIMUM = 100.0
ASSUMED_RESTORE_RATE = 50.0


class CacheBackend(Protocol):
	"""The slice of Redis this module needs. Narrow on purpose so tests can fake it."""

	def get(self, key: str) -> str | None: ...

	def set(self, key: str, value: str) -> None: ...

	def incrbyfloat(self, key: str, amount: float) -> float: ...

	def expire(self, key: str, seconds: int) -> None: ...


class InMemoryCache:
	"""Process-local backend for unit tests. Not safe across workers; never use in a bench."""

	def __init__(self) -> None:
		self._data: dict[str, str] = {}

	def get(self, key: str) -> str | None:
		return self._data.get(key)

	def set(self, key: str, value: str) -> None:
		self._data[key] = value

	def incrbyfloat(self, key: str, amount: float) -> float:
		current = float(self._data.get(key, 0.0))
		updated = current + amount
		self._data[key] = str(updated)
		return updated

	def expire(self, key: str, seconds: int) -> None:
		return None


class FrappeCache:
	"""Backs onto the bench's Redis. Keys are namespaced by hand because we use the raw
	redis verbs (``incrbyfloat``) rather than Frappe's get_value/set_value helpers."""

	def __init__(self) -> None:
		import frappe

		self._redis = frappe.cache()

	def get(self, key: str) -> str | None:
		value = self._redis.get(key)
		if value is None:
			return None
		return value.decode() if isinstance(value, bytes) else str(value)

	def set(self, key: str, value: str) -> None:
		self._redis.set(key, value)

	def incrbyfloat(self, key: str, amount: float) -> float:
		return float(self._redis.incrbyfloat(key, amount))

	def expire(self, key: str, seconds: int) -> None:
		self._redis.expire(key, seconds)


@dataclass(frozen=True)
class ThrottleState:
	"""A snapshot of the leaky bucket, as last reported by Shopify."""

	maximum_available: float
	currently_available: float
	restore_rate: float
	observed_at: float

	def projected_available(self, now: float) -> float:
		"""Refill the bucket forward from the snapshot to ``now``, capped at maximum.

		Wall-clock time is deliberate: the snapshot is shared between worker processes,
		so a monotonic clock would not be comparable across them.
		"""
		elapsed = max(0.0, now - self.observed_at)
		return min(self.maximum_available, self.currently_available + self.restore_rate * elapsed)

	def to_json(self) -> str:
		return json.dumps(
			{
				"maximum_available": self.maximum_available,
				"currently_available": self.currently_available,
				"restore_rate": self.restore_rate,
				"observed_at": self.observed_at,
			}
		)

	@classmethod
	def from_json(cls, raw: str) -> ThrottleState | None:
		try:
			data = json.loads(raw)
			return cls(
				maximum_available=float(data["maximum_available"]),
				currently_available=float(data["currently_available"]),
				restore_rate=float(data["restore_rate"]),
				observed_at=float(data["observed_at"]),
			)
		except (ValueError, TypeError, KeyError):
			# A malformed snapshot must not wedge the client: fall back to assuming
			# nothing is known, and let the next response re-teach us.
			return None


def parse_throttle_status(response: dict, now: float | None = None) -> ThrottleState | None:
	"""Pull a ThrottleState out of a GraphQL response body, or None if absent.

	Absence is normal, not an error: transport failures and some error responses carry no
	``extensions`` block at all.
	"""
	status = (response.get("extensions") or {}).get("cost", {}).get("throttleStatus")
	if not status:
		return None
	try:
		return ThrottleState(
			maximum_available=float(status["maximumAvailable"]),
			currently_available=float(status["currentlyAvailable"]),
			restore_rate=float(status["restoreRate"]),
			observed_at=now if now is not None else time.time(),
		)
	except (ValueError, TypeError, KeyError):
		return None


def actual_query_cost(response: dict) -> float | None:
	"""``actualQueryCost`` from a response, used to sharpen future cost estimates."""
	cost = (response.get("extensions") or {}).get("cost", {})
	value = cost.get("actualQueryCost")
	if value is None:
		return None
	try:
		return float(value)
	except (ValueError, TypeError):
		return None


class AdaptiveThrottle:
	"""Per-store gate in front of the GraphQL endpoint.

	Usage is strictly paired -- every ``acquire`` must be followed by exactly one
	``release``, which is what the client's ``finally`` block guarantees::

	    throttle.acquire(cost_hint)
	    try:
	        response = do_request()
	        throttle.observe(response)
	    finally:
	        throttle.release(cost_hint)
	"""

	def __init__(
		self,
		store: str,
		backend: CacheBackend | None = None,
		*,
		floor_ratio: float = DEFAULT_FLOOR_RATIO,
		clock=time.time,
		sleeper=time.sleep,
	) -> None:
		self.store = store
		self.backend = backend if backend is not None else FrappeCache()
		self.floor_ratio = floor_ratio
		self._clock = clock
		self._sleep = sleeper

	@property
	def _state_key(self) -> str:
		return f"shopify_integration:throttle:{self.store}"

	@property
	def _in_flight_key(self) -> str:
		return f"shopify_integration:in_flight:{self.store}"

	def read_state(self) -> ThrottleState | None:
		raw = self.backend.get(self._state_key)
		return ThrottleState.from_json(raw) if raw else None

	def _read_in_flight(self) -> float:
		raw = self.backend.get(self._in_flight_key)
		try:
			return max(0.0, float(raw)) if raw else 0.0
		except (ValueError, TypeError):
			return 0.0

	def headroom(self) -> float:
		"""Points spendable right now: projected refill, minus what peers have reserved,
		minus the reserved floor. May be negative, which means "wait"."""
		state = self.read_state()
		if state is None:
			# Nothing learned yet. Assume the smallest plan rather than optimistically
			# firing into an unknown bucket.
			state = ThrottleState(
				maximum_available=ASSUMED_MAXIMUM,
				currently_available=ASSUMED_MAXIMUM,
				restore_rate=ASSUMED_RESTORE_RATE,
				observed_at=self._clock(),
			)
		floor = state.maximum_available * self.floor_ratio
		return state.projected_available(self._clock()) - self._read_in_flight() - floor

	def acquire(self, cost: float) -> float:
		"""Block until ``cost`` points are spendable, then reserve them.

		Returns the seconds spent waiting, which the client logs. Returns even when the
		wait cap is hit -- the queue's own backoff is the outer safety net, and blocking a
		worker indefinitely would be worse than one rejected call.
		"""
		waited = 0.0
		while waited < MAX_TOTAL_WAIT_SECONDS:
			deficit = cost - self.headroom()
			if deficit <= 0:
				break

			state = self.read_state()
			restore_rate = state.restore_rate if state and state.restore_rate > 0 else ASSUMED_RESTORE_RATE
			nap = min(MAX_SLEEP_SECONDS, max(0.05, deficit / restore_rate))
			nap = min(nap, MAX_TOTAL_WAIT_SECONDS - waited)
			self._sleep(nap)
			waited += nap

		# Reserve regardless of whether the wait cap was hit, so release() stays balanced.
		self.backend.incrbyfloat(self._in_flight_key, cost)
		self.backend.expire(self._in_flight_key, IN_FLIGHT_TTL_SECONDS)
		return waited

	def release(self, cost: float) -> None:
		"""Drop a reservation. Clamped at zero so a stray release cannot mint capacity."""
		remaining = self.backend.incrbyfloat(self._in_flight_key, -cost)
		if remaining < 0:
			self.backend.set(self._in_flight_key, "0")

	def observe(self, response: dict) -> ThrottleState | None:
		"""Replace our projection with Shopify's ground truth from a live response."""
		state = parse_throttle_status(response, now=self._clock())
		if state is not None:
			self.backend.set(self._state_key, state.to_json())
		return state

	def penalise(self, retry_after: float | None = None) -> None:
		"""Record a 429 that arrived with no usable telemetry.

		Empties our view of the bucket so the next ``acquire`` waits a full refill rather
		than hammering a shop that has already told us to stop.
		"""
		state = self.read_state()
		now = self._clock()
		maximum = state.maximum_available if state else ASSUMED_MAXIMUM
		restore_rate = state.restore_rate if state and state.restore_rate > 0 else ASSUMED_RESTORE_RATE
		# Treat Retry-After as "the bucket is empty until then" by backdating the snapshot.
		observed_at = now + retry_after if retry_after else now
		self.backend.set(
			self._state_key,
			ThrottleState(
				maximum_available=maximum,
				currently_available=0.0,
				restore_rate=restore_rate,
				observed_at=observed_at,
			).to_json(),
		)
