"""Every document in `api/queries/` is checked against the real Admin API schema.

On 2 October, between 02:02 and 02:06, every collection sync failed:

    Type mismatch on variable $product and argument product (ProductInput! / ProductUpdateInput)

`product_collections_update.graphql` declared `$product: ProductInput!`, which 2026-01 split
into `ProductCreateInput` and `ProductUpdateInput`. Shopify rejects that during validation,
before the mutation runs, so the operation had never once worked.

Nothing in the suite could have caught it. The tests answer with fake clients, and a fake
client never reads the query text -- a mutation Shopify refuses outright looks exactly like
one that works. Only the schema knows, so this brings the schema into the suite: the
vendored SDL in `api/schema/` is the published 2026-01 one, and `graphql.validate` is the
same check Shopify runs on the way in.
"""

from __future__ import annotations

import gzip
import json
import pathlib

from frappe.tests.utils import FrappeTestCase

from shopify_integration.api.client import QUERY_DIR

SCHEMA_DIR = pathlib.Path(__file__).resolve().parent.parent / "api" / "schema"


def pinned_api_version() -> str:
	"""The version a new store is created against -- the one the queries are written for."""
	doctype = (
		pathlib.Path(__file__).resolve().parent.parent
		/ "shopify_integration"
		/ "doctype"
		/ "shopify_store"
		/ "shopify_store.json"
	)
	fields = json.loads(doctype.read_text())["fields"]
	return next(f["default"] for f in fields if f["fieldname"] == "api_version")


def load_schema(api_version: str):
	from graphql import build_schema

	path = SCHEMA_DIR / f"admin_{api_version}.graphql.gz"
	sdl = gzip.decompress(path.read_bytes()).decode()
	# assume_valid: Shopify's own schema is not this suite's to re-verify, and skipping that
	# pass halves the second it takes to build.
	return build_schema(sdl, assume_valid=True)


class TestTheQueriesMatchTheSchema(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		try:
			import graphql  # noqa: F401
		except ImportError:
			raise ImportError(
				"graphql-core is needed to check the GraphQL documents against Shopify's "
				"schema. It is a dependency of this app: install it with "
				"`./env/bin/pip install graphql-core` from the bench directory."
			) from None

		cls.api_version = pinned_api_version()
		cls.schema = load_schema(cls.api_version)

	def test_the_pinned_version_has_a_vendored_schema(self):
		"""Bumping api_version without vendoring that version's schema would otherwise leave
		every query being checked against last version's rules -- which is the failure this
		whole file exists to prevent, one release later."""
		self.assertTrue(
			(SCHEMA_DIR / f"admin_{self.api_version}.graphql.gz").exists(),
			f"no vendored schema for {self.api_version}: run "
			f"`python3 scripts/refresh_graphql_schema.py {self.api_version}`",
		)

	def test_every_document_is_valid(self):
		from graphql import parse, validate

		documents = sorted(QUERY_DIR.glob("*.graphql"))
		self.assertGreater(len(documents), 25, "the queries directory should not be empty")

		problems = []
		for path in documents:
			try:
				document = parse(path.read_text())
			except Exception as exc:
				problems.append(f"{path.name}: will not parse -- {exc}")
				continue
			for error in validate(self.schema, document):
				problems.append(f"{path.name}: {error.message}")

		self.assertEqual(
			problems,
			[],
			"these would be rejected by Shopify before the operation runs:\n  " + "\n  ".join(problems),
		)

	def test_the_check_catches_the_fault_it_was_written_for(self):
		"""A validator that silently passes everything is worse than none at all. This is the
		October mutation exactly as it was, and it has to fail."""
		from graphql import parse, validate

		errors = validate(
			self.schema,
			parse("""
				mutation productCollections($product: ProductInput!) {
					productUpdate(product: $product) {
						product { id }
						userErrors { field message }
					}
				}
			"""),
		)
		self.assertTrue(errors, "the wrong input type must not validate")
		self.assertIn("ProductUpdateInput", errors[0].message)

	def test_the_check_catches_a_field_that_does_not_exist(self):
		from graphql import parse, validate

		errors = validate(self.schema, parse('query { product(id: "gid://shopify/Product/1") { nope } }'))
		self.assertTrue(errors, "a hallucinated field must not validate")

	def test_every_document_is_loadable_by_name(self):
		"""`load_query` takes a bare name. A file the code asks for by a name that does not
		match its filename is a FileNotFoundError at the worst moment."""
		from shopify_integration.api.client import load_query

		for path in sorted(QUERY_DIR.glob("*.graphql")):
			self.assertTrue(load_query(path.stem).strip(), f"{path.name} is empty")


class TestTheCollectionMutation(FrappeTestCase):
	"""The one that broke, specifically."""

	def test_it_declares_the_update_input(self):
		from shopify_integration.api.client import load_query

		source = load_query("product_collections_update")
		self.assertIn("$product: ProductUpdateInput!", source)
		self.assertNotIn(
			"$product: ProductInput!",
			source,
			"ProductInput is the legacy `input:` argument's type, not `product:`'s",
		)
