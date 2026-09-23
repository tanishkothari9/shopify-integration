# Contributing

## Getting set up

```bash
bench get-app https://github.com/tanishkothari9/shopify-integration
```

```bash
bench --site yoursite.localhost install-app shopify_integration
```

`docs/prerequisites.md` covers what to configure in Shopify and ERPNext before anything works.

## Running the tests

Two suites, and both must pass.

The pure ones need no site:

```bash
pytest shopify_integration/tests -q --ignore=shopify_integration/tests/test_catalogue.py --ignore=shopify_integration/tests/test_handlers.py --ignore=shopify_integration/tests/test_integration.py --ignore=shopify_integration/tests/test_inventory.py --ignore=shopify_integration/tests/test_fulfillment.py --ignore=shopify_integration/tests/test_orders.py --ignore=shopify_integration/tests/test_prices.py --ignore=shopify_integration/tests/test_product_push.py --ignore=shopify_integration/tests/test_reconcile.py --ignore=shopify_integration/tests/test_refunds.py
```

The rest need a site:

```bash
bench --site yoursite.localhost run-tests --app shopify_integration
```

CI runs the suite **twice on the same site**. Handlers commit deliberately, so a test that does
not clean up after itself passes once and fails on every run after.

## Style

`ruff` decides. Tabs, double quotes, 110 columns — Frappe's house style.

```bash
ruff check . && ruff format --check .
```

## What a good change looks like

**A test that fails before your fix and passes after.** Not a test that merely exercises the new
code — one that would have caught the bug.

Several defects in this app's history hid behind tests that mocked away the very call that was
broken. If you find yourself patching the thing you are testing, stop and write a test that hits
the real database instead.

**A comment that says why, not what.** The code says what it does. Explain the thing that is not
obvious: the constraint, the race, the Shopify quirk, the wrong turn someone would otherwise take.

**Don't widen an API scope** to make something easier. If a feature seems to need
`write_customers` or `write_shipping`, say so in the issue and let it be discussed.

## Compatibility

Targets Frappe/ERPNext **v15**. CI also builds against Frappe `develop` as a non-blocking
early warning, so a coming release that breaks us shows up before it ships.

The Shopify API version is pinned per store (`api_version`, default `2026-01`) and every GraphQL
document lives in `shopify_integration/api/queries/`. Shopify ships a new version quarterly and
sunsets each after twelve months; when you move a store forward, diff those documents as part of
the upgrade.
