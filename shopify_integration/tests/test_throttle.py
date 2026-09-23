"""Adaptive throttle (spec §6.2)."""

from shopify_integration.api.throttle import (
	ASSUMED_MAXIMUM,
	AdaptiveThrottle,
	InMemoryCache,
	ThrottleState,
	actual_query_cost,
	parse_throttle_status,
)
from shopify_integration.tests.conftest import FakeClock, throttle_extensions


def make_throttle(clock=None, cache=None):
	clock = clock or FakeClock()
	cache = cache or InMemoryCache()
	return AdaptiveThrottle("test-store", cache, clock=clock, sleeper=clock.sleep), clock, cache


def test_parses_shopify_cost_telemetry():
	state = parse_throttle_status({"extensions": throttle_extensions()}, now=100.0)
	assert state.maximum_available == 1000.0
	assert state.currently_available == 900.0
	assert state.restore_rate == 50.0
	assert state.observed_at == 100.0


def test_missing_telemetry_is_none_not_an_error():
	"""Transport failures and some error responses carry no extensions block at all."""
	assert parse_throttle_status({}) is None
	assert parse_throttle_status({"extensions": {}}) is None
	assert parse_throttle_status({"data": {"shop": {}}}) is None


def test_reads_actual_query_cost():
	assert actual_query_cost({"extensions": throttle_extensions(actual_cost=17)}) == 17.0
	assert actual_query_cost({}) is None


def test_bucket_refills_over_time_and_caps_at_maximum():
	state = ThrottleState(1000.0, 500.0, 50.0, observed_at=0.0)
	assert state.projected_available(0.0) == 500.0
	assert state.projected_available(2.0) == 600.0
	assert state.projected_available(1000.0) == 1000.0  # capped, not unbounded


def test_learns_plan_size_from_a_response_without_configuration():
	"""A Plus store and a Standard store need no setting -- the response tells us."""
	throttle, _, _ = make_throttle()
	throttle.observe({"extensions": throttle_extensions(maximum=2000.0, currently_available=2000.0)})
	assert throttle.read_state().maximum_available == 2000.0
	assert throttle.headroom() == 2000.0 - (2000.0 * 0.10)


def test_assumes_the_smallest_plan_before_anything_is_known():
	"""Guessing low costs one round trip; guessing high causes a 429."""
	throttle, _, _ = make_throttle()
	assert throttle.headroom() == ASSUMED_MAXIMUM - (ASSUMED_MAXIMUM * 0.10)


def test_does_not_sleep_when_there_is_headroom():
	throttle, clock, _ = make_throttle()
	throttle.observe({"extensions": throttle_extensions(maximum=1000.0, currently_available=900.0)})
	assert throttle.acquire(10) == 0.0
	assert clock.slept == []


def test_sleeps_until_the_bucket_refills_past_the_floor():
	throttle, clock, _ = make_throttle()
	throttle.observe({"extensions": throttle_extensions(maximum=1000.0, currently_available=105.0)})
	# headroom = 105 - floor(100) = 5; a cost of 55 needs 50 more points at 50/s => ~1s.
	waited = throttle.acquire(55)
	assert waited > 0
	assert 0.5 <= clock.total_slept <= 2.0


def test_floor_protects_interactive_work_from_bulk_backfills():
	"""At exactly the floor there is no headroom left to spend, by design."""
	throttle, _, _ = make_throttle()
	throttle.observe({"extensions": throttle_extensions(maximum=1000.0, currently_available=100.0)})
	assert throttle.headroom() == 0.0


def test_reservations_make_concurrent_workers_see_less_headroom():
	"""Two workers reading the same snapshot must not both spend it."""
	cache = InMemoryCache()
	clock = FakeClock()
	worker_a = AdaptiveThrottle("s", cache, clock=clock, sleeper=clock.sleep)
	worker_b = AdaptiveThrottle("s", cache, clock=clock, sleeper=clock.sleep)

	worker_a.observe({"extensions": throttle_extensions(maximum=1000.0, currently_available=1000.0)})
	before = worker_b.headroom()
	worker_a.acquire(400)
	assert worker_b.headroom() == before - 400


def test_release_returns_the_reservation():
	throttle, _, _ = make_throttle()
	throttle.observe({"extensions": throttle_extensions(maximum=1000.0, currently_available=1000.0)})
	before = throttle.headroom()
	throttle.acquire(300)
	throttle.release(300)
	assert throttle.headroom() == before


def test_stray_release_cannot_mint_capacity():
	throttle, _, _ = make_throttle()
	throttle.observe({"extensions": throttle_extensions(maximum=1000.0, currently_available=1000.0)})
	baseline = throttle.headroom()
	throttle.release(500)
	throttle.release(500)
	assert throttle.headroom() == baseline


def test_penalise_empties_the_bucket_after_a_429():
	throttle, _, _ = make_throttle()
	throttle.observe({"extensions": throttle_extensions(maximum=1000.0, currently_available=1000.0)})
	throttle.penalise()
	assert throttle.read_state().currently_available == 0.0
	assert throttle.headroom() < 0


def test_penalise_honours_retry_after_by_backdating_the_snapshot():
	throttle, clock, _ = make_throttle()
	throttle.observe({"extensions": throttle_extensions(maximum=1000.0, currently_available=1000.0)})
	throttle.penalise(retry_after=10.0)
	# The bucket is treated as empty until now+10, so nothing has refilled yet at now.
	assert throttle.read_state().projected_available(clock.now) == 0.0


def test_a_corrupt_snapshot_does_not_wedge_the_client():
	cache = InMemoryCache()
	throttle, _, _ = make_throttle(cache=cache)
	cache.set("shopify_integration:throttle:test-store", "{not json")
	assert throttle.read_state() is None
	assert throttle.headroom() > 0  # falls back to the assumed plan rather than blocking


def test_acquire_gives_up_rather_than_blocking_a_worker_forever():
	"""The queue's backoff is the outer safety net; a worker must stay available."""
	throttle, clock, _ = make_throttle()
	throttle.observe(
		{"extensions": throttle_extensions(maximum=1000.0, currently_available=0.0, restore=1.0)}
	)
	throttle.acquire(900)
	assert clock.total_slept <= 61.0
