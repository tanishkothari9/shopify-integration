"""The outbox: enqueue and drain (spec §7, core principle 1).

ERPNext changes never call Shopify. They write a queue row; workers drain it. That
indirection is what buys retry, coalescing, backpressure, ordering control and
observability -- and it is why a POS save never waits on a network call.

Two invariants make the queue self-healing rather than merely durable:

* **Absolute state, not deltas.** A row records *that* an item needs pushing, never the
  value to push. Workers re-read current state at drain time, so out-of-order or duplicated
  rows converge on the truth instead of compounding into drift.
* **Coalescing.** Enqueueing a dedupe_key that already has a Pending row is a no-op, so a
  20-line invoice produces one row per affected item rather than twenty API calls.
"""

from __future__ import annotations

import random
import re
from contextlib import contextmanager
from datetime import timedelta

import frappe
from frappe.utils import cstr, get_site_path, now_datetime
from frappe.utils.file_lock import LockTimeoutError

from shopify_integration.exceptions import PartialFailure

#: Rows claimed per drain pass. Bounded so one store's backlog cannot monopolise a worker.
DEFAULT_BATCH_SIZE = 50

#: After this many attempts a row is parked as Failed with its error preserved. Parking
#: matters as much as retrying: a poison row must never block the rows behind it.
MAX_ATTEMPTS = 5

#: A row Running longer than this is assumed orphaned by a killed worker (spec §7.2).
STALE_RUNNING_MINUTES = 15

#: Operation -> dotted path of callable(store, rows). Handlers receive the whole group so
#: they can batch into one mutation, which is the entire point of moving off REST.
#: Lower drains first. Inventory is the only thing a customer can be hurt by waiting for:
#: everything else is a listing that is briefly stale, but stale availability oversells.
#:
#: Strict `creation ASC` is what made this necessary. Editing the tax rules on an Item Group
#: queues one collection row per published product under it -- thousands on a real
#: catalogue -- and at roughly two API calls each that is hours of work. Every POS sale from
#: that moment queued behind it, so the "stock reaches Shopify in seconds" guarantee became
#: a three-hour delay. The throttle's floor reserves API *budget* for exactly this case and
#: could do nothing about claim order.
DEFAULT_PRIORITY = 1
REALTIME_PRIORITY = 0
SWEEP_PRIORITY = 2

PRIORITY_BY_OPERATION: dict[str, int] = {"inventory": REALTIME_PRIORITY}

#: How many rows of one operation, for one item, inside the window below before this stops
#: being a busy product and starts being a loop. Ten is comfortably above anything honest:
#: the dedupe key already collapses a burst of edits into a single Pending row, so ten rows
#: means ten separate drains each earning another enqueue.
BREAKER_MAX_ROWS = 10

#: The window those rows are counted over.
BREAKER_WINDOW_SECONDS = 300

#: How long that one operation stays blocked for that one item once the breaker trips.
#: Everything else about the item -- and every other item -- keeps syncing. It clears by
#: itself, because a loop whose cause has been fixed should not need anyone to come and
#: unlatch it, and the alert is what makes sure the trip was not missed.
BREAKER_COOLDOWN_SECONDS = 3600

#: Operations the breaker will not stop, however often they are queued.
#:
#: Inventory, and only inventory. A breaker is a trade: it spends correctness to contain a
#: fault. For a listing that is the right trade -- a title an hour stale harms nobody. For
#: stock it is the wrong one, because the harm the block causes is the harm the app exists
#: to prevent: Shopify keeps selling units ERPNext knows are gone.
#:
#: And a run of inventory rows is not even good evidence of a loop. Eleven sales of one SKU
#: inside five minutes is a flash sale, not a fault, and it is exactly when stopping would
#: cost the most. An inventory push is convergent besides -- it sends the level that is
#: true now, not a delta -- so a loop of them wastes API budget and corrupts nothing, while
#: the throttle already bounds what that waste can cost.
BREAKER_EXEMPT_OPERATIONS = frozenset({"inventory"})


