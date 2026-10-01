#!/usr/bin/env python3
"""Vendor the Shopify Admin API schema for one API version.

    python3 scripts/refresh_graphql_schema.py 2026-01

Run it when the pinned API version changes. `test_graphql_queries.py` validates every
operation in `api/queries/` against the file this writes, so without it a bump to a version
whose schema is not vendored fails the suite rather than passing on a stale one.

The schema comes from `@shopify/dev-mcp` on npm, which is Shopify's own MCP server and ships
the published introspection result for each supported version. That is a plain public
package download: no store, no token, nothing specific to an installation. Shopify's other
route -- introspecting a shop with its Admin token -- would give the same answer and is
deliberately not used here, because a test fixture should not need anybody's credentials.

Stored as SDL rather than the introspection JSON: smaller, and legible in a diff when a
version bump changes something.
"""

from __future__ import annotations

import gzip
import io
import json
import pathlib
import sys
import tarfile
import urllib.request

PACKAGE = "@shopify/dev-mcp"
REGISTRY = "https://registry.npmjs.org"
SCHEMA_DIR = pathlib.Path(__file__).resolve().parent.parent / "shopify_integration" / "api" / "schema"


def tarball_url() -> str:
	with urllib.request.urlopen(f"{REGISTRY}/{PACKAGE.replace('/', '%2f')}", timeout=60) as response:
		meta = json.load(response)
	latest = meta["dist-tags"]["latest"]
	return meta["versions"][latest]["dist"]["tarball"]


def introspection(api_version: str) -> dict:
	url = tarball_url()
	print(f"downloading {url}")
	with urllib.request.urlopen(url, timeout=300) as response:
		payload = response.read()

	member = f"package/dist/data/admin_{api_version}.json.gz"
	with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
		try:
			handle = tar.extractfile(member)
		except KeyError:
			handle = None
		if handle is None:
			available = sorted(
				name.split("admin_")[1].removesuffix(".json.gz")
				for name in tar.getnames()
				if "/data/admin_" in name
			)
			raise SystemExit(f"{PACKAGE} has no schema for {api_version}. It ships: {', '.join(available)}")
		return json.loads(gzip.decompress(handle.read()))["data"]


def main(api_version: str) -> None:
	from graphql import build_client_schema, print_schema

	sdl = print_schema(build_client_schema(introspection(api_version)))

	SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
	target = SCHEMA_DIR / f"admin_{api_version}.graphql.gz"
	# mtime=0 so regenerating unchanged content produces a byte-identical file, and a refresh
	# that changed nothing shows up as no diff at all.
	with gzip.GzipFile(filename="", mode="wb", fileobj=target.open("wb"), compresslevel=9, mtime=0) as out:
		out.write(sdl.encode())

	print(f"wrote {target} ({target.stat().st_size:,} bytes, {len(sdl):,} chars of SDL)")


if __name__ == "__main__":
	if len(sys.argv) != 2:
		raise SystemExit(__doc__)
	main(sys.argv[1])
