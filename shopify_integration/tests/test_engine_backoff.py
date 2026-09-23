"""Queue backoff (spec §7.2)."""

from shopify_integration.sync.engine import MAX_ATTEMPTS, backoff_seconds


def test_backoff_grows_with_attempts():
	early = [backoff_seconds(1) for _ in range(50)]
	late = [backoff_seconds(4) for _ in range(50)]
	assert sum(late) / len(late) > sum(early) / len(early)


def test_backoff_is_jittered_not_a_fixed_ladder():
	"""Without jitter, every row throttled at the same instant comes due at the same
	instant and re-throttles the shop as a group."""
	assert len({backoff_seconds(3) for _ in range(50)}) > 1


def test_backoff_is_capped_so_a_row_is_not_parked_for_hours():
	assert all(backoff_seconds(attempt) <= 300.0 for attempt in range(1, 20))


def test_backoff_is_always_positive():
	assert all(backoff_seconds(attempt) > 0 for attempt in range(1, 20))


def test_total_retry_window_is_bounded():
	worst_case = sum(backoff_seconds(a) for a in range(1, MAX_ATTEMPTS))
	assert worst_case <= 300.0 * MAX_ATTEMPTS