def _breaker_keys(store: str, operation: str, subject: str) -> tuple[str, str]:
	base = f"shopify_breaker:{store}:{operation}:{subject}"
	return f"{base}:count", f"{base}:open"


def clear_breaker(store: str, operation: str, subject: str) -> None:
	"""Let this operation run for this item again, now rather than when the cooldown ends."""
	count_key, open_key = _breaker_keys(store, operation, subject)
	cache = frappe.cache()
	cache.delete_value(count_key)
	cache.delete_value(open_key)


def breaker_is_open(store: str, operation: str, subject: str) -> bool:
	_, open_key = _breaker_keys(store, operation, subject)
	return bool(_read_flag(open_key))


def _read_flag(open_key: str):
	"""Read the breaker flag past Frappe's per-request memo.

	`expires=True` is load-bearing. Without it `get_value` writes whatever it found into
	`frappe.local.cache`, including a miss -- so the very first check of a breaker that is
	closed poisons every later check in the same job with that `None`, and the flag set a
	moment afterwards is never seen. `set_value` with `expires_in_sec` writes only to Redis
	and never to that memo, which is what makes the two disagree.
	"""
	return frappe.cache().get_value(open_key, expires=True)


def _breaker_blocks(store: str, operation: str, subject: str) -> bool:
	"""Whether this operation is currently stopped for this item.

	Fails open. A cache that is down must slow nothing and block nothing -- the breaker
	exists to contain a fault, not to become one.
	"""
	if not subject or operation in BREAKER_EXEMPT_OPERATIONS:
		return False
	_, open_key = _breaker_keys(store, operation, subject)
	try:
		return bool(_read_flag(open_key))
	except Exception:
		frappe.logger("shopify_integration").warning(
			"Sync breaker could not reach the cache; allowing the enqueue.", exc_info=True
		)
		return False


def _breaker_count(store: str, operation: str, subject: str) -> None:
	"""Count a row that was actually written, and trip if there have been too many.

	Rows, not attempts. An enqueue that coalesces into an existing Pending row is the
	queue working -- one row is standing in for the burst, and counting those would trip
	the breaker on a busy product rather than a looping one.

	Counted in Redis rather than by querying the queue table: this runs inside whatever
	save asked for the sync, including a sale at the POS counter, and that path may not
	grow a `COUNT(*)` over a table the size of the queue.
	"""
	if not subject or operation in BREAKER_EXEMPT_OPERATIONS:
		return

	count_key, open_key = _breaker_keys(store, operation, subject)
	try:
		cache = frappe.cache()
		seen = cache.incrby(cache.make_key(count_key), 1)
		if seen == 1:
			cache.expire(cache.make_key(count_key), BREAKER_WINDOW_SECONDS)
		if seen <= BREAKER_MAX_ROWS:
			return
		cache.set_value(open_key, 1, expires_in_sec=BREAKER_COOLDOWN_SECONDS)
	except Exception:
		frappe.logger("shopify_integration").warning(
			"Sync breaker could not reach the cache; not counting this row.", exc_info=True
		)
		return

	message = (
		f"{subject} has queued {seen} {operation} syncs for {store} in under "
		f"{BREAKER_WINDOW_SECONDS // 60} minutes, which is a feedback loop rather than a busy "
		f"product. Further {operation} syncs for this item are blocked for "
		f"{BREAKER_COOLDOWN_SECONDS // 60} minutes; everything else about it, and every other "
		f"item, is unaffected.\n\n"
		f"Clear it early with:\n"
		f"    shopify_integration.sync.engine.clear_breaker({store!r}, {operation!r}, {subject!r})"
	)
	frappe.logger("shopify_integration").error(message)
	# An Error Log entry, because a logger line in a worker's file is not somewhere anyone
	# looks. This is the alert.
	frappe.log_error(title=f"Shopify sync loop stopped: {operation} on {subject}", message=message)


