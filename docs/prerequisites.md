# Before you start

Everything you need to set up, in order. Plain language, no jargon.

There are three parts: **Shopify**, **ERPNext**, and **the server**. Do them in that order.

---

## Part 1 — Shopify

### 1.1 Your store must be the right country and currency

Check **Settings → General**:

| Setting | For an Indian business |
|---|---|
| Store address | Your real address, in India |
| Store currency | INR |
| Timezone | Asia/Kolkata |

**Do this before you take a single order.** Shopify locks the currency once a store has orders,
and the tax engine follows the store's country. A store set up as US/USD will never charge
Indian GST, and you cannot fix it later without starting again.

### 1.2 Create the app and copy two secrets

**Settings → Apps and sales channels → Develop apps → Create an app**

Under **Configuration → Admin API integration**, tick these eight:

```
read_products      write_products
read_orders
read_inventory     write_inventory
read_locations
read_customers
write_merchant_managed_fulfillment_orders
```

That last one is only needed if you want ERPNext to mark orders shipped. Ask for it now
anyway — adding it later means re-authorising the whole app.

Then **Install app**, and copy:

* **Admin API access token** (starts `shpat_`)
* **API secret key**

Keep them somewhere safe. You paste them into ERPNext once, and Shopify never shows the token
again.

### 1.3 Turn on GST (India)

**Settings → Taxes and duties → India**

1. Enter your **GSTIN**
2. Set the state you are **registered in** — your own state

Get step 2 right. Shopify charges CGST + SGST to customers in your registered state and IGST to
everyone else. If the wrong state is set, every sale in your own state is taxed as interstate,
which is wrong on the invoice and wrong in your GST return.

### 1.4 Set your GST rates

Shopify uses one rate for the whole store unless you tell it otherwise. Most catalogues need
more than one.

**Products → Collections** — make one **manual** collection per rate:

| Collection | What goes in it | Rate |
|---|---|---|
| GST 5% | Sarees | 5% |
| GST 18% | Kurtis, lehengas, Indo-Western | 18% |
| GST 3% | Artificial jewellery | 3% |

> Make them **manual**, not automated. Shopify refuses a tax override on an automated
> collection — it says *"Can only create tax override for manual collections."*

Then **Settings → Taxes and duties → your region → Tax overrides → Add override**, once per
collection.

### 1.5 If your prices already include GST (Indian MRP)

Most Indian retail quotes one price with the tax already inside it. A saree on the shelf at
₹4,500 is ₹4,500 at the till — the 5% GST is *within* that, not added at checkout.

**Settings → Taxes and duties → All prices include tax** — tick it.

This is the switch, and it can only be set in the Shopify admin. There is no API for it: the
REST endpoint returns `406`, and no GraphQL mutation exists.

The app follows whichever way you set it, per order, using Shopify's own `taxesIncluded` flag.
With it on, an order imports like this:

```
SAREE-001   rate 4,500.00   ← the shelf price, unchanged
            net  4,285.72   ← GST taken out
GRAND TOTAL 12,800.00       ← exactly what the customer paid
GST            681.52       ← inside the price, not on top
```

The tax rows carry `included_in_print_rate = 1`, so ERPNext prints the MRP the customer
recognises while still reporting the correct taxable value. Each line keeps its own slab — a
5% saree and an 18% kurti in one order stay separate, and GSTR-1 shows the net taxable value
per rate.

Verified live on a mixed 5/18/3% order: total 12,800.00, GST 681.52. Had the rates been
blended into one it would have come out 609.52.

### 1.6 Decide about shipping

Same page: **Charge tax on shipping**.

In India freight normally attracts GST at the rate of the goods. If you leave this off, your
delivery charge carries no GST — which is a decision, not a mistake, as long as it is
deliberate. The app copies whatever Shopify charges either way.

---

## Part 2 — ERPNext

### 2.1 Install the apps

India Compliance first, if you need GST:

```bash
bench --site yoursite.com install-app india_compliance
```

```bash
bench --site yoursite.com install-app shopify_integration
```

### 2.2 Set up your Company

**Country: India**, **Currency: INR**.

Check **GST Settings → Accounts** has these three, which India Compliance creates for you
automatically when you make the company:

```
Output Tax CGST
Output Tax SGST
Output Tax IGST
```

### 2.2a Give the Company an Address carrying your GSTIN

Easy to miss, and everything looks fine without it.

Create an **Address**, tick **Is Your Company Address**, link it to the Company, and put your
**GSTIN** on *the address* — not just on the Company record.

India Compliance works out **place of supply** by comparing your GSTIN's state code with the
customer's. With no company address, orders import perfectly — correct totals, correct per-line
GST, correct HSN — and `place_of_supply` comes out **empty**, which breaks GSTR-1.

Verified on a fresh site: the same order imported twice, once without the address and once
with.

| | Without company address | With it |
|---|---|---|
| Total | 13,538.00 ✅ | 13,538.00 ✅ |
| Per-line GST | 5 / 18 / 3% ✅ | 5 / 18 / 3% ✅ |
| **Place of supply** | **empty ❌** | `27-Maharashtra` ✅ |

### 2.3 Create the Shopify Store record

**Shopify Store → New**. The ones you must fill:

| Field | Value |
|---|---|
| Store Name | Anything, e.g. "Main Shop" |
| Shop Domain | `yourshop.myshopify.com` |
| Admin API Token | from step 1.2 |
| API Secret | from step 1.2 |
| Company | your company |
| Default Warehouse | where stock lives |
| Cash / Bank Account | where payments land |
| Shipping Item | a non-stock service item for freight |

