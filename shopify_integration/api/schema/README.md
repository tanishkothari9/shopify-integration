# Vendored Admin API schemas

One gzipped SDL per API version the app is pinned to, used by
`shopify_integration/tests/test_graphql_queries.py` to check every operation in
`../queries/` before it can reach a real shop.

These are not hand-written. Regenerate with:

    python3 scripts/refresh_graphql_schema.py 2026-01

The source is the published introspection result that ships inside Shopify's own
`@shopify/dev-mcp` npm package — a public download that needs no store and no token.

A version bump means a new file here. The test reads the `api_version` default from the
Shopify Store doctype and looks for the matching schema, so forgetting this step fails the
suite instead of quietly validating against last version's rules.