OPERATION_HANDLERS: dict[str, str] = {
	"inventory": "shopify_integration.outbound.inventory.push_inventory",
	"product": "shopify_integration.outbound.product.push_products",
	"price": "shopify_integration.outbound.price.push_prices",
	"fulfillment": "shopify_integration.outbound.fulfillment.push_fulfillments",
	"collection": "shopify_integration.outbound.collections.push_collections",
	"media": "shopify_integration.outbound.media.push_media",
}


def enqueue_sync(
	store: str,
	operation: str,
	dedupe_key: str,
	ref_doctype: str | None = None,
	ref_docname: str | None = None,
	payload: dict | None = None,
	priority: int | None = None,
) -> str | None:
	"""Insert a Pending row unless one already exists for this dedupe_key, then schedule a drain.

	Called from doc_events, so it must be cheap and must never touch the network. It runs
	inside the user's save transaction -- including at the POS counter.

	Returns the row name, or None when coalesced into an existing Pending row, or None
	when the circuit breaker has this item's operation stopped.
	"""
	subject = cstr(ref_docname)
	if _breaker_blocks(store, operation, subject):
		return None

	doc = frappe.new_doc("Shopify Sync Queue")
	doc.store = store
	doc.operation = operation
	doc.dedupe_key = dedupe_key
	doc.ref_doctype = ref_doctype
	doc.ref_docname = ref_docname
	doc.state = "Pending"
	doc.priority = (
		priority if priority is not None else PRIORITY_BY_OPERATION.get(operation, DEFAULT_PRIORITY)
	)
	doc.attempts = 0
	doc.next_attempt_at = now_datetime()
	if payload:
		doc.payload = frappe.as_json(payload)

	try:
		doc.insert(ignore_permissions=True)
	except Exception as exc:
		if _is_duplicate(exc):
			# Coalesced: a Pending row for this key already exists and will pick up the
			# current state when it drains. Losing this insert is the design, not a failure.
			frappe.clear_last_message()
			return None
		raise

	_breaker_count(store, operation, subject)
	schedule_drain(store)
	return doc.name


def schedule_drain(store: str, *, follow_on: bool = False) -> None:
	"""Ask for a drain of one store.

	``deduplicate`` keeps a burst of enqueues from spawning a job each;
	``enqueue_after_commit`` is mandatory -- without it a worker can pick the job up before
	the transaction that wrote the row has committed, and push stale data or find nothing.

	`follow_on` is for a drain asking for the next one, and it must not deduplicate.
	Frappe refuses to queue a job whose id is already QUEUED *or STARTED*, and the drain
	making the request is itself that id, started. So the request was silently dropped every
	time -- the rows written during a drain waited for the once-a-minute safety net instead
	of going straight out. Moving the call outside the filelock, which is where it used to
	be, did not help: the lock was never what was holding the id.
	"""
	frappe.enqueue(
		"shopify_integration.sync.engine.drain_store",
		queue="short",
		job_id=(
			f"shopify_drain::{store}::{frappe.generate_hash(length=8)}"
			if follow_on
			else f"shopify_drain::{store}"
		),
		deduplicate=not follow_on,
		enqueue_after_commit=True,
		store=store,
	)


