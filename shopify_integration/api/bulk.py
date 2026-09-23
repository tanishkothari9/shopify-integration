"""Bulk operation state machine (spec §11).

A 87,000-item catalogue cannot be imported through paginated queries, so Shopify runs the
query asynchronously and hands back a JSONL file. Three properties of that file shape this
module:

* **It is large.** It is streamed to disk in chunks and then read line by line. It is never
  parsed into memory as a whole, at any point.
* **It is flat.** With ``groupObjects: false`` a product is one line and each of its variants
  is a separate line carrying ``__parentId``. Children always follow their parent, so a
  single-pass buffer reassembles them without random access.
* **It is ordered and stable for seven days.** That is what makes the import resumable: the
  number of lines already applied is recorded, and a resumed run skips exactly that many.

Shopify permits one running QUERY and one running MUTATION per shop concurrently -- not one
operation in total, as the spec has it. The guard is therefore per type.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from urllib.parse import urlparse

import frappe
import requests
from frappe import _
from frappe.utils import cint, cstr

from shopify_integration.api.client import ShopifyClient, load_query
from shopify_integration.shopify_integration.doctype.shopify_bulk_operation.shopify_bulk_operation import (
	SHOPIFY_STATUS_MAP,
	active_operation,
)

#: Bytes per chunk when streaming the result file to disk.
DOWNLOAD_CHUNK_BYTES = 1024 * 1024

#: A ceiling for the bulk result file. Shopify's own limit is far below this; the cap exists so
#: a wrong or tampered URL cannot quietly fill the disk.
MAX_RESULT_BYTES = 5 * 1024 * 1024 * 1024

#: Product groups applied between database commits. Small enough that a crash loses little,
#: large enough that we are not paying a commit per product across 50,000 of them.
COMMIT_EVERY = 200

#: Seconds between polls while an operation is running.
POLL_INTERVAL_SECONDS = 30


def start_import(store: str, purpose: str = "product_import") -> str:
	"""Kick off a catalogue import. Returns the Shopify Bulk Operation name.

	If one is already in flight for this store, its name is returned instead of starting a
	second: Shopify would reject the second anyway, and queueing behind the first is what the
	user meant.
	"""
	existing = active_operation(store, "query")
	if existing:
		frappe.msgprint(
			frappe._("A bulk query is already running for {0}. Watching that one instead.").format(store)
		)
		return existing

	client = ShopifyClient.for_store(store)
	data = client.execute(
		load_query("bulk_operation_run_query"),
		{"groupObjects": False, "query": load_query("bulk_products")},
		cost_hint=10,
	)
	operation = (data.get("bulkOperationRunQuery") or {}).get("bulkOperation") or {}

	doc = frappe.new_doc("Shopify Bulk Operation")
	doc.store = store
	doc.purpose = purpose
	doc.type = "query"
	doc.status = SHOPIFY_STATUS_MAP.get(operation.get("status", "CREATED"), "Created")
	doc.operation_gid = operation.get("id")
	doc.insert(ignore_permissions=True)
	frappe.db.commit()

	schedule_poll(doc.name)
	return doc.name


def schedule_poll(operation_name: str) -> None:
	frappe.enqueue(
		"shopify_integration.api.bulk.poll",
		queue="long",
		job_id=f"shopify_bulk_poll::{operation_name}",
		deduplicate=True,
		enqueue_after_commit=True,
		operation_name=operation_name,
	)


def poll(operation_name: str) -> str:
	"""Ask Shopify how the operation is going, and act on the answer.

	Polling rather than relying solely on the ``bulk_operations/finish`` webhook: the webhook
	can be dropped, and an import that silently never finishes is the worst outcome here.
	"""
	doc = frappe.get_doc("Shopify Bulk Operation", operation_name)
	client = ShopifyClient.for_store(doc.store)
	data = client.execute(load_query("current_bulk_operation"), {"type": doc.type.upper()}, cost_hint=5)
	current = data.get("currentBulkOperation") or {}

	# currentBulkOperation returns the shop's latest operation of this type, which may not be
	# ours if another one has since started. Only trust it when the ids match.
	if current.get("id") and doc.operation_gid and current["id"] != doc.operation_gid:
		doc.db_set({"status": "Failed", "last_error": "Superseded by a newer bulk operation"})
		return "Failed"

	status = SHOPIFY_STATUS_MAP.get(current.get("status", ""), doc.status)
	doc.db_set(
		{
			"status": status,
			"object_count": cint(current.get("objectCount")),
			"root_object_count": cint(current.get("rootObjectCount")),
			"error_code": current.get("errorCode"),
			"result_url": current.get("url") or current.get("partialDataUrl"),
		}
	)
	frappe.db.commit()
	publish_progress(doc)

	if status in ("Created", "Running"):
		schedule_poll(operation_name)
		return status

	if status == "Completed" or (status == "Failed" and current.get("partialDataUrl")):
		# A failed operation with partial data is still worth applying: importing 40,000 of
		# 50,000 products beats importing none and starting over.
		frappe.enqueue(
			"shopify_integration.api.bulk.process",
			queue="long",
			job_id=f"shopify_bulk_process::{operation_name}",
			deduplicate=True,
			enqueue_after_commit=True,
			operation_name=operation_name,
		)
	return status


def cancel(operation_name: str) -> str:
	doc = frappe.get_doc("Shopify Bulk Operation", operation_name)
	client = ShopifyClient.for_store(doc.store)
	client.execute(load_query("bulk_operation_cancel"), {"id": doc.operation_gid}, cost_hint=5)
	doc.db_set("status", "Cancelled")
	frappe.db.commit()
	return "Cancelled"


def _assert_shopify_url(url: str | None) -> None:
	"""Refuse to download from anywhere that is not Shopify.

	`result_url` normally comes straight from the GraphQL response, but it is stored on a
	doctype a System Manager can edit, and `requests` follows redirects by default. Both are
	ways for this to fetch an attacker-chosen host onto the server.
	"""
	parsed = urlparse(cstr(url))
	host = (parsed.hostname or "").lower()
	if parsed.scheme != "https" or not (
		host == "shopify.com" or host.endswith(".shopify.com") or host.endswith(".myshopify.com")
	):
		frappe.throw(_("Refusing to download a bulk result from {0}.").format(host or url))


def download_result(operation_name: str) -> str:
	"""Stream the JSONL to a private file and return its path.

	Streamed in chunks and never held in memory: these files reach hundreds of megabytes, and
	``response.text`` on one would take the worker down.
	"""
	doc = frappe.get_doc("Shopify Bulk Operation", operation_name)
	if doc.result_file and os.path.exists(doc.result_file):
		return doc.result_file
	if not doc.result_url:
		frappe.throw(frappe._("Bulk operation {0} has no result URL.").format(operation_name))

	target_dir = frappe.get_site_path("private", "files", "shopify_bulk")
	os.makedirs(target_dir, exist_ok=True)
	path = os.path.join(target_dir, f"{operation_name}.jsonl")

	# `result_url` arrives from Shopify's own response, but it is also a plain field on a
	# doctype, so it is checked rather than trusted: a redirect or an edited row must not turn
	# this into "download whatever you like onto the server's disk".
	_assert_shopify_url(doc.result_url)

	written = 0
	with requests.get(doc.result_url, stream=True, timeout=120, allow_redirects=False) as response:
		response.raise_for_status()
		with open(path, "wb") as handle:
			for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_BYTES):
				if not chunk:
					continue
				written += len(chunk)
				if written > MAX_RESULT_BYTES:
					handle.close()
					os.remove(path)
					frappe.throw(
						_(
							"Bulk result for {0} exceeded {1} GB and was abandoned. "
							"Streaming keeps it out of memory, but nothing was stopping it "
							"filling the disk."
						).format(operation_name, MAX_RESULT_BYTES // (1024**3))
					)
				handle.write(chunk)

	doc.db_set("result_file", path)
	frappe.db.commit()
	return path


def iter_lines(path: str, skip: int = 0) -> Iterator[tuple[int, dict]]:
	"""Yield ``(line_number, object)`` from a JSONL file, skipping the first ``skip`` lines.

	Line-oriented rather than whole-file: a 500 MB result is read with a constant memory
	footprint. Blank and malformed lines are skipped rather than aborting an import that is
	otherwise fine.
	"""
	malformed = 0
	with open(path, encoding="utf-8") as handle:
		for index, raw in enumerate(handle):
			if index < skip:
				continue
			raw = raw.strip()
			if not raw:
				continue
			try:
				yield index, json.loads(raw)
			except ValueError:
				malformed += 1

	if malformed:
		# Summarised once rather than logged per line: this loop runs over millions of lines,
		# and the logger call is expensive enough to matter at that scale. Guarded because an
		# import must not die over a log file it cannot open.
		try:
			frappe.logger("shopify_integration").warning(
				f"Skipped {malformed} malformed JSONL line(s) in {path}"
			)
		except Exception:
			pass


def iter_product_groups(path: str, skip: int = 0) -> Iterator[tuple[int, dict]]:
	"""Reassemble flat JSONL into products with their variants attached.

	Yields ``(lines_done, product)`` where ``lines_done`` is the number of lines fully
	accounted for once this product has been applied -- which is what makes resumption exact.

	Shopify guarantees a child follows its parent, so one buffer suffices: a line without
	``__parentId`` starts a new product and flushes the previous one.
	"""
	current: dict | None = None
	last_index = skip - 1

	for index, obj in iter_lines(path, skip=skip):
		last_index = index

		if not obj.get("__parentId"):
			if current is not None:
				# Everything before this new root is now accounted for. The root itself is
				# not, which is why the count is exclusive of `index`.
				yield index, current
			current = dict(obj)
			current["variants"] = []
			continue

		if current is None:
			# A child whose parent was consumed in an earlier run. Its product is already
			# applied, so dropping it is correct rather than lossy.
			continue

		if _is_variant(obj):
			current["variants"].append(obj)

	if current is not None:
		# The final product has no successor to trigger its flush.
		yield last_index + 1, current


def _is_variant(obj: dict) -> bool:
	return "ProductVariant" in str(obj.get("id", ""))


def process(operation_name: str) -> dict:
	"""Apply a completed import, resuming from wherever a previous run stopped."""
	from shopify_integration.catalogue.mapping import write_product_mapping

	doc = frappe.get_doc("Shopify Bulk Operation", operation_name)
	path = download_result(operation_name)

	applied = cint(doc.objects_processed)
	skip = cint(doc.lines_consumed)
	failures = 0
	since_commit = 0

	for lines_done, product in iter_product_groups(path, skip=skip):
		try:
			write_product_mapping(doc.store, product)
			applied += 1
		except Exception:
			# Roll back what this product managed to write before it broke. Without it the
			# Items, Item Groups and Attributes it created stay in the transaction and the
			# next checkpoint commits a half-built product -- which then looks imported, so no
			# retry ever revisits it.
			frappe.db.rollback()
			failures += 1
			frappe.log_error(
				title=f"Shopify product import failed: {product.get('id')}",
				message=frappe.get_traceback(),
			)

		since_commit += 1
		if since_commit >= COMMIT_EVERY:
			# Checkpoint. lines_consumed is only advanced past products that are committed,
			# so a crash re-does at most COMMIT_EVERY products and never skips any.
			doc.db_set({"objects_processed": applied, "lines_consumed": lines_done}, update_modified=False)
			frappe.db.commit()
			publish_progress(doc, processed=applied)
			since_commit = 0

	doc.db_set({"objects_processed": applied, "lines_consumed": cint(doc.object_count) or applied})
	frappe.db.commit()
	publish_progress(doc, processed=applied, done=True)

	return {"applied": applied, "failed": failures}


def publish_progress(doc, processed: int | None = None, done: bool = False) -> None:
	"""Push progress to anyone watching the import in the UI (spec §11)."""
	total = cint(doc.root_object_count) or cint(doc.object_count)
	frappe.publish_realtime(
		"shopify_bulk_progress",
		{
			"operation": doc.name,
			"store": doc.store,
			"status": "Completed" if done else doc.status,
			"processed": processed if processed is not None else cint(doc.objects_processed),
			"total": total,
		},
		user=frappe.session.user,
	)


def poll_running_operations() -> int:
	"""Scheduled sweep: re-poll anything still in flight.

	Safety net for a lost poll job -- an import that stops being watched would otherwise sit
	at Running forever with its results never applied.
	"""
	names = frappe.get_all(
		"Shopify Bulk Operation", filters={"status": ["in", ("Created", "Running")]}, pluck="name"
	)
	for name in names:
		schedule_poll(name)
	return len(names)
