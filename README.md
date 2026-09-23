# Shopify Integration for ERPNext

[![CI](https://github.com/tanishkothari9/shopify-integration/actions/workflows/ci.yml/badge.svg)](https://github.com/tanishkothari9/shopify-integration/actions/workflows/ci.yml)

A Shopify ↔ ERPNext integration built around three things the existing options don't do:

**Multi-store.** Connect as many Shopify stores to one ERPNext site as you like. Every
mapping, credential, queue row and throttle bucket is keyed by store, so two shops selling
the same SKU stay independent — including when one of them is being rate-limited.

**GraphQL-first.** The Shopify REST Admin API has been legacy since 2024-10-01. This app
speaks only the GraphQL Admin API, which means batched mutations (one call for fifty
inventory levels, not fifty), bulk operations for catalogue-scale work, and cost-based
throttling that adapts to the shop's plan without being told what it is.

**Real-time inventory.** Stock moves reach Shopify in seconds, not on a 5-to-60-minute
timer. Every stock ledger entry and every sales order reservation enqueues a push, and the
worker sends the *current* availability — so out-of-order events heal instead of corrupting.

Plus first-class refunds: a Shopify refund becomes a proper ERPNext Credit Note, with
restocking, proportional tax and shipping, and a reversing Payment Entry.

## Status

Working today:

*Foundation*

- `Shopify Store` — multi-store configuration with encrypted credentials
- GraphQL client with adaptive, plan-agnostic throttling and a three-layer error taxonomy
- `Shopify Sync Queue` — the outbox: coalescing, claim semantics, backoff, stale recovery
- `Shopify Event Log` — every webhook, verified, deduplicated and replayable
- Webhook receiver and automatic subscription registration

*Catalogue*

- Bulk catalogue import over Shopify's async JSONL export — streamed to disk, applied in
  batches, and **resumable**: a crash at product 60,000 continues from 60,000
- `Shopify Item Link` — the mapping table, with the `inventoryItem` GID cached so inventory
  pushes cost one API call rather than two
- Product webhooks (create / update / delete), with delete unlinking rather than deleting
  the ERPNext Item
- Multi-option products map to ERPNext variant templates; single-variant products map to
  plain Items
- Echo suppression, so a product arriving from Shopify never pushes itself back

*Orders*

- `orders/create` → a submitted Sales Order, idempotent on `(store, order GID)` so Shopify's
  retries cannot produce a second order for one sale
- `orders/paid` → Sales Invoice plus a Payment Entry for what was **actually** received, not
  what was owed — `orders/paid` does not reliably mean paid in full
- `orders/fulfilled` and `orders/partially_fulfilled` → one Delivery Note per Shopify
  fulfilment, carrying only that fulfilment's quantities
- `orders/cancelled` → cancels the linked documents, dependents first
- Customers matched on Shopify GID, never duplicated; guest checkouts fall back to the
  store's default customer
- Tax lines mapped to account heads and consolidated; tax-inclusive pricing, per-line
  discounts, shipping as either a charge or a line item, and multi-currency
- **No document is submitted whose total disagrees with Shopify's.** Every amount is a
  `Decimal` from parse to post

*Real-time inventory*

- Every stock movement — POS, Delivery Note, Stock Entry, Purchase Receipt, Reconciliation —
  queues a push the moment it is submitted, and so does a Sales Order raising `reserved_qty`
- Availability is `actual_qty - reserved_qty`, floored. `reserved_qty` is not optional:
  between a web order arriving and its Delivery Note being made, nothing physical has moved
  but those units are already sold
- Pushes are **batched** into one `inventorySetQuantities` call, with `compareQuantity` for
  optimistic concurrency, so a hand edit in Shopify admin is not silently clobbered
- Twenty stock movements on one item produce **one** queue row, not twenty API calls
- `inventory_levels/update` is used for drift detection only. ERPNext is the master; stock is
  never written back from Shopify, because that loop has no stable fixed point

*Refunds*

- `refunds/create` → a return Sales Invoice against the original, carrying only the refunded
  lines and quantities, with tax and shipping taken from Shopify's own breakdown
- Restocking follows Shopify's per-line `restockType`, and **how** the stock returns depends
  on whether it ever left: a delivered order gets a return Delivery Note, an unfulfilled one
  gets none, because booking stock back that never shipped invents inventory
- A reversing Payment Entry only when the gateway actually settled money — a store-credit
  exchange is recorded as a refund but moves nothing
- Idempotent on `(store, refund GID)`, so a redelivered webhook cannot credit twice

All twelve webhook topics Shopify sends for products, orders, refunds, inventory and
customers are handled.

*Prices*

- Selling price changes on the store's own price list push to Shopify via
  `productVariantsBulkUpdate`, grouped per product. Wholesale and cost lists are ignored, and
  an item with no price is skipped rather than zeroed on Shopify

*India and GST*

- Works with [`india_compliance`](https://github.com/resilient-tech/india_compliance). CGST +
  SGST within your own state, IGST outside it, **per line** — a 5% saree and an 18% kurti in
  one order stay at 5% and 18% rather than being blended into an average that is neither
- Item Tax Templates are created on demand from the rates Shopify actually charged, so the
  taxable value reported against each HSN code is the one the customer paid
- Place of supply derived from the buyer's state; company GSTIN on every document
- **Tax-inclusive (MRP) pricing**, which is how most Indian retail quotes a price: a ₹4,500
  saree stays ₹4,500 on the invoice and the GST is taken out of it, not added on top
- Freight carries its own rate rather than inheriting the goods' — a delivery charge taxed at
  the saree's 5% instead of 18% is wrong by a few rupees on the order and wrong in the return
- Verified against GSTR-1: B2C(Small) reports each rate separately and the HSN summary files
  every product under its own code

Not built: buyer GSTIN for B2B (Shopify does not collect it), and e-Invoice / IRN / e-Way Bill.

*Reconciliation and operations*

- A nightly drift check compares every mapped variant's Shopify level against ERPNext's and
  enqueues corrections, and replays any order Shopify has that ERPNext does not
- A dashboard on each store: pending and failed counts, webhook errors, live API headroom,
  last successful sync, last reconciliation
- Retry on any event log, Requeue on any failed queue row

## Documentation

| | |
|---|---|
| **[docs/prerequisites.md](docs/prerequisites.md)** | **Start here.** What to set up in Shopify, then in ERPNext, in order |
| [docs/how-it-works.md](docs/how-it-works.md) | The whole flow, and the things that will bite you |
| [docs/operations.md](docs/operations.md) | Running it day to day |
| [docs/test-plan.md](docs/test-plan.md) | What has been verified end to end, and how |
| [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) | Contributing, and reporting a vulnerability |

[The build specification](shopify-integration-spec.md) records the original plan.

## Importing a catalogue

Open the Shopify Store and click **Import Catalogue**. The import runs in the background and
reports progress on the form. It is safe to re-run: mappings are keyed by
`(store, variant GID)`, so a second import updates rather than duplicates.

## Requirements

| | |
|---|---|
| Frappe | v15 |
| ERPNext | v15 |
| Python | 3.10–3.13, 3.12 recommended (Frappe v15 does not support 3.14) |
| MariaDB | 10.6+ — `FOR UPDATE SKIP LOCKED` is required by the queue |

## Install

```bash
bench get-app https://github.com/tanishkothari9/shopify-integration
bench --site your-site.localhost install-app shopify_integration
```

## Configure a store

1. In Shopify, create a custom app with the scopes listed under [Access scopes](#access-scopes)
   and copy its **Admin API access token** and **API secret key**.
2. In ERPNext, create a **Shopify Store**: shop domain, both credentials, company and
   warehouse defaults.
3. Click **Test Connection** to confirm the token, domain and pinned API version agree.
4. Tick **Enabled**. Webhook subscriptions register automatically — you never configure
   them by hand in Shopify.

### API version

Each store pins its own Shopify API version. The default is `2026-01`, which is the version
this app's GraphQL documents have been verified against. Shopify ships a new version
quarterly and sunsets each one after twelve months; when you move a store forward, diff the
documents in `shopify_integration/api/queries/` as part of the upgrade.

### Access scopes

| Scope | Why |
|---|---|
| `read_products`, `write_products` | Catalogue sync |
| `read_orders` | Orders → Sales Orders |
| `read_inventory`, `write_inventory` | Real-time inventory |
| `read_locations` | Mapping warehouses to Shopify locations |
| `read_customers` | Customer, Address and Contact creation |
| `write_merchant_managed_fulfillment_orders` | Marking orders fulfilled and pushing tracking |

## Design

Two decisions account for most of the behaviour:

**The outbox.** ERPNext changes never call Shopify. They write a queue row; per-store
workers drain it. That buys retry, coalescing, backpressure and observability, and it means
nothing in a save path — including a POS transaction — ever waits on the network.

**Absolute state, not deltas.** A queue row records that an item needs pushing, never the
value to push. Workers re-read current state at drain time, so duplicate and out-of-order
events converge on the truth rather than compounding.

## Development

The full suite needs a site:

```bash
bench --site your-site.localhost run-tests --app shopify_integration
```

Most of it is pure and needs no site at all, but those files have to be excluded by name —
see the exact command in [CONTRIBUTING.md](CONTRIBUTING.md), which CI runs verbatim.

CI runs the site-bound suite **twice on the same site**. Handlers commit deliberately, so a
test that does not clean up after itself passes once and fails on every run after.

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request, and
[SECURITY.md](SECURITY.md) to report a vulnerability — please don't open a public issue for one.

## License

GPLv3. This app adapts logic from
[`frappe/ecommerce_integrations`](https://github.com/frappe/ecommerce_integrations),
also GPLv3.