def drain_store(store: str, batch_size: int = DEFAULT_BATCH_SIZE) -> dict:
	"""Claim and process a batch of Pending rows for one store.

	One job per store: a throttled shop waits on its own, never on someone else's. Stores drain
	in parallel; a single store drains one batch at a time.

	That last part is what the lock is for. `schedule_drain` deduplicates *queued* jobs, but a
	drain already running does not stop another being queued behind it -- so two workers end up
	draining the same store together, read the same inventory levels, and each invalidates the
	other's compare-and-set. Under three workers that was most of the batch spent losing races
	and backing off, for no throughput at all. Shopify meters per shop anyway, so a second
	concurrent drain of one store could not have gone faster even if it had worked.
	"""
	try:
		with quiet_filelock(_lock_name(store), timeout=1):
			result = _drain_locked(store, batch_size)
	except LockTimeoutError:
		# Another worker has this store. It will pick up whatever we would have claimed, and
		# it schedules a fresh drain itself if anything is still due when it finishes.
		return {"claimed": 0, "done": 0, "failed": 0, "requeued": 0, "skipped": "already draining"}

	# More work waiting: come back for it rather than draining unboundedly in one job.
	#
	# Scheduled *after* the lock is released, and only once this job's work is done. Asking
	# from inside the drain -- which is where it used to be -- deduplicates the new job
	# against the one currently executing, so the signal is swallowed and the row waits for
	# the safety net. That is how an inventory push sat Pending from 21:47 until 22:00:
	# correct in the end, thirteen minutes late.
	if has_pending(store):
		schedule_drain(store, follow_on=True)

	return result


@contextmanager
def quiet_filelock(lock_name: str, *, timeout: int = 1):
	"""Frappe's filelock, without an Error Log every time somebody loses the race.

	`frappe.utils.synchronization.filelock` calls `frappe.log_error` before it re-raises, so
	catching LockTimeoutError does not stop the entry being written. A second worker finding
	a store already draining is the design working, not a fault, and on a busy shop it filled
	the Error Log with hundreds of them a day -- which is how the errors that matter get
	missed.

	Same lock file and same semantics, so it interlocks with anything still using Frappe's.
	"""
	import os

	from filelock import FileLock, Timeout

	path = os.path.abspath(get_site_path("locks", lock_name + ".lock"))
	try:
		with FileLock(path, timeout=timeout):
			yield
	except Timeout as exc:
		raise LockTimeoutError(f"Failed to acquire lock: {lock_name}") from exc


def _lock_name(store: str) -> str:
	"""A filesystem-safe lock name. Store names carry spaces and shop domains carry dots."""
	return "shopify_drain_" + re.sub(r"[^A-Za-z0-9_-]+", "_", store)


def _drain_locked(store: str, batch_size: int) -> dict:
	claimed = claim_rows(store, batch_size)
	if not claimed:
		return {"claimed": 0, "done": 0, "failed": 0, "requeued": 0}

	result = {"claimed": len(claimed), "done": 0, "failed": 0, "requeued": 0}

	by_operation: dict[str, list[dict]] = {}
	for row in claimed:
		by_operation.setdefault(row["operation"], []).append(row)

	for operation, rows in by_operation.items():
		handler_path = OPERATION_HANDLERS.get(operation)
		if not handler_path:
			for row in rows:
				_fail(row["name"], f"No handler registered for operation '{operation}'")
				result["failed"] += 1
			continue

		failures: dict[str, Exception] = {}
		try:
			handler = frappe.get_attr(handler_path)
			handler(store, rows)
		except PartialFailure as partial:
			# The handler worked row by row and told us exactly which ones broke. The rest
			# were pushed and are done.
			failures = partial.failures
		except Exception as exc:
			# Batched, or broken before it got to any particular row. The whole group fails.
			failures = {row["name"]: exc for row in rows}

		for row in rows:
			exc = failures.get(row["name"])
			if exc is None:
				_succeed(row["name"])
				result["done"] += 1
			else:
				result[_handle_error(row, exc)] += 1

	frappe.db.commit()
	return result