**Leave Customer Group blank** unless you specifically want one. ERPNext pre-fills it with
`All Customer Groups`, which is the root of the tree and which a Customer cannot hold — the app
ignores that value and uses `Individual`, but blank is cleaner.

### 2.4 Tax Map — required for India

Match the names Shopify sends to your accounts:

| Shopify Tax Title | Account Head | Charge Type |
|---|---|---|
| `CGST` | Output Tax CGST - XXX | sales_tax |
| `SGST` | Output Tax SGST - XXX | sales_tax |
| `IGST` | Output Tax IGST - XXX | sales_tax |
| `Shipping` | your freight account | shipping |

Miss a row and the order stops with an error naming the tax it could not place. It never
guesses.

### 2.5 Default HSN/SAC Code — required for India

A 6 or 8 digit code, e.g. `621142` for garments.

India Compliance makes HSN mandatory on every sales item and Shopify has no HSN field. Without
this the very first product fails to save and nothing imports at all.

Items that already have an HSN keep theirs. This is only the fallback for items the app
creates — correct them in ERPNext afterwards.

### 2.6 Location Map

One row per Shopify location, pointing at an ERPNext warehouse. Without this, stock has nowhere
to go.

### 2.7 Tick Enabled, then choose what syncs

Saving with **Enabled** on registers the webhooks with Shopify. Then turn on what you want:

| Switch | Default | What it does |
|---|---|---|
| Sync Orders | **on** | Shopify orders become Sales Orders |
| Sync Invoices | off | Paid orders become Sales Invoices |
| Sync Delivery Notes | off | Fulfilled orders become Delivery Notes |
| Sync Refunds | off | Shopify refunds become Credit Notes |
| Sync Inventory | off | ERPNext stock → Shopify, live |
| Sync Items | off | Product changes flow both ways |
| Let ERPNext Overwrite Titles | off | ⚠️ see below |
| Sync Prices | off | ERPNext prices → Shopify |
| Publish New Items | off | Create Shopify products from ERPNext |
| Sync Fulfilments | off | Shipping in ERPNext marks Shopify fulfilled |
| Email Customer on Fulfilment | off | ⚠️ emails your real customers |

**About "Let ERPNext Overwrite Titles":** leave it off and Shopify keeps your storefront copy —
ERPNext can never touch the product title or description. Turn it on and *any* save of the Item
in ERPNext replaces both, including a save that only changed the weight. Only turn it on if your
catalogue is genuinely written in ERPNext.

---

## Part 3 — The server

### 3.1 A public URL

Shopify has to reach your site to deliver webhooks. In production that is your real domain. For
testing, ngrok:

```bash
ngrok http 8000
```

Two things people trip over:

* **The ngrok URL changes every restart.** The old webhooks then point at a dead address.
  Re-enable the store to re-register them.
* **ngrok rewrites the Host header**, so Frappe may resolve the wrong site. Start the server
  bound to the site explicitly:

  ```bash
  bench --site yoursite.com serve --port 8000
  ```

### 3.2 The scheduler and a worker must be running

```bash
bench --site yoursite.com enable-scheduler
```

Without the scheduler, retries never fire, the janitor never runs, and nothing reconciles
overnight. Without a worker, webhooks arrive, get logged, and sit there forever.

**On macOS**, workers die instantly from a fork() crash unless you set:

```bash
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES bench worker
```

### 3.3 One shop, one store record

Two Shopify Store records pointing at the same shop fight over webhook subscriptions.
Disabling or deleting either one unregisters the webhooks for the **whole shop**, and the other
silently stops receiving anything.

### 3.4 Count your workers

Each `bench worker` holds the code it loaded at startup. Several stale workers will run several
different versions of your code and a fix will appear not to work. If a change seems to have no
effect, check how many workers are running before debugging anything else.

---

## How it behaves day to day

**Nothing in ERPNext ever waits on Shopify.** Saving an item or submitting a stock entry writes
a queue row and returns. Workers drain that queue in the background. A slow or unreachable
Shopify cannot block a POS sale.

**Queue rows carry no values, only "this needs pushing".** Workers re-read the current state
when they drain, so a backlog converges on the truth instead of replaying stale numbers in the
wrong order.

**Stock sent to Shopify is `actual − reserved`.** Goods promised to an existing order are not
advertised again, which is what stops you overselling.

**Nothing Shopify does deletes anything in ERPNext.** Deleting a product in Shopify disables and
unlinks the ERPNext Item; your stock history and past orders stay intact.

**A document whose total disagrees with the Shopify order is refused, not saved.** You get a
clear error naming the difference rather than quietly wrong books.

---

## Quick check that it is working

1. Place a test order in Shopify
2. Within a few seconds a **Sales Order** appears in ERPNext
3. The total matches the Shopify order exactly
4. Each line carries its own GST rate and HSN code
5. Change stock in ERPNext — Shopify updates within seconds

If something does not arrive, look at **Shopify Event Log** in ERPNext. Every webhook is
recorded there with its status and, when it failed, the full error.

---

## What is not built, and what is not proven

Be aware before you rely on it.

**Not built:**

* **Buyer GSTIN for B2B** — Shopify does not collect it, so B2B invoices file as B2C
* **e-Invoice / IRN / e-Way Bill** — not handled
* **Shopify tax settings** cannot be configured from ERPNext. Shopify exposes no API for them;
  they must be set in the Shopify admin

**Built, but not proven at scale:**

* **Load and concurrency.** Everything has been exercised one operation at a time. The queue is
  built for concurrent workers and the claim uses `FOR UPDATE SKIP LOCKED`, but it has not been
  run under real parallel load.
* **A very large catalogue.** The bulk importer is written for 50,000 products and has been
  tested on a handful.
