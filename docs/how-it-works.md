# How it works

Everything this app does, in the order you would meet it. Written for whoever has to run it
or fix it at two in the morning, not for whoever wrote it.

---

## 1. The shape of the thing

ERPNext is the source of truth for stock and money. Shopify is the source of truth for
orders. The app keeps the two honest in both directions, and never lets one silently
overwrite the other.

```
                    Shopify                               ERPNext
                       |                                     |
   order placed  ──────┼──── webhook ──────────────────────► Sales Order
   customer paid ──────┼──── webhook ──────────────────────► Sales Invoice + Payment
   refund issued ──────┼──── webhook ──────────────────────► Credit Note (+ restock)
   stock edited  ──────┼──── webhook ──────────────────────► drift check (never overwrites)
   product edited ─────┼──── webhook ──────────────────────► Item
                       |                                     |
   Shopify stock ◄─────┼──── sync queue ──────────────────── stock moved / order placed
   Shopify price ◄─────┼──── sync queue ──────────────────── Item Price changed
   Shopify product ◄───┼──── sync queue ──────────────────── Item renamed
   order fulfilled ◄───┼──── sync queue ──────────────────── Delivery Note submitted
   tracking added ◄────┼──── sync queue ──────────────────── Shipment submitted
```

Two rules hold everywhere:

* **Shopify's money is authoritative.** Prices, discounts and tax come from Shopify's own
  computed figures, never re-derived. A document whose total disagrees with Shopify is
  refused before it posts, not corrected.
* **ERPNext's stock is authoritative.** A Shopify stock change is treated as drift to be
  corrected, never as instruction. The app has no code path that writes ERPNext stock from
  Shopify.

---

## 2. Setting it up

In order. Each step depends on the one before.

1. **Install the app.** `bench --site <site> install-app shopify_integration`. It needs
   ERPNext. It creates its own doctypes, adds its custom fields, and builds its indexes.
2. **Create a Shopify app** in the Shopify dashboard. Note the Client ID and Client Secret.
3. **Make the site publicly reachable over HTTPS.** Shopify has to be able to call you.
   A real domain in production; ngrok on a laptop.
4. **Create a Shopify Store record** in ERPNext. Fill in the shop domain, company, warehouse
   and Client ID/Secret.
5. **Connect to Shopify.** The button runs the OAuth install and stores the access token.
   Nothing works before this.
6. **Map your locations and tax accounts** on the store record.
7. **Tick Enabled.** *This is the step that registers webhooks.* Until now Shopify has not
   been told to call you.
8. **Import the catalogue** (see §4).

For India, also install `india_compliance` before step 1, and set **Default HSN/SAC Code** on
the store — see §9.

---

## 3. The queue, and why everything goes through it

Nothing writes to Shopify from inside your save. A stock movement writes one row to
**Shopify Sync Queue** and returns; a background worker drains it.

This is deliberate and it is the single most important design decision in the app:

* **Your work never waits on Shopify.** A POS sale completes at the counter whether or not
  Shopify is reachable.
* **Your work never fails because of Shopify.** If the queue write itself breaks, the
  document still saves. Tested explicitly.
* **The queue coalesces.** Eight stock movements on one item produce **one** API call, not
  eight. Rows carry a dedupe key, and a pending row for the same key is reused.
* **Handlers read current state at drain time**, never a stored payload. A row that waited
  behind a backlog pushes what the stock *is now*, not what it was when the row was written.

Failures retry with exponential backoff and jitter, five attempts, then park as **Failed**
with the reason on the row. A poison row never blocks the rows behind it.

**Where to look when something is stuck:** Shopify Sync Queue (outbound) and Shopify Event
Log (inbound). Both have Requeue/Retry buttons.

---

## 4. Products and the catalogue

**Importing.** Shopify's bulk API dumps the whole catalogue to a file, which the app streams
to disk and applies line by line. A 50,000-product import uses constant memory and is
resumable: it records how many lines it has applied and a resumed run skips exactly that many.