def claim_rows(store: str, batch_size: int) -> list[dict]:
	"""Atomically move up to ``batch_size`` due rows from Pending to Running.

	``FOR UPDATE SKIP LOCKED`` is what stops two workers from claiming the same row: the
	second worker steps over rows the first has locked instead of blocking behind them.
	Supported by MariaDB 10.6+ and PostgreSQL 9.5+, which covers every platform Frappe v15
	runs on. The lock is held only across the flip to Running, so the window is negligible.
	"""
	now = now_datetime()
	rows = frappe.db.sql(
		"""
		SELECT name, operation, dedupe_key, ref_doctype, ref_docname, attempts, payload
		FROM `tabShopify Sync Queue`
		WHERE store = %(store)s
		  AND state = 'Pending'
		  AND (next_attempt_at IS NULL OR next_attempt_at <= %(now)s)
		ORDER BY priority ASC, creation ASC
		LIMIT %(limit)s
		FOR UPDATE SKIP LOCKED
		""",
		{"store": store, "now": now, "limit": batch_size},
		as_dict=True,
	)
	if not rows:
		frappe.db.commit()
		return []

	names = [row["name"] for row in rows]
	frappe.db.sql(
		"""
		UPDATE `tabShopify Sync Queue`
		SET state = 'Running', pending_dedupe_key = NULL, modified = %(now)s
		WHERE name IN %(names)s
		""",
		{"names": tuple(names), "now": now},
	)
	frappe.db.commit()
	return rows


def has_pending(store: str) -> bool:
	"""Whether a drain would find anything, asked the same way the claim asks it.

	`IS NULL OR <= now`, matching claim_rows. A plain `<=` never matches NULL, so a row whose
	next_attempt_at was somehow unset would be claimable but invisible here -- and a drain
	scheduled on this answer would never be scheduled at all.
	"""
	return bool(
		frappe.db.sql(
			"""SELECT 1 FROM `tabShopify Sync Queue`
			WHERE store = %(store)s AND state = 'Pending'
			  AND (next_attempt_at IS NULL OR next_attempt_at <= %(now)s)
			LIMIT 1""",
			{"store": store, "now": now_datetime()},
		)
	)


def recover_stale_running(minutes: int = STALE_RUNNING_MINUTES) -> int:
	"""Return rows orphaned by a killed worker to Pending (spec §7.2).

	A worker that dies mid-flight leaves its claim behind; without this janitor those rows
	are stuck Running forever and the work is silently lost.
	"""
	cutoff = now_datetime() - timedelta(minutes=minutes)
	stale = frappe.get_all(
		"Shopify Sync Queue",
		filters={"state": "Running", "modified": ["<", cutoff]},
		pluck="name",
	)
	recovered = []
	for name in stale:
		doc = frappe.get_doc("Shopify Sync Queue", name)
		if _key_taken_by_another({"name": name, "dedupe_key": doc.dedupe_key}):
			# Newer work for the same key is already queued. Saving this one would restore its
			# `pending_dedupe_key` and collide with the unique index -- and an exception here
			# aborts the whole janitor pass before its commit, so *nothing* is recovered and
			# the same collision repeats on every tick.
			_supersede(name, "Superseded by a newer queued row")
			continue

		doc.state = "Pending"
		doc.next_attempt_at = now_datetime()
		doc.last_error = f"Recovered from stale Running state after {minutes} minutes"
		doc.save(ignore_permissions=True)
		recovered.append(name)

	if stale:
		frappe.db.commit()
	return len(recovered)


def drain_all_stores() -> None:
	"""Scheduled safety net: nudge every enabled store that has work waiting.

	Belt and braces for the event-driven path -- if an ``enqueue_after_commit`` job was lost
	to a worker restart, the row still drains on the next tick instead of sitting forever.
	"""
	for store in frappe.get_all("Shopify Store", filters={"enabled": 1}, pluck="name"):
		if has_pending(store):
			schedule_drain(store)


def _succeed(name: str) -> None:
	frappe.db.set_value(
		"Shopify Sync Queue",
		name,
		{"state": "Done", "last_error": None, "pending_dedupe_key": None},
		update_modified=True,
	)


def _fail(name: str, message: str) -> None:
	frappe.db.set_value(
		"Shopify Sync Queue",
		name,
		{"state": "Failed", "last_error": message, "pending_dedupe_key": None},
		update_modified=True,
	)


