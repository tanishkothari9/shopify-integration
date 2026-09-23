"""JSONL reassembly and resumability (spec §11).

These are the tests that make a 50,000-product import trustworthy: the file is read line by
line, variants are reattached to their parents in one pass, and a crash resumes exactly where
it stopped rather than restarting or skipping.
"""

import json

import pytest

from shopify_integration.api import bulk

PRODUCT_1 = {"id": "gid://shopify/Product/1", "title": "Tee", "handle": "tee"}
VARIANT_1A = {
	"id": "gid://shopify/ProductVariant/11",
	"sku": "TEE-S",
	"__parentId": "gid://shopify/Product/1",
}
VARIANT_1B = {
	"id": "gid://shopify/ProductVariant/12",
	"sku": "TEE-M",
	"__parentId": "gid://shopify/Product/1",
}
PRODUCT_2 = {"id": "gid://shopify/Product/2", "title": "Mug", "handle": "mug"}
VARIANT_2A = {"id": "gid://shopify/ProductVariant/21", "sku": "MUG", "__parentId": "gid://shopify/Product/2"}


@pytest.fixture
def jsonl(tmp_path):
	def write(objects):
		path = tmp_path / "bulk.jsonl"
		path.write_text("\n".join(json.dumps(o) for o in objects) + "\n")
		return str(path)

	return write


def test_variants_are_reattached_to_their_product(jsonl):
	path = jsonl([PRODUCT_1, VARIANT_1A, VARIANT_1B, PRODUCT_2, VARIANT_2A])
	groups = [product for _, product in bulk.iter_product_groups(path)]

	assert [p["id"] for p in groups] == ["gid://shopify/Product/1", "gid://shopify/Product/2"]
	assert [v["sku"] for v in groups[0]["variants"]] == ["TEE-S", "TEE-M"]
	assert [v["sku"] for v in groups[1]["variants"]] == ["MUG"]


def test_last_product_is_flushed_even_with_no_successor(jsonl):
	"""The final group has nothing after it to trigger the flush -- a classic off-by-one."""
	path = jsonl([PRODUCT_1, VARIANT_1A])
	groups = [p for _, p in bulk.iter_product_groups(path)]
	assert len(groups) == 1
	assert len(groups[0]["variants"]) == 1


def test_line_counts_allow_exact_resumption(jsonl):
	"""lines_done must exclude the product being yielded, or a resume skips it."""
	path = jsonl([PRODUCT_1, VARIANT_1A, VARIANT_1B, PRODUCT_2, VARIANT_2A])
	counts = [lines_done for lines_done, _ in bulk.iter_product_groups(path)]
	assert counts == [3, 5]  # product 1 spans lines 0-2; the file has 5 lines total


def test_resuming_from_a_checkpoint_yields_only_the_remainder(jsonl):
	"""The crash-at-60,000 case: restart mid-file and get each product exactly once."""
	path = jsonl([PRODUCT_1, VARIANT_1A, VARIANT_1B, PRODUCT_2, VARIANT_2A])
	first_pass = list(bulk.iter_product_groups(path))
	checkpoint = first_pass[0][0]

	resumed = [p["id"] for _, p in bulk.iter_product_groups(path, skip=checkpoint)]
	assert resumed == ["gid://shopify/Product/2"]


def test_no_product_is_processed_twice_across_a_resume(jsonl):
	path = jsonl([PRODUCT_1, VARIANT_1A, PRODUCT_2, VARIANT_2A])
	done, seen = 0, []
	for lines_done, product in bulk.iter_product_groups(path, skip=done):
		seen.append(product["id"])
		done = lines_done
		break  # simulate a crash right after the first product

	for _, product in bulk.iter_product_groups(path, skip=done):
		seen.append(product["id"])

	assert seen == ["gid://shopify/Product/1", "gid://shopify/Product/2"]
	assert len(seen) == len(set(seen))


def test_orphan_variant_after_a_resume_is_dropped_not_misattached(jsonl):
	"""Resuming mid-product would otherwise glue leftover variants onto the next product."""
	path = jsonl([PRODUCT_1, VARIANT_1A, VARIANT_1B, PRODUCT_2, VARIANT_2A])
	groups = [p for _, p in bulk.iter_product_groups(path, skip=2)]  # start on VARIANT_1B

	assert [p["id"] for p in groups] == ["gid://shopify/Product/2"]
	assert [v["sku"] for v in groups[0]["variants"]] == ["MUG"]


def test_malformed_line_is_skipped_not_fatal(jsonl, tmp_path):
	"""One bad line must not abandon an otherwise good 50,000-product import."""
	path = tmp_path / "broken.jsonl"
	path.write_text(json.dumps(PRODUCT_1) + "\n" + "{not json\n" + json.dumps(VARIANT_1A) + "\n")
	groups = [p for _, p in bulk.iter_product_groups(str(path))]
	assert len(groups) == 1
	assert [v["sku"] for v in groups[0]["variants"]] == ["TEE-S"]


def test_blank_lines_are_ignored(jsonl, tmp_path):
	path = tmp_path / "blanks.jsonl"
	path.write_text(json.dumps(PRODUCT_1) + "\n\n\n" + json.dumps(VARIANT_1A) + "\n")
	groups = [p for _, p in bulk.iter_product_groups(str(path))]
	assert len(groups) == 1


def test_empty_file_yields_nothing(tmp_path):
	path = tmp_path / "empty.jsonl"
	path.write_text("")
	assert list(bulk.iter_product_groups(str(path))) == []


def test_non_variant_children_are_not_treated_as_variants(jsonl):
	"""Media and other nested connections also carry __parentId; only variants belong in
	the variants list."""
	media = {"id": "gid://shopify/MediaImage/99", "__parentId": "gid://shopify/Product/1"}
	path = jsonl([PRODUCT_1, VARIANT_1A, media])
	groups = [p for _, p in bulk.iter_product_groups(path)]
	assert [v["sku"] for v in groups[0]["variants"]] == ["TEE-S"]