**Mapping.** One **Shopify Item Link** per (store, ERPNext item). It caches the product,
variant and inventory-item IDs so an inventory push costs one API call instead of three.

**What a Shopify change does to an ERPNext Item:**

| In Shopify | In ERPNext |
|---|---|
| Product created | Item created, mapped |
| Variant added | New Item, linked as a variant of the template |
| Renamed | Item renamed |
| Archived | Item disabled |
| **Deleted** | **Item kept, disabled, unlinked** |

That last row matters. Deleting in Shopify never deletes in ERPNext. Your stock history and
past orders stay intact; only the link goes. Nothing Shopify does can remove an accounting
record.

A product with more than 100 variants is **refused** rather than partially imported.

### Adding a product from ERPNext

Products normally start life in Shopify. To start one in ERPNext instead, two switches must
both be on:

1. **Publish New Items to Shopify** on the Shopify Store
2. **Publish to Shopify** on the Item

Both are off by default, and deliberately so: an ERPNext catalogue is mostly raw materials,
packaging and internal parts, and unpublishing thousands of products by hand is not a
recoverable mistake.

With both ticked, saving the Item creates the Shopify product, gives its variant your item
code as the SKU and the price from the store's price list, turns on inventory tracking, and
writes the link. Stock you receive afterwards flows out as usual.

**Items with variants work too.** Tick **Publish to Shopify** on the *template*. The app reads
the attributes its variants actually use, builds the Shopify product options from those, and
creates one Shopify variant per ERPNext variant — each with its own SKU, price and inventory
tracking.

The options come from the variants that exist, not from the Item Attribute's full value list:
an attribute may hold every size the business has ever stocked, and a shop offering two of
them should show two.

Add a size later and it appears in the shop on its own. A new variant inherits the template's
decision — nobody ticks a box per size.

A template with no variants publishes nothing: there is nothing to sell, and Shopify would
reject a product carrying options with no variants for them.

### Who owns the storefront copy

**Shopify does, and the app leaves it alone.** The title a customer reads and the description
on the product page are merchandising, written by whoever writes your marketing. ERPNext's
`item_name` is a warehouse label and its `description` is a picking note. They are not the
same thing and must not overwrite each other.

So an ordinary Item save pushes exactly one field to an existing product: whether it is
**active or archived**, which follows the Item's `disabled` flag. Title and description are
not sent.

This was a real bug, not a hypothetical. `push_products` used to send both on every save, so
editing an Item's *weight* in ERPNext did this:

```
"Banarasi Silk Saree — Festive Edition"   ->  "The Inventory Not Tracked Snowboard"
79 characters of marketing HTML           ->  35 characters of plain ERPNext text
```

Nothing failed and nothing was logged. The merchant would have found out from the shop.

Two deliberate exceptions:

- **A product the app creates** sends the title and description once, at creation. That is the
  product's initial copy, and there is nothing in Shopify yet to overwrite. Edit it in Shopify
  afterwards and the edit stands.
- **Let ERPNext Overwrite Shopify Titles and Descriptions**, a per-store switch that is off by
  default, for catalogues genuinely mastered in ERPNext. Turning it on means any Item save
  replaces both fields in Shopify, including a save that had nothing to do with either.

---

## 5. Inventory, in both directions

### ERPNext → Shopify

What Shopify may sell is:

```
available = floor(actual_qty − reserved_qty)
```

`reserved_qty` is the crucial half. When a Sales Order is submitted the goods are still on
the shelf, but they are spoken for — so Shopify stops offering them **immediately**, before
anything ships. That is what prevents overselling.

| You do | Shopify sees |
|---|---|
| Receive 25 units | +25 |
| Issue 10 units | −10 |
| **Submit a Sales Order for 7** | **−7** (nothing has moved yet) |
| Cancel that order | +7 |
| **Ship the order** | **no change** — already accounted for at order time |