def _supersede(name: str, reason: str) -> None:
	"""Retire a row whose work a newer row already covers.

	Done rather than Failed, deliberately. Nothing went wrong: the queue coalesces by design,
	and handlers read current state at drain time, so the newer row pushes everything this one
	would have. Marking it Failed puts rows on the dashboard that an operator will investigate
	and find nothing wrong with -- and buries the failures that do matter.
	"""
	frappe.db.set_value(
		"Shopify Sync Queue",
		name,
		{"state": "Done", "last_error": reason, "pending_dedupe_key": None},
		update_modified=True,
	)


#: Frappe's own "someone else got there first" errors. They carry no `retryable` attribute, so
#: without this they are treated as permanent -- and two writers touching one document is a
#: normal thing here, not a defect. A products/update webhook arriving while a bulk import or a
#: reconciliation is rewriting the same Item is exactly the case, and failing it permanently
#: strands work that would have succeeded on the next attempt.
CONTENTION_ERRORS = (
	frappe.TimestampMismatchError,
	frappe.QueryDeadlockError,
	frappe.QueryTimeoutError,
)


def _is_contention(exc: Exception) -> bool:
	return isinstance(exc, CONTENTION_ERRORS)


def _handle_error(row: dict, exc: Exception) -> str:
	"""Route a failure by whether it can ever succeed. Returns the result key to count.

	This is where the error taxonomy earns its keep: a userError retried five times is five
	guaranteed failures and a five-times-longer wait before a human sees the real problem.
	"""
	retryable = getattr(exc, "retryable", False) or _is_contention(exc)
	message = f"{type(exc).__name__}: {exc}"

	if not retryable:
		_fail(row["name"], message)
		frappe.log_error(title=f"Shopify sync failed permanently: {row['dedupe_key']}", message=message)
		return "failed"

	attempts = (row.get("attempts") or 0) + 1
	if attempts >= MAX_ATTEMPTS:
		_fail(row["name"], f"Giving up after {attempts} attempts. {message}")
		return "failed"

	if _key_taken_by_another(row):
		# While this row was Running, its work was enqueued again -- claiming a row frees its
		# `pending_dedupe_key` precisely so that can happen. Writing the key back now would
		# collide with the unique index, and that error is raised from inside the drain's own
		# error handling, where nothing catches it: the whole batch's successes roll back
		# uncommitted and every claimed row is left Running until the janitor comes round.
		#
		# The newer row already covers this work, and handlers read current state at drain
		# time rather than a stored payload, so retiring this one loses nothing.
		_supersede(row["name"], f"Superseded by a newer queued row. {message}")
		return "superseded"

	frappe.db.set_value(
		"Shopify Sync Queue",
		row["name"],
		{
			"state": "Pending",
			"pending_dedupe_key": row["dedupe_key"],
			"attempts": attempts,
			"next_attempt_at": now_datetime() + timedelta(seconds=backoff_seconds(attempts)),
			"last_error": message,
		},
		update_modified=True,
	)
	return "requeued"


def _key_taken_by_another(row: dict) -> bool:
	"""Whether some other row already holds this dedupe key in the Pending slot."""
	holder = frappe.db.get_value("Shopify Sync Queue", {"pending_dedupe_key": row["dedupe_key"]}, "name")
	return bool(holder) and holder != row["name"]


def backoff_seconds(attempt: int) -> float:
	"""Exponential backoff with full jitter, capped at five minutes.

	Jitter matters here for the same reason it does in the client: rows throttled together
	would otherwise all come due at the same instant and re-throttle the shop as a group.
	"""
	ceiling = min(300.0, 2.0**attempt * 5.0)
	return random.uniform(ceiling / 2, ceiling)


def _is_duplicate(exc: Exception) -> bool:
	"""True when an insert failed on a unique index rather than anything else."""
	if isinstance(exc, frappe.UniqueValidationError | frappe.DuplicateEntryError):
		return True
	try:
		return bool(frappe.db.is_unique_key_violation(exc))
	except Exception:
		return "Duplicate entry" in str(exc) or "duplicate key" in str(exc).lower()
