"""Shared fakes for the pure unit tests.

Everything here exists so the interesting logic -- throttling, the error taxonomy, HMAC
verification, money parsing -- can be tested with no database, no Redis and no network.
"""

from __future__ import annotations

import json


class FakeClock:
	"""A wall clock that only moves when something sleeps.

	Lets a test assert on how long ``acquire`` *would* have waited without actually waiting,
	and keeps the throttle's refill maths observable.
	"""

	def __init__(self, start: float = 1_000_000.0):
		self.now = start
		self.slept: list[float] = []

	def __call__(self) -> float:
		return self.now

	def sleep(self, seconds: float) -> None:
		self.slept.append(seconds)
		self.now += seconds

	@property
	def total_slept(self) -> float:
		return sum(self.slept)


class FakeResponse:
	def __init__(self, payload=None, status_code: int = 200, headers: dict | None = None, text=None):
		self.status_code = status_code
		self.headers = headers or {}
		self._payload = payload
		self.text = text if text is not None else json.dumps(payload)

	def json(self):
		if self._payload is None:
			raise ValueError("no json")
		return self._payload


class FakeSession:
	"""Returns canned responses in order; repeats the last one once exhausted."""

	def __init__(self, responses):
		self.responses = list(responses)
		self.requests: list[dict] = []

	def post(self, url, headers=None, data=None, timeout=None):
		self.requests.append(
			{"url": url, "headers": headers, "body": json.loads(data) if data else None, "timeout": timeout}
		)
		response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
		if isinstance(response, Exception):
			raise response
		return response


def throttle_extensions(currently_available=900.0, maximum=1000.0, restore=50.0, actual_cost=8):
	return {
		"cost": {
			"requestedQueryCost": 12,
			"actualQueryCost": actual_cost,
			"throttleStatus": {
				"maximumAvailable": maximum,
				"currentlyAvailable": currently_available,
				"restoreRate": restore,
			},
		}
	}
