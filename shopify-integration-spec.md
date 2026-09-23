# Shopify Integration for ERPNext — Build Specification

**Target app name:** `shopify_sync` (rename if you prefer; used throughout this doc)
**Platform:** Frappe Framework v15 / ERPNext v15
**License:** GPLv3 (required for ecosystem compatibility)
**Status:** Greenfield build specification

---

## 0. How to use this document

You are building a production-grade Shopify ↔ ERPNext integration app, intended for public
release to the Frappe community. This document specifies architecture, data model, flows and
quality bar. It is deliberately prescriptive about the decisions that are expensive to change
later, and deliberately loose about the ones that aren't.

**Non-negotiables** (deviating from these breaks the product):

1. Multi-store from day one. No `issingle` settings doctype.
2. GraphQL Admin API only. No REST.
3. All outbound Shopify calls go through a queue. Never from a `doc_event` directly.
4. Every webhook is verified, deduplicated and enqueued. Never processed inline.
5. Money is never handled as `float`.

**Before you write code:** verify every GraphQL mutation signature, field name and input shape
in this document against the official Shopify Admin API documentation for the API version you
pin. This spec describes intent and shape; Shopify's schema is the authority, and it changes
quarterly. Where this document and Shopify's docs disagree, Shopify wins — and you should fix
this document.

---

## 1. Why this app exists

An existing app, `frappe/ecommerce_integrations`, already provides Shopify integration for
ERPNext. This new app is justified by four limitations in it that are architectural rather than
cosmetic. These were verified by source inspection, and they are your positioning:

| Limitation in the existing app | Evidence | What we do instead |
|---|---|---|
| One Shopify store per ERPNext site | `Shopify Setting` has `"issingle": 1` | `Shopify Store` is a normal doctype; everything is keyed by store |
| REST only | Zero GraphQL in the codebase | GraphQL-first. REST went legacy 2024-10-01 |
| Inventory sync polls on a 5–60 min timer | Scheduler-driven, 5 min floor | Event-driven, seconds |
| No refunds / returns | No refund handling exists | First-class Credit Note flow |

Secondary gaps also addressed: price list sync (existing app pushes a single custom field, not
`Item Price`), and multi-currency (existing app has none).

**Non-goals.** Explicitly out of scope; do not build these:

- Shopify POS as a distinct channel
- Shopify Payments payout reconciliation
- Draft orders, abandoned checkouts, gift cards
- Collections, metafields, SEO fields, tags
- ERPNext pricing rules applied to Shopify-originated orders (Shopify's price is authoritative)

---

## 2. Prior art — what to reuse

`ecommerce_integrations` is GPLv3. You may read and adapt it, and you should.

**Reuse the logic for** (this is where bespoke builds bleed weeks, and the existing
implementation is correct and battle-tested):

- Tax line → ERPNext tax account head resolution
- Tax-inclusive price handling
- Shipping lines as either a charge or a line item
- Per-line discount allocation
- The `actual_qty - reserved_qty` availability formula, and *why* `reserved_qty` is in it

**Do not copy:** the settings-as-Single design, the REST client, the polling scheduler, or the
two-API-calls-per-item inventory push.

**Strategic note for the humans:** before going fully greenfield, it is worth asking the Frappe
maintainers whether multi-store and GraphQL work would be accepted into `ecommerce_integrations`.
That path trades independence for an existing user base. This spec assumes the answer was no, or
that independence was chosen deliberately.

---

## 3. Platform constraints (verified)

| Item | Value |
|---|---|
| Frappe | v15 (developed against 15.88) |
| ERPNext | v15 (developed against 15.87) |
| Python | 3.12 (support >=3.10,<3.15) |
| Shopify Admin API | GraphQL, pinned version per store |
| REST Admin API | Legacy since 2024-10-01 — do not use |

### Shopify rate limits (official, as of this writing — re-verify)

GraphQL Admin API, calculated in cost points:

| Plan | Bucket capacity | Restore rate |
|---|---|---|
| Standard | 100 points/s | 50 points/s |
| Advanced | 200 points/s | — |
| Plus | 1000 points/s | — |
| Enterprise | 2000 points/s | — |

**Do not hardcode these.** See §6.2 — read actual headroom from each response.

### Other hard limits to design around

- Webhook handler must return HTTP 200 within roughly 5 seconds or Shopify treats it as failed
- Shopify retries failed webhooks for ~48 hours with backoff — duplicates are normal
- Only **one bulk operation per store at a time**
- Shopify does not support fractional inventory quantities — integers only

---

## 4. Architecture

```
                    ┌─────────────────────────────────────────┐
                    │              Shopify                    │
                    └───────┬─────────────────────────▲───────┘
                  webhooks  │                         │  GraphQL
                            ▼                         │
              ┌──────────────────────┐    ┌───────────┴───────────┐
              │  Webhook Receiver    │    │   GraphQL Client      │
              │  HMAC → dedupe →     │    │   adaptive throttle,  │
              │  log → enqueue       │    │   retry, versioning   │
              └──────────┬───────────┘    └───────────▲───────────┘
                         │                            │
                         ▼                            │
              ┌──────────────────────┐    ┌───────────┴───────────┐
              │   Inbound Handlers   │    │   Outbound Workers    │
              │   order, refund,     │    │   drain sync queue,   │
              │   product, customer  │    │   per-store isolation │
              └──────────┬───────────┘    └───────────▲───────────┘
                         │                            │
                         ▼                            │
              ┌─────────────────────────────────────┬─┘
              │            ERPNext                  │
              │  SO / SI / DN / Credit Note /       │
              │  Item / Customer / Bin              │
              └─────────────────────────────────────┘
                         │
                    doc_events (SLE, Sales Order, Item, Item Price)
                         │
                         ▼
                  ┌──────────────┐
                  │ Sync Queue   │  ← the outbox
                  └──────────────┘
```

### Core principles

1. **The outbox.** ERPNext changes never call Shopify. They write a queue row. Workers drain it.
   This gives retry, coalescing, backpressure, ordering control and observability. It is the
   single most important decision in this document.

2. **Absolute state, not deltas.** For inventory and prices, push the *current* value read at
   drain time, not the change that triggered the sync. Out-of-order delivery then becomes
   self-healing instead of corrupting.

3. **Idempotency everywhere.** Every inbound event carries an ID; every outbound operation has a
   dedupe key. Processing the same thing twice must be a no-op.

4. **Adaptive throttling.** Read remaining capacity from each API response. Never assume a plan.

5. **Reconciliation is a feature.** Webhooks get dropped. A drift-detection job that repairs
   divergence is what separates an integration people trust from one they babysit. Build it in
   phase 1, not "later".

---

## 5. Data model

All doctypes live in module `Shopify Sync`. Field types are Frappe fieldtypes.

### 5.1 `Shopify Store`

The root of everything. Replaces the existing app's Single settings doctype.

| Field | Type | Notes |
|---|---|---|
| `store_name` | Data | Unique, the doc name |
| `enabled` | Check | |
| `shop_domain` | Data | e.g. `my-shop.myshopify.com` |
| `admin_api_token` | Password | Encrypted at rest |
| `api_secret` | Password | For webhook HMAC verification |
| `api_version` | Data | Pinned, e.g. `2026-07` |
| `company` | Link → Company | |
| `currency` | Link → Currency | Store's presentment currency |
| `default_customer` | Link → Customer | Guest checkout fallback |
| `customer_group` | Link → Customer Group | |
| `default_warehouse` | Link → Warehouse | |
| `selling_price_list` | Link → Price List | For outbound price sync |
| `cost_center` | Link → Cost Center | |
| `cash_bank_account` | Link → Account | For Payment Entries |
| `sales_order_series` / `sales_invoice_series` / `delivery_note_series` / `credit_note_series` | Select | Naming series per doctype |
| `sync_orders` / `sync_invoices` / `sync_delivery_notes` / `sync_refunds` | Check | Inbound toggles |
| `sync_inventory` / `sync_items` / `sync_prices` | Check | Outbound toggles |
| `create_missing_items` | Check | Create Item on unknown order line |
| `location_map` | Table → Shopify Location Map | |
| `tax_map` | Table → Shopify Tax Mapping | |
| `shipping_item` | Link → Item | If shipping booked as a line |
| `last_reconciled_on` | Datetime | |

### 5.2 `Shopify Location Map` (child)

| Field | Type |
|---|---|
| `warehouse` | Link → Warehouse |
| `location_gid` | Data (`gid://shopify/Location/...`) |
| `location_name` | Data (display only) |

Support mapping a **group warehouse**, consolidating all descendant leaf warehouses into one
Shopify location.

### 5.3 `Shopify Tax Mapping` (child)

| Field | Type |
|---|---|
| `shopify_tax_title` | Data |
| `account_head` | Link → Account |
| `charge_type` | Select: `sales_tax` / `shipping` |

### 5.4 `Shopify Item Link`

The mapping table. Replaces `Ecommerce Item`.

| Field | Type | Notes |
|---|---|---|
| `store` | Link → Shopify Store | |
| `item_code` | Link → Item | |
| `product_gid` | Data | |
| `variant_gid` | Data | |
| `inventory_item_gid` | Data | **Cache this.** Avoids an extra API call per sync |
| `sku` | Data | |
| `is_variant` | Check | |
| `template_item` | Link → Item | For variants |
| `inventory_synced_on` | Datetime | Delta-detection watermark |
| `price_synced_on` | Datetime | |

**Indexes — required:**
- Unique: `(store, item_code)`
- Unique: `(store, variant_gid)`
- Index: `(store, sku)`
- Index: `(store, inventory_synced_on)`

Caching `inventory_item_gid` is not an optimisation detail — the existing app omits it and
consequently spends two API calls per item on every inventory push. Do not repeat this.

### 5.5 `Shopify Sync Queue`

The outbox.

| Field | Type | Notes |
|---|---|---|
| `store` | Link → Shopify Store | |
| `operation` | Select | `inventory` / `product` / `price` / `fulfillment` |
| `dedupe_key` | Data | e.g. `inventory:<store>:<item>:<location_gid>` |
| `ref_doctype` / `ref_docname` | Data | Origin, for traceability |
| `state` | Select | `Pending` / `Running` / `Done` / `Failed` / `Superseded` |
| `attempts` | Int | |
| `next_attempt_at` | Datetime | Backoff |
| `last_error` | Long Text | |
| `payload` | JSON | Optional hint; workers re-read live state |

**Indexes — required:**
- `(store, state, next_attempt_at)` — the drain query
- Unique partial on `(dedupe_key)` where `state = 'Pending'` — enforces coalescing

Coalescing rule: enqueueing a `dedupe_key` that already has a `Pending` row is a no-op. A
20-line POS invoice produces one queue row per affected item, not twenty API calls.

### 5.6 `Shopify Event Log`

Inbound webhook record. One row per received webhook.

| Field | Type | Notes |
|---|---|---|
| `store` | Link → Shopify Store | |
| `webhook_id` | Data | From `X-Shopify-Webhook-Id`. **Unique index** — this is the dedupe |
| `topic` | Data | |
| `payload` | Long Text (JSON) | Raw body as received |
| `status` | Select | `Queued` / `Success` / `Error` / `Skipped` |
| `traceback` | Long Text | |
| `processed_on` | Datetime | |
| `ref_doctype` / `ref_docname` | Data | What it produced |

Must expose a **Retry** action that re-runs the handler against the stored payload.

### 5.7 `Shopify Bulk Operation`

State machine for Shopify's async bulk jobs (only one per store at a time).

| Field | Type |
|---|---|
| `store` | Link → Shopify Store |
| `operation_gid` | Data |
| `type` | Select: `query` / `mutation` |
| `purpose` | Data (e.g. `product_import`) |
| `status` | Select: `Created` / `Running` / `Completed` / `Failed` / `Cancelled` |
| `result_url` | Data (JSONL download) |
| `objects_processed` | Int |
| `error_code` | Data |

---

## 6. GraphQL client

### 6.1 Interface

```python
class ShopifyClient:
	def __init__(self, store: str): ...

	def execute(self, query: str, variables: dict | None = None, *, cost_hint: int = 10) -> dict:
		"""Execute a GraphQL document. Blocks on the throttle if needed.
		Raises ShopifyUserError / ShopifyThrottled / ShopifyTransportError."""

	def paginate(self, query: str, variables: dict, connection_path: str):
		"""Yield nodes across cursor pages, following pageInfo.hasNextPage."""
```

One client instance per store. Endpoint:
`https://{shop_domain}/admin/api/{api_version}/graphql.json`
Auth header: `X-Shopify-Access-Token: {admin_api_token}`.

### 6.2 Adaptive throttling — required

Every GraphQL response includes cost telemetry:

```json
{"extensions": {"cost": {
    "requestedQueryCost": 12,
    "actualQueryCost": 8,
    "throttleStatus": {
        "maximumAvailable": 1000.0,
        "currentlyAvailable": 940.0,
        "restoreRate": 50.0}}}}
```

After every call, store `currentlyAvailable` and `restoreRate` per store in Redis. Before every
call, estimate cost and sleep if projected headroom is insufficient. This auto-adapts to
Standard vs Plus with zero configuration, and it is what makes the app safe to install on a
store whose plan you do not know.

Keep a safety floor (e.g. never spend below 10% of `maximumAvailable`) so interactive operations
aren't starved by bulk backfills.

### 6.3 Error taxonomy

Shopify reports failures in three distinct places. Handle all three — conflating them is a
common and painful bug:

| Layer | Where | Meaning | Action |
|---|---|---|---|
| Transport | HTTP status | 429, 5xx, network | Retry with exponential backoff + jitter |
| GraphQL | top-level `errors[]` | Malformed query, `THROTTLED` | `THROTTLED` → back off and retry. Others → fail, do not retry |
| Business | `data.<mutation>.userErrors[]` | Validation rejected the write | **Fail permanently.** Never retry — it will never succeed |

`userErrors` is the one teams miss: HTTP 200, no `errors[]`, and the write silently did nothing.
**Always check `userErrors` on every mutation.**

### 6.4 API version policy

- Pin per store in `Shopify Store.api_version`
- Default new installs to the current stable version
- CI runs the integration suite against both the pinned default and the next release
- Surface a warning in the UI when a store's pinned version is within 90 days of sunset

---

## 7. Sync engine (outbound)

### 7.1 Enqueue

Called from `doc_events`. Must be cheap and must not touch the network.

```python
def enqueue_sync(store, operation, dedupe_key, ref_doctype=None, ref_docname=None):
	"""Insert a Pending Shopify Sync Queue row unless one already exists
	for this dedupe_key. Then schedule a drain."""
```

Then schedule the drain with Frappe's deduplicated enqueue:

```python
frappe.enqueue(
	"shopify_sync.sync.engine.drain_store",
	queue="short",
	job_id=f"shopify_drain::{store}",
	deduplicate=True,  # supported in Frappe v15
	enqueue_after_commit=True,  # critical: never enqueue before the txn commits
	store=store,
)
```

`enqueue_after_commit=True` is mandatory. Without it a worker can read the row before the
transaction that created it has committed, and push stale data.

### 7.2 Drain

```
drain_store(store):
    claim up to N Pending rows where next_attempt_at <= now, ordered by creation
      (SELECT ... FOR UPDATE SKIP LOCKED, or an atomic state flip to Running)
    group by operation
    for each group:
        read CURRENT state from ERPNext   ← not the payload
        build one batched mutation where the API allows it
        execute
        on success  → state = Done
        on userError→ state = Failed, record error, no retry
        on throttle → state = Pending, next_attempt_at = now + backoff
        on transport→ state = Pending, attempts += 1, exponential backoff + jitter
    if more Pending rows remain, re-enqueue self
```

Requirements:

- **Per-store isolation.** One throttled store must never stall another. Separate job per store.
- **Claim semantics.** Two workers must never process the same row. `SKIP LOCKED` or an atomic
  `Pending → Running` update guarded by row version.
- **Max attempts** (suggest 5), then `Failed` with the error preserved for inspection.
- **Stale `Running` recovery.** A worker killed mid-flight leaves `Running` rows. A janitor
  resets rows stuck in `Running` beyond a timeout back to `Pending`.

---

## 8. Inbound: Shopify → ERPNext

### 8.1 Webhook receiver

```python
@frappe.whitelist(allow_guest=True)
def webhook():
    # 1. Read raw body BEFORE any parsing
    # 2. Verify HMAC-SHA256 (base64) of raw body against store api_secret,
    #    using a constant-time comparison
    # 3. Resolve store from X-Shopify-Shop-Domain
    # 4. Dedupe on X-Shopify-Webhook-Id -> if seen, return 200 immediately
    # 5. Insert Shopify Event Log (status=Queued)
    # 6. frappe.enqueue(handler, ...) and return 200
```

Rules, all mandatory:

- HMAC is verified **before** parsing the body. Never parse untrusted input first.
- Use a constant-time comparison (`hmac.compare_digest`). A naive `==` is timing-attackable.
- Return 200 in well under 5 seconds. **Never** do ERPNext document work inline.
- A duplicate `X-Shopify-Webhook-Id` returns 200 without reprocessing.
- An unrecognised topic returns 200 and logs `Skipped` — never raise. A 500 makes Shopify retry
  for 48 hours.

Note on multi-store: resolve the store from the shop domain header, then verify HMAC with *that
store's* secret.

### 8.2 Subscriptions

Register programmatically via `webhookSubscriptionCreate` when a store is enabled; remove them
when disabled. Never ask the user to configure webhooks by hand.

Topics required:

| Topic | Produces |
|---|---|
| `orders/create` | Sales Order |
| `orders/paid` | Sales Invoice + Payment Entry |
| `orders/fulfilled` | Delivery Note |
| `orders/partially_fulfilled` | Delivery Note (partial) |
| `orders/cancelled` | Cancel linked documents |
| `refunds/create` | Credit Note |
| `products/create` | Item (+ link) |
| `products/update` | Update Item |
| `products/delete` | Unlink (never delete the ERPNext Item) |
| `inventory_levels/update` | Drift detection only — see §10.4 |
| `customers/create`, `customers/update` | Customer / Address / Contact |

### 8.3 Orders → Sales Order

- Idempotency: unique on `(store, shopify_order_gid)`. Re-delivery must return the existing SO.
- Customer: match on Shopify customer GID; fall back to the store's `default_customer` for guest
  checkouts. Never create duplicate Customers for the same GID.
- Items: resolve via `Shopify Item Link`. Unknown line → create the Item if
  `create_missing_items`, else fail the event with a clear error naming the SKU.
- Prices: Shopify's line price is authoritative. Use a dedicated price list and set
  `ignore_pricing_rule`. ERPNext pricing rules must not rewrite web order prices.
- Attach the raw order JSON to the event log for support.
- Order note → comment on the SO.
- Submit the SO. This raises `reserved_qty`, which is load-bearing for inventory correctness
  (§10.1).

### 8.4 Paid → Sales Invoice + Payment Entry

Gated by `sync_invoices`. Build the SI from the SO, set posting date to the Shopify order date,
apply the store cost center, submit, then create and submit a Payment Entry against
`cash_bank_account`.

Handle partial payments: do not assume `orders/paid` means paid in full — check the financial
status and outstanding amount.

### 8.5 Fulfilled → Delivery Note

Gated by `sync_delivery_notes`. Map the fulfilment's location GID to a warehouse via
`location_map`, falling back to `default_warehouse`. Handle partial fulfilment: build the DN from
only the fulfilled line quantities. Multiple fulfilments on one order produce multiple DNs.

### 8.6 Refunds → Credit Note (new capability)

`refunds/create` carries refunded line items, quantities, restock flags and refunded shipping.

1. Locate the originating SO and its SI
2. Create a return Sales Invoice (`is_return = 1`, `return_against = <SI>`) with negative
   quantities for the refunded lines
3. Include refunded shipping and tax proportionally
4. If Shopify indicates restocking, ensure stock returns — via the credit note's stock effect or
   a linked return Delivery Note, depending on whether the original DN exists
5. Create a reverse Payment Entry if the refund was actually disbursed
6. Idempotency: unique on `(store, shopify_refund_gid)`

Partial refunds, refunds without restock, and refunds on unfulfilled orders must all work.
Test each explicitly.

### 8.7 Products → Items

Three paths, all converging on one mapping writer:

1. **Bulk import** (§11) — initial catalogue load
2. **Webhook** `products/create` / `products/update` — ongoing, real-time
3. **Lazy** — unknown SKU on an incoming order

Mapping: product → Item (template if it has variants), variant → Item variant. Carry over title,
description, images, weight with UOM conversion, vendor → Supplier, product type → Item Group.
Shopify permits at most 3 options — items with more attributes cannot round-trip; fail clearly.

**Echo suppression is critical.** An item created from a webhook must not immediately push back
to Shopify. Set a flag on the document before saving and check it in the outbound hook:

```python
item.flags.from_shopify = True  # inbound writer sets this
# outbound hook:
if getattr(doc.flags, "from_shopify", False):
	return
```

Without this you get an infinite Shopify → ERPNext → Shopify loop. This is the single most
common bug in bidirectional integrations.

---

## 9. Outbound: ERPNext → Shopify

### 9.1 Hooks

```python
doc_events = {
	"Stock Ledger Entry": {"on_submit": "...inventory.on_stock_movement"},
	"Sales Order": {
		"on_submit": "...inventory.on_reservation_change",
		"on_cancel": "...inventory.on_reservation_change",
	},
	"Item": {"after_insert": "...product.on_item_change", "on_update": "...product.on_item_change"},
	"Item Price": {"on_change": "...price.on_price_change"},
}
```

**Do not hook `Bin`.** Verified in ERPNext v15: `update_bin_qty()` writes with `bin.db_update()`,
a direct database write that does **not** fire doc events. A `Bin` hook will silently never run.

Every one of these handlers does exactly one thing: compute a dedupe key and call
`enqueue_sync()`. No network calls, no heavy queries. They run inside the user's save
transaction — including at the POS counter.

---

## 10. Real-time inventory (flagship feature)

### 10.1 The quantity formula

```
available = int(bin.actual_qty) - int(bin.reserved_qty)
```

`reserved_qty` is not optional. Between a web order arriving and its Delivery Note being made,
`actual_qty` has not yet dropped, but the submitted Sales Order has raised `reserved_qty`.
Omitting it makes every push bounce Shopify's stock back up and re-expose units already sold.

Shopify requires integers. Truncate, and document the behaviour for fractional-UOM items.

### 10.2 Trigger points

| Event | Why |
|---|---|
| `Stock Ledger Entry.on_submit` | Every physical movement: POS, DN, Stock Entry, Purchase Receipt, Reconciliation. Verified: SLEs are created via `sle.submit()`, so doc events fire |
| `Sales Order.on_submit` / `on_cancel` | Changes `reserved_qty`. SLE hooks alone would miss this |

Dedupe key: `inventory:{store}:{item_code}:{location_gid}`.

Only enqueue when the item has a `Shopify Item Link` for a store, and the warehouse maps to a
location in that store. Most stock movements in a 87k-item catalogue touch nothing Shopify knows
about — check cheaply and return early.

### 10.3 The push

At drain time, re-read current `Bin` values, then batch:

```graphql
mutation inventorySetQuantities($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) {
    inventoryAdjustmentGroup { createdAt reason }
    userErrors { field message code }
  }
}
```

with `name: "available"`, a `referenceDocumentUri` identifying the ERPNext document, and a
`quantities[]` array of `{inventoryItemId, locationId, quantity, compareQuantity}`.

- Batch many items into one mutation. This is the whole point of moving off REST.
- `compareQuantity` gives optimistic concurrency — if Shopify's value changed underneath you,
  the write is rejected rather than clobbering it. Re-read and retry once.
- Use the cached `inventory_item_gid` from `Shopify Item Link`. Never look it up per push.
- On success, stamp `inventory_synced_on`.

**Verify this mutation's exact input shape against your pinned API version before implementing.**

Target latency: under 10 seconds from POS save to Shopify, typically 2–5.

### 10.4 What `inventory_levels/update` is for

Subscribe, but **do not** write ERPNext stock from it. ERPNext is the master. Use it only to
detect drift: if Shopify's level disagrees with ERPNext's computed availability and no sync is
pending, log it and enqueue a corrective push. Writing ERPNext stock from Shopify creates a
feedback loop with no stable fixed point.

---

## 11. Bulk operations

Required for catalogue-scale work — a 87,000-item import via paginated queries is not viable.

**Import flow:**

1. `bulkOperationRunQuery` with a products+variants query
2. Poll `currentBulkOperation` (or use the `bulk_operations/finish` webhook)
3. On completion, stream the JSONL result URL — **stream it, never load into memory**
4. Process in batches, committing periodically
5. Record progress in `Shopify Bulk Operation`

Constraints:

- One bulk operation per store at a time. Guard with the state machine; queue requests behind it.
- JSONL is flat with parent/child linked by `__parentId` — variants arrive as separate lines
  referencing their product. Buffer accordingly.
- Bulk result URLs expire. Download promptly.
- Make the whole import resumable: a crash at object 60,000 must not restart from zero.

Use `bulkOperationRunMutation` for mass outbound work (initial price or inventory backfill).

Report progress to the UI with `frappe.publish_realtime`.

---

## 12. Reconciliation

A scheduled job (default daily, configurable) that detects and repairs drift.

**Inventory:** page all mapped variants' Shopify levels, compare against computed ERPNext
availability, enqueue corrective pushes for mismatches. Log every correction — a rising
correction count means webhooks or the queue are failing.

**Orders:** query Shopify for orders created since `last_reconciled_on` and verify each has a
Sales Order. Missing ones are replayed. This is the safety net for dropped webhooks and
downtime; the existing app's hourly order catch-up is the right instinct, generalised.

Surface results in a dashboard: last reconciliation, drift found, drift repaired, unresolved.

---

## 13. Taxes, money and currency

This is where integrations quietly produce wrong books. Treat it as the highest-risk area.

### 13.1 Money handling

- **Never use `float`.** Use `Decimal` internally and Frappe `Currency` fields for storage.
- Shopify returns money as decimal strings (`"19.99"`). Parse with `Decimal`, never `float()`.
- Round only at document boundaries, using ERPNext's currency precision.
- Reconcile totals: after building any document, assert its total matches Shopify's total within
  the currency's smallest unit. Mismatch = fail loudly with both figures in the error. Never
  silently book a document whose total disagrees with the order.

### 13.2 Tax handling

Support all of:

- **Tax-inclusive pricing** (`taxes_included`) — back out tax correctly rather than adding on top
- **Tax line → account head** mapping via `tax_map`, with a clear error when a tax title is
  unmapped (do not silently drop it)
- **Consolidation** — optionally merge multiple Shopify tax lines into one ERPNext tax row
- **Shipping** — either a charge row or a line item, per store config
- **Per-line discounts** — allocate discount allocations to the right lines
- **Zero-rated lines** — mark as free rather than dropping them

Port this logic from `ecommerce_integrations` rather than deriving it fresh. It is correct, it
is GPL, and rebuilding it from scratch buys nothing but new bugs.

### 13.3 Multi-currency

Each store has a presentment currency. If it differs from the Company's base currency:

- Set the document's currency and a conversion rate
- Prefer Shopify's own presented/base amounts over re-deriving from a rate table
- Ensure the Payment Entry uses a consistent rate with its invoice

---

## 14. Error handling and observability

**Design rule:** every failure must be visible in the UI, attributable to a store, and
replayable. "It didn't sync" must always have an answer.

| Surface | Purpose |
|---|---|
| `Shopify Event Log` | Every inbound webhook: payload, status, traceback, Retry action |
| `Shopify Sync Queue` | Every outbound op: state, attempts, last error, Requeue action |
| Dashboard | Per store: pending/failed counts, last successful sync, API headroom, last reconciliation |

Failure classification:

- **Transient** (throttle, 5xx, network) → automatic retry with backoff. Do not alert.
- **Permanent** (`userErrors`, unmapped tax account, missing SKU) → `Failed`, surfaced
  immediately with an actionable message naming the specific record.
- **Poison** (repeatedly failing) → park after max attempts, never block the queue behind it.

Error messages must name the thing: not "sync failed" but "Item SKU-1234 has no Shopify variant
mapping for store my-shop".

---

## 15. Security

- Store `admin_api_token` and `api_secret` as Frappe `Password` fields (encrypted at rest).
  Never log them, never return them in API responses, and redact them in tracebacks.
- Verify webhook HMAC on the **raw** body before parsing, with `hmac.compare_digest`.
- The webhook endpoint is `allow_guest=True` and therefore publicly reachable. It must do nothing
  except verify, log and enqueue. No side effects before HMAC verification passes.
- Request the minimum Shopify access scopes needed; document exactly which and why.
- Rate-limit / size-cap the webhook endpoint defensively.
- Treat all Shopify payload content as untrusted input, especially anything rendered in the UI.

---

## 16. Testing

The existing app has roughly 400 lines of tests against 1,670 lines of Shopify code. Beat that
clearly — for a community release, test quality is what earns trust.

| Layer | Approach |
|---|---|
| Unit | Pure mapping functions — order JSON → SO dict, tax computation, quantity formula. No DB, no network |
| Integration | `FrappeTestCase` against a real test site, with recorded HTTP cassettes (VCR-style). No live API in CI |
| Contract | A scheduled job replaying cassettes against the live API on a free dev store, to catch schema drift early |
| Migration | Fixture of `ecommerce_integrations` data → run migration → assert mappings |

**Mandatory test cases** (each of these is a real production failure mode):

- Duplicate webhook delivery → exactly one document
- Out-of-order inventory events → final state correct
- Echo loop: product webhook → Item save → must not push back
- Partial fulfilment, then the remainder
- Partial refund; refund without restock; refund on an unfulfilled order
- Tax-inclusive order totals match Shopify's totals exactly
- Throttled response → backoff and eventual success
- `userErrors` returned → marked Failed, not retried
- Worker killed mid-drain → `Running` rows recovered
- Two stores, same SKU, independent mappings and independent throttling

---

## 17. Migration from `ecommerce_integrations`

This is the biggest adoption lever. Existing users have mappings and history they cannot lose.

Provide a single command / wizard that:

1. Reads existing `Shopify Setting` (Single) and creates one `Shopify Store` from it
2. Converts every `Ecommerce Item` row (integration = `shopify`) into `Shopify Item Link`,
   translating numeric REST IDs into GIDs (`gid://shopify/ProductVariant/{id}`)
3. Backfills `inventory_item_gid` via a bulk query
4. Maps existing warehouse and tax mappings into the new child tables
5. Leaves existing SO/SI/DN history untouched, reading legacy custom fields for linkage
6. Runs **read-only in dry-run mode first**, reporting exactly what it would do

Also handle the **older, pre-`ecommerce_integrations`** case: ERPNext's built-in Shopify connector
(removed after v13) left custom fields `shopify_product_id`, `shopify_variant_id`,
`shopify_description`, `shopify_order_id`, `shopify_order_number`, `shopify_fulfillment_id`,
`shopify_customer_id`, `shopify_supplier_id`, `shopify_address_id`. Sites upgraded from v12/v13
often still carry these, orphaned and empty. Detect them, offer to adopt any populated mappings,
and offer to clean up the empty ones.

Note for installers: if those legacy fields are also exported into a site's own custom app
fixtures, they will be re-created on every `bench migrate` and will fight the app's own
definitions. Detect this and warn explicitly.

---

## 18. Project layout

```
shopify_sync/
├── pyproject.toml                # frappe >=15,<16; erpnext >=15,<16; python >=3.10,<3.15
├── README.md                     # lead with multi-store + GraphQL + real-time
├── LICENSE                       # GPLv3
├── .github/workflows/ci.yml      # lint, unit, integration w/ cassettes
└── shopify_sync/
    ├── hooks.py
    ├── api/
    │   ├── client.py             # GraphQL client
    │   ├── throttle.py           # adaptive token bucket
    │   ├── queries/              # .graphql documents, versioned
    │   └── bulk.py               # bulk operation state machine
    ├── sync/
    │   ├── engine.py             # enqueue + drain
    │   └── reconcile.py
    ├── inbound/
    │   ├── webhook.py            # receiver: HMAC, dedupe, enqueue
    │   ├── order.py  refund.py  product.py  customer.py
    ├── outbound/
    │   ├── inventory.py  product.py  price.py
    ├── shopify_sync/doctype/
    │   ├── shopify_store/  shopify_item_link/  shopify_sync_queue/
    │   ├── shopify_event_log/  shopify_bulk_operation/
    │   ├── shopify_location_map/  shopify_tax_mapping/
    ├── migration/
    │   └── from_ecommerce_integrations.py
    └── tests/
        ├── cassettes/
        └── test_*.py
```

Keep GraphQL documents as `.graphql` files, not inline strings. They are versioned artifacts that
need diffing when the API version changes.

---

## 19. Delivery phases

Each phase ends with working, tested, demoable software. Do not build the data model for all
phases up front and integrate at the end.

| Phase | Deliverable | Acceptance |
|---|---|---|
| **1. Foundation** | `Shopify Store`, GraphQL client with adaptive throttle, `Shopify Sync Queue`, `Shopify Event Log`, webhook receiver | Connect two stores; receive and verify a webhook; observe throttle adapting under load |
| **2. Catalogue** | Bulk import, `Shopify Item Link`, product webhooks, echo suppression | Import a 50k+ product catalogue; resumable after a kill; no echo loop |
| **3. Orders** | `orders/create` → SO, paid → SI + PE, fulfilled → DN, cancelled | Duplicate webhooks produce one SO; totals match Shopify exactly; partial fulfilment works |
| **4. Real-time inventory** | SLE + SO hooks, batched `inventorySetQuantities`, drift detection | POS sale reflected on Shopify in under 10s; 20-line invoice = 1 queue row per item, batched |
| **5. Refunds & prices** | `refunds/create` → Credit Note, `Item Price` → Shopify | Partial refund with restock produces correct stock and accounting |
| **6. Reconciliation & ops** | Drift job, dashboard, retry actions | Kill webhooks for an hour; reconciliation detects and repairs everything |
| **7. Release** | Migration tool, docs, CI, README | Migrate a real `ecommerce_integrations` site with zero mapping loss |

Validate against a real production catalogue as early as phase 2. An integration with one
demanding real deployment behind it is far more credible than one built to a spec.

---

## 20. Definition of done

A feature is complete when all of the following hold:

- [ ] Works correctly with **two** stores configured, not one
- [ ] Idempotent: running it twice changes nothing the second time
- [ ] All outbound calls go through the queue; no network I/O in a `doc_event`
- [ ] Both `errors[]` and `userErrors[]` handled, with distinct retry semantics
- [ ] Failures are visible in the UI, attributable to a store, and replayable
- [ ] Money paths use `Decimal`; document totals reconcile against Shopify's
- [ ] Covered by unit tests, plus an integration test with a recorded cassette
- [ ] Degrades sanely when Shopify is unreachable — queues, never loses, never blocks a save
- [ ] Nothing in the save path can make a POS transaction slow

---

## Appendix A — verified facts

Established by direct source inspection; you can rely on these without re-checking:

- `ecommerce_integrations`' `Shopify Setting` is `"issingle": 1` — one store per site
- That app contains **zero** GraphQL; it is entirely REST via `ShopifyAPI==12.7.0`
- Its inventory delta query is `WHERE bin.modified > ecommerce_item.inventory_synced_on` —
  already incremental, and a good model to follow
- Its inventory push costs **two** REST calls per item (`Variant.find` then `InventoryLevel.set`)
  because `inventory_item_id` is not cached — do not repeat this
- ERPNext v15 updates `Bin` via `bin.db_update()`, which **bypasses doc events** — a `Bin` hook
  will never fire
- ERPNext v15 creates Stock Ledger Entries via `sle.submit()`, so SLE doc events **do** fire
- Frappe v15 `frappe.enqueue()` supports `job_id`, `deduplicate` and `enqueue_after_commit`
- Shopify REST Admin API: legacy since 2024-10-01
- Shopify GraphQL rate limits: Standard 100 pts/s, Advanced 200, Plus 1000, Enterprise 2000

## Appendix B — must verify before coding

These change with Shopify's quarterly releases. Confirm against the docs for your pinned version:

- Exact input shape of `inventorySetQuantities` (and whether `compareQuantity` is supported)
- `bulkOperationRunQuery` / `bulkOperationRunMutation` signatures and JSONL result format
- `webhookSubscriptionCreate` input and the current topic enum names
- Refund payload structure: line items, restock flags, shipping and tax breakdown
- Required access scopes for each operation
- Current GraphQL cost of the queries you rely on

## Appendix C — references

- Shopify API limits: https://shopify.dev/docs/api/usage/limits
- REST rate limits and legacy notice: https://shopify.dev/docs/api/admin-rest/usage/rate-limits
- GraphQL Admin API: https://shopify.dev/docs/api/admin-graphql
- Bulk operations: https://shopify.dev/docs/api/usage/bulk-operations/queries
- Webhooks: https://shopify.dev/docs/apps/build/webhooks
- Prior art (GPLv3): https://github.com/frappe/ecommerce_integrations
