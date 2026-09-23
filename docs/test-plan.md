# Test plan — what we are about to do

Everything is on **fresh.localhost** (`http://localhost:8080`) against your real Shopify store
`your-store.myshopify.com`.

Both sides start empty: 0 products, 0 orders, 0 items, 0 customers in ERPNext.

Each phase says **what I do**, **what you should see**, and **exactly where to look**. Nothing
moves to the next phase until you are happy with the one before it.

---

## Phase 0 — Settings (before anything else)

Three things are not ready yet. I fix these first.

| Setting | Now | Will be | Why |
|---|---|---|---|
| Location Map | **empty** | Shop location → `Stores - VCJ` | Without it stock has nowhere to go and inventory sync does nothing |
| Selling Price List | `Wholesale Test` | `Shopify - Smart Choice` | Leftover from another site |
| Item Groups | Saree only | + Kurti, Indo-Western, Lehenga, Jewellery | So product type maps cleanly both ways |

Then the switches. Off by default, turned on deliberately:

| Switch | Set to | Why |
|---|---|---|
| Sync Orders | ON | Orders become Sales Orders |
| Sync Invoices | ON | Paid orders become Sales Invoices |
| Sync Inventory | ON | ERPNext stock → Shopify, live |
| Sync Items | ON | Product changes flow |
| Sync Prices | ON | ERPNext prices → Shopify |
| Sync Delivery Notes | ON | Shopify fulfilment → Delivery Note |
| Sync Fulfilments | ON | Delivery Note → Shopify fulfilled |
| Sync Refunds | ON | Shopify refund → Credit Note |
| Publish New Items | ON | ERPNext can create Shopify products |
| **Email Customer on Fulfilment** | **OFF** | Would email real addresses |
| **Let ERPNext Overwrite Titles** | **OFF** | Would replace your storefront copy |

**You verify:** open `http://localhost:8080/app/shopify-store/Smart Choice` and read the
Location Map, price list and the switch list against this table.

---

## Phase 1 — Items

### 1a. Products created in Shopify, flowing down to ERPNext

Five products, matching your real catalogue and its GST slabs:

| Product | Type | SKU | Price | GST | HSN |
|---|---|---|---|---|---|
| Banarasi Silk Saree | Saree | `SAREE-001` | ₹4,500 | 5% | 500720 |
| Chanderi Cotton Saree | Saree | `SAREE-002` | ₹2,800 | 5% | 520852 |
| Printed Rayon Kurti | Kurti | `KURTI-001` | ₹1,200 | 18% | 621142 |
| Bridal Lehenga Set | Lehenga | `LEHENGA-001` | ₹18,000 | 18% | 620442 |
| Kundan Necklace Set | Jewellery | `JEWEL-001` | ₹800 | 3% | 711790 |

**You verify:** `http://localhost:8080/app/item` — five new Items, each with the right HSN code,
stock UOM and a Shopify Item Link. Open one and check the HSN field.

### 1b. A product created in ERPNext, flowing up to Shopify

One item made in ERPNext with **Publish to Shopify** ticked:

| Item | Group | Price | GST |
|---|---|---|---|
| `INDOWEST-001` Indo-Western Draped Gown | Indo-Western | ₹6,500 | 18% |

**You verify:** it appears in your Shopify admin under Products, with the SKU, price and
inventory tracking on.

### 1c. A product with variants

One template with real variants, to prove options map correctly:

```
KURTA-SET   Chikankari Kurta     Size: S, M, L    Colour: White, Blue
```

**You verify:** one Shopify product with two options and six variants, each with its own SKU;
six variant Items in ERPNext under one template.

---

## Phase 2 — Customers and addresses

Customers are **not** created directly — they arrive with orders, which is the real path.

Each test order carries a different Indian address:

| Order | Ships to | Expected GST |
|---|---|---|
| A | Mumbai, **Maharashtra** | CGST + SGST (your own state) |
| B | Bengaluru, **Karnataka** | IGST (interstate) |

**You verify:** `http://localhost:8080/app/customer` — customers created with a Shopify ID.
Open one → Addresses → **both** a Billing and a Shipping address, with street, city, state
and PIN filled in.

---

## Phase 3 — Orders

Four orders, covering the combinations that actually differ:

