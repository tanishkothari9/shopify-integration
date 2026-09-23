# Security

## Reporting a vulnerability

Please do **not** open a public issue for a security problem.

Email the maintainer at the address in `pyproject.toml`, with:

* what the problem is and roughly how bad you think it is
* the steps to reproduce it
* the app version (`shopify_integration/__init__.py`) and your Frappe/ERPNext versions

You'll get an acknowledgement within a few days. If the report is valid, you'll be told when a
fix lands, and credited unless you'd rather not be.

## What this app holds

Two secrets per store, both in Password-type fields, which Frappe encrypts at rest with the
site's `encryption_key`:

| Field | What it is |
|---|---|
| `admin_api_token` | Shopify Admin API access token |
| `api_secret` | the app's client secret, used to verify webhook signatures |

**Anyone who can read the Shopify Store doctype can use those credentials**, because Frappe
lets a permitted user decrypt a Password field. Treat access to that doctype as equivalent to
handing out the Shopify token. Restrict it to System Manager.

The site's `encryption_key` lives in `site_config.json`. If that file leaks, the stored
credentials leak with it.

## The public endpoint

One route is reachable without logging in:

```
/api/method/shopify_integration.inbound.webhook.webhook
```

It is deliberately the dumbest code in the app. In order:

1. The body is capped at 5 MiB before anything reads it.
2. The shop domain header selects which store's secret to verify against — a single indexed
   read, no side effects.
3. HMAC-SHA256 of the **raw body** is compared against the header using `hmac.compare_digest`,
   so a wrong signature cannot be brute-forced a byte at a time.
4. Only then is anything written to the database.

An unknown or disabled shop gets `401`, not `404` — otherwise an anonymous caller could
enumerate which shops a site is connected to.

Replays are dropped: a webhook id already in the Event Log returns `200` without reprocessing.

## Scopes

The app asks Shopify for the narrowest set that covers its features, and a test asserts the
list does not grow by accident:

```
read_products      write_products
read_orders
read_inventory     write_inventory
read_locations
read_customers
write_merchant_managed_fulfillment_orders
```

It deliberately does **not** ask for `write_customers`, `write_shipping` or `write_draft_orders`.
An integration has no business deleting customers or rewriting a merchant's tax configuration.

## Things to get right when you deploy

* **Serve over HTTPS.** Webhook bodies and the token in flight are only as private as the
  transport.
* **Never commit `site_config.json`.** It holds the encryption key and the database password.
* **Restrict the Shopify Store doctype** to System Manager.
* **One shop, one Shopify Store record.** Two records for the same shop fight over webhook
  subscriptions, and disabling either unregisters the app's webhooks for the whole shop.

## Supported versions

Fixes land on `main` against the versions in the README. Only the latest release is supported.