Every push is compare-and-set: the app tells Shopify what it believes the old value was, and
Shopify refuses if someone changed it in between.

### Shopify → ERPNext

An `inventory_levels/update` webhook is a **drift report, never an instruction**. The app
compares Shopify's number against ERPNext's, and if they differ it queues a correction *to
Shopify*. ERPNext's stock is never written from a webhook.

### Reconciliation

Nightly, and on demand via `bench --site <site> shopify-reconcile`. It walks every mapped
item, compares both sides, corrects Shopify, and replays orders Shopify has that ERPNext does
not — the safety net for a webhook that was dropped.

---

## 6. Orders, money and shipping

```
orders/create    →  Sales Order (submitted)
orders/paid      →  Sales Invoice + Payment Entry
orders/fulfilled →  Delivery Note
refunds/create   →  Credit Note (+ return Delivery Note if it restocks)
orders/cancelled →  cancels the lot, in dependency order
```

**Customers and addresses.** A first-time buyer becomes an ERPNext Customer with billing and
shipping addresses and a Contact. A repeat buyer is matched on the Shopify customer ID — not
on email, because people change emails and households share them. A guest checkout falls back
to the store's default customer.

Shopify fires `customers/create` a fraction of a second *before* `orders/create`, so the buyer
usually exists by the time the order lands. Addresses are written on both paths; they used to
be written only when creating the customer, which meant every webhook order lost them.

**Partial everything is normal.** Partial fulfilment produces one Delivery Note per Shopify
fulfilment. Partial refunds credit only the lines refunded. An item appearing on two order
lines is drawn down correctly and never ships, credits or restocks twice.

**Cancellation order matters** and is fixed: Payment Entries, then returns, then the documents
they return against. Any other order leaves ERPNext refusing to cancel.

### Shipping out (ERPNext → Shopify)

Off by default. Tick **Sync Fulfilments to Shopify** on the store.

| You submit | Shopify gets |
|---|---|
| **Delivery Note** | the order marked fulfilled — this sends the customer's dispatch email |
| **Shipment** | carrier, AWB number and tracking URL attached to that fulfilment |

Partial shipments work: ship 2 of 3 and Shopify says *partially fulfilled*; ship the last one
and it says *fulfilled*, with two fulfilments.

**Email Customer on Fulfilment** is separate and also off by default. Leave it off while
importing a backlog of orders that already shipped, or every one of those customers hears from
you again.

---

## 7. Tax

The app takes Shopify's computed tax and reproduces it in ERPNext. It never calculates tax
itself — what the customer was charged at checkout is Shopify's business, set in Shopify.

**Each line is taxed at its own rate.** Every order line gets an Item Tax Template built from
the rates Shopify charged that line. An order mixing a 5% saree, an 18% kurti and 3%
jewellery produces three correct lines, not one blended average.

A line Shopify charged **no** tax on gets an explicit **0% template**. Without one ERPNext
falls back to the order's dominant rate and silently taxes it — and because the total still
reconciles, nothing complains.

**Tax-inclusive pricing** (the Indian norm) is handled by ERPNext's own mechanism rather than
by subtracting tax by hand, which cannot be done exactly at two decimal places.

Shopify sometimes states a rate that does not reproduce its own amount. The **amount wins** —
that is what the customer paid, and a document that does not total it is refused.

---

## 8. What is guarded, and what happens when it is not

| Guard | What it stops |
|---|---|
| Order total must equal Shopify's | a Sales Order that quietly disagrees with the sale |
| **Credit note total must equal the refund** | a refund that credits the wrong amount |
| Unique index on (store, order ID) | duplicate Sales Orders from redelivered webhooks |
| Unique index on (store, refund ID) | crediting a customer twice |
| HMAC on every webhook | anyone but Shopify posting to your public endpoint |
| Dedupe on webhook ID | Shopify's at-least-once delivery creating two of everything |
| Echo suppression, two mechanisms | Shopify → ERPNext → Shopify loops |
| Compare-and-set on inventory | overwriting a change someone made in between |