| # | Items | State | Tax style | Expected |
|---|---|---|---|---|
| 1 | Saree ×2, Kurti ×1, Jewellery ×3 | Maharashtra | on top | CGST+SGST, 5/18/3% per line |
| 2 | same | Karnataka | on top | IGST only, 5/18/3% per line |
| 3 | same | Maharashtra | **included (MRP)** | total = MRP, GST taken out |
| 4 | Lehenga ×1 | Maharashtra | included | single high-value line |

**You verify:** `http://localhost:8080/app/sales-order`

For each one, open it and check:
- **Grand Total matches the Shopify order exactly** — this is the single most important number
- Each line has its own **Item Tax Template** (5%, 18%, 3% — not one blended rate)
- Each line carries its **HSN code**
- **Place of Supply** = `27-Maharashtra` or `29-Karnataka`
- Tax rows: CGST+SGST for Maharashtra, IGST for Karnataka

On the inclusive ones (3 and 4), also check the line **Rate is the shelf price** (₹4,500, not
₹4,285.71) and the tax rows show **Is this Tax included in Basic Rate = ticked**.

---

## Phase 4 — Invoices and payment

Each order gets marked **Paid** in Shopify.

**You verify:** `http://localhost:8080/app/sales-invoice`
- One invoice per order, **Submitted**, status **Paid**, Outstanding **0.00**
- Same total as the Sales Order
- A Payment Entry at `http://localhost:8080/app/payment-entry`

---

## Phase 5 — Inventory sync (the one you care about most)

Tested in both directions, with timings.

**ERPNext → Shopify.** I move stock four ways and time each one:

| Movement | What it represents |
|---|---|
| Material Receipt +30 | goods arriving |
| Material Issue −12 | goods leaving |
| Material Receipt +5 | a small top-up |
| Material Issue −3 | a small pick |

**You verify:** watch a product's inventory in the Shopify admin while I do this. It should
move within seconds. I will report the exact latency for each.

**Shopify → ERPNext.** An order placed in Shopify reduces what is available, and the
reservation shows up in ERPNext.

**The formula:** what Shopify advertises is `actual stock − reserved for orders`. So goods
already promised to an order are not offered twice. I will show you the numbers side by side.

---

## Phase 6 — Delivery Notes

Both directions again.

**ERPNext ships it:** I raise a Delivery Note from the Sales Order, exactly the way your
warehouse would — no Shopify fields typed by hand.

**You verify:** the Shopify order flips to **Fulfilled**. Check at
Shopify admin → Orders → the order.

**Shopify ships it:** I mark an order fulfilled inside Shopify.

**You verify:** `http://localhost:8080/app/delivery-note` — a Delivery Note appears,
**Submitted**, with the right quantity and linked to the Sales Order.

I will also prove it does **not** loop: the Delivery Note Shopify created must not turn round
and tell Shopify it shipped again.

---

## Phase 7 — Shipments and tracking

After the Delivery Note, I raise an ERPNext **Shipment** with a carrier and AWB number:

```
Carrier      Blue Dart
AWB          BD########
Tracking URL https://www.bluedart.com/tracking/BD########
```

**You verify:** Shopify admin → the order → the fulfilment now shows the tracking number and
carrier. Same AWB as the ERPNext Shipment at `http://localhost:8080/app/shipment`.

---

## Phase 8 — Refunds

One order refunded in Shopify.

**You verify:** a **Credit Note** (return Sales Invoice) in ERPNext for the right amount, with
the GST reversed proportionally — not a flat total.

---

## Phase 9 — GST returns

The final proof that the books are right.

**You verify:** `http://localhost:8080/app/query-report/GSTR-1` — set your company and the
date, choose **B2C (Small)**.

You should see the taxable value split by rate:

```
27-Maharashtra    5%   →  sarees
27-Maharashtra   18%   →  kurtis / lehengas
27-Maharashtra    3%   →  jewellery
29-Karnataka     ...   →  the interstate order
```

If a rate is missing or a value looks blended, something is wrong. That is the single best
check that per-line GST actually worked.

---

## What I will report at each phase

- The exact numbers on both sides, side by side
- Timings for anything that syncs
- Any failure, in full, without smoothing it over

## What will not be tested

- **Emails to customers** — left off deliberately; your customers are real addresses
- **e-Invoice / IRN / e-Way Bill** — not built
- **B2B GSTIN on the buyer** — Shopify does not collect it

## One thing to clear first

Your Shopify store still has **36 customers** from earlier testing. The app cannot delete them
(it only holds `read_customers`). If a test order uses an email that already exists, it will
attach to that customer instead of making a new one. Clearing them at
Shopify admin → Customers gives a cleaner run, but is not essential.