A failing webhook records **Error** with its traceback on the Shopify Event Log, and survives
the rollback that follows — so a failure is always visible, never a row silently stuck at
*Queued*.

---

## 9. India and GST

Optional. Install `india_compliance` **before** this app if you need it.

**Required setup:** set **Default HSN/SAC Code** (six or eight digits) on the Shopify Store.
`india_compliance` makes HSN mandatory on every sales item and Shopify has no such field, so
without it the very first product of an import fails to save and nothing imports.

An item that **already has** an HSN keeps it, always. The default is only used for items the
app creates, and you correct it afterwards in ERPNext.

**What works:** CGST/SGST for intra-state and IGST for inter-state, split correctly per line;
place of supply derived from the buyer's state; HSN on every invoice line; company GSTIN.
Verified against GSTR-1 — B2C(Small) reports each rate separately, and the HSN summary puts
every product under its own code at its own slab.

**What is not built:** buyer GSTIN for B2B sales (Shopify does not collect it by default), and
e-Invoice / IRN / e-Way Bill.

---

## 10. Running it day to day

```bash
# reconcile now instead of waiting for tonight
bench --site <site> shopify-reconcile --store "My Shop"
```

**Where to look when something is wrong:**

| Symptom | Look at |
|---|---|
| An order did not arrive | Shopify Event Log — was it delivered? what status? |
| It arrived but did nothing | the event's traceback; use Retry |
| Shopify stock looks wrong | Shopify Sync Queue for Failed rows, then run reconcile |
| A price did not update | the queue; check the price is on the store's own price list |
| Nothing at all is arriving | are the webhook subscriptions still registered? (§11) |

---

## 11. Things that will bite you

**One shop domain, one Shopify Store record.** Two records pointing at the same shop fight
over webhook subscriptions — deleting or disabling either one unregisters the app's webhooks
for the *whole shop*, and the other silently stops receiving anything. This happened during
development and took a while to spot.

**Restarting ngrok changes the URL**, and the old subscriptions keep pointing at the dead one.
Update the site's `host_name` and re-enable the store to re-register. Re-registration is a
reconcile, not a blind create: it replaces stale subscriptions and leaves correct ones alone,
so doing it twice is harmless.

**Count your workers.** Each `bench worker` holds the code it loaded at startup. Several
stale workers competing for jobs will run several different versions of your code, and a fix
will appear not to work. If a change seems to have no effect, check how many workers are
running before you debug anything else.

**Turning on fulfilment emails your customers.** Both toggles start off for that reason.

**Leave Customer Group blank unless you mean it.** ERPNext keeps a global default of
`All Customer Groups`, and Frappe fills that into any new document with a matching field —
including the Shopify Store, without anyone choosing it. But that value is the *root* of the
group tree, and a Customer cannot hold a group node. Left as-is it broke every order import
on a fresh site with `Cannot select a Group type Customer Group`, while the store looked
perfectly configured. The app now treats a group node here as "nothing chosen" and falls back
to `Individual`, and the picker no longer offers one.

**The store's currency is Shopify's business.** It cannot be changed through the API, and
Shopify locks it once the store has orders. An Indian store should be created as India/INR
from the start.

---

## 12. What is still not proven

Being straight about the edges:

* **Load and concurrency.** Everything has been exercised one operation at a time. The queue
  is built for concurrent workers and the claim uses `FOR UPDATE SKIP LOCKED`, but it has not
  been run under real parallel load.
* **A very large catalogue.** The bulk importer is written for 50,000 products and tested on
  a handful.
* **B2B GST.** Buyer GSTIN is not captured, so invoices file as B2C.
* **A real migration.** The migration tooling was removed at the owner's request.
