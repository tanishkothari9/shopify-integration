"""Website content, from ERPNext to Shopify.

ERPNext is the master. The Website (Shopify) section on the Item holds what the storefront
shows -- title, description, SEO, handle, tags, category, metafields, collections and image
alt text -- and this module turns it into what Shopify's API wants. Where a field is left
empty the Item's own name or description stands in, so an Item nobody has written copy for
still publishes sensibly.

Everything here is pure: it reads documents and returns payloads. The calls themselves are
made by `outbound.product`, from the outbox, so a save never waits on the network.

Content lives on the **template** of a product with sizes and is sent once for the product.
A variant has no storefront page of its own; sending per-variant titles would rename the
whole range after whichever size was saved last, which is a fault this app has already had.
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import cstr

from shopify_integration.exceptions import ShopifyUserError

#: Shopify's own limits. Exceeding them is rejected at the API, so they are checked here
#: where the message can name the Item and the field.
MAX_TITLE = 255
MAX_SEO_TITLE = 70
MAX_SEO_DESCRIPTION = 320

#: Shopify's handle grammar: lowercase alphanumerics in hyphen-separated runs.
HANDLE_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: The namespace Shopify's own category attributes live in. Reserved: the merchant's own
#: fields go in `custom`, and writing to `shopify` outside a category attribute is refused
#: by the API.
TAXONOMY_NAMESPACE = "shopify"


class ContentError(frappe.ValidationError):
	"""Website content ERPNext holds that Shopify will not accept."""


def subject_of(item_code: str) -> str:
	"""The Item whose website content describes this product -- the template, if any."""
	return cstr(frappe.db.get_value("Item", item_code, "variant_of")) or item_code


def _first(*values) -> str:
	for value in values:
		text = cstr(value).strip()
		if text:
			return text
	return ""


def tags_of(item) -> list[str]:
	"""The tag list, from a comma-separated field, trimmed and de-duplicated in order."""
	seen, tags = set(), []
	for tag in cstr(item.get("shopify_tags")).split(","):
		tag = tag.strip()
		if tag and tag.lower() not in seen:
			seen.add(tag.lower())
			tags.append(tag)
	return tags


def validate_content(item) -> None:
	"""Refuse content Shopify would reject, naming the Item and the field.

	Checked before anything is sent, and raised rather than trimmed: silently cutting a
	merchant's SEO title in half is worse than telling them it is too long.
	"""
	problems = []

	title = cstr(item.get("shopify_title")).strip()
	if len(title) > MAX_TITLE:
		problems.append(_("Website Title is {0} characters; Shopify allows {1}.").format(len(title), MAX_TITLE))

	seo_title = cstr(item.get("shopify_seo_title")).strip()
	if len(seo_title) > MAX_SEO_TITLE:
		problems.append(
			_("SEO Title is {0} characters; Shopify allows {1}.").format(len(seo_title), MAX_SEO_TITLE)
		)

	seo_description = cstr(item.get("shopify_seo_description")).strip()
	if len(seo_description) > MAX_SEO_DESCRIPTION:
		problems.append(
			_("SEO Description is {0} characters; Shopify allows {1}.").format(
				len(seo_description), MAX_SEO_DESCRIPTION
			)
		)

	handle = cstr(item.get("shopify_handle")).strip()
	if handle and not HANDLE_PATTERN.match(handle):
		problems.append(
			_(
				"URL Handle {0!r} is not a handle. Use lowercase letters, numbers and single "
				"hyphens between them -- 'banarasi-silk-saree'."
			).format(handle)
		)

	for row in item.get("shopify_metafields") or []:
		namespace = cstr(row.namespace).strip()
		key = cstr(row.key).strip()
		if not namespace or not key or not cstr(row.type).strip():
			problems.append(_("A metafield row is missing its namespace, key or type."))
		elif namespace == TAXONOMY_NAMESPACE and not cstr(item.get("shopify_product_category")).strip():
			problems.append(
				_(
					"Metafield {0}.{1} is a category attribute, but no Product Category is set "
					"on this item. Shopify will not accept it."
				).format(namespace, key)
			)

	if problems:
		frappe.throw(
			_("Website content on {0} cannot be sent to Shopify:\n\n{1}").format(
				item.name, "\n".join(f"- {problem}" for problem in problems)
			),
			ContentError,
		)


def website_payload(store_doc, item, *, creating: bool) -> dict:
	"""The storefront half of a productCreate or productUpdate payload.

	`creating` decides how the handle is sent. On create it is simply the handle. On update
	it comes with `redirectNewHandle`, so if the merchant has changed it Shopify leaves a
	redirect behind and the links already out in the world keep working. Sending an
	unchanged handle is a no-op, so this needs no round trip to find out.
	"""
	if not store_doc.get("sync_website_content"):
		return {}

	validate_content(item)
	payload: dict = {
		"title": _first(item.get("shopify_title"), item.get("item_name"), item.name)[:MAX_TITLE],
		"descriptionHtml": _first(item.get("shopify_description"), item.get("description")),
	}

	seo = {}
	if cstr(item.get("shopify_seo_title")).strip():
		seo["title"] = cstr(item.get("shopify_seo_title")).strip()
	if cstr(item.get("shopify_seo_description")).strip():
		seo["description"] = cstr(item.get("shopify_seo_description")).strip()
	if seo:
		payload["seo"] = seo

	tags = tags_of(item)
	if tags:
		payload["tags"] = tags

	category = cstr(item.get("shopify_product_category")).strip()
	if category:
		payload["category"] = category

	handle = cstr(item.get("shopify_handle")).strip()
	if handle:
		payload["handle"] = handle
		if not creating:
			payload["redirectNewHandle"] = True

	return payload


def desired_metafields(item) -> list[dict]:
	"""The metafield rows as `metafieldsSet` wants them, minus the owner."""
	wanted = []
	for row in item.get("shopify_metafields") or []:
		namespace, key = cstr(row.namespace).strip(), cstr(row.key).strip()
		value = cstr(row.value).strip()
		if not namespace or not key or not value:
			continue
		wanted.append(
			{"namespace": namespace, "key": key, "type": cstr(row.type).strip(), "value": value}
		)
	return wanted


def desired_image_alts(item) -> dict[str, str]:
	"""file_url -> alt text, for the images the merchant has written alt text for."""
	return {
		cstr(row.file_url).strip(): cstr(row.alt_text).strip()
		for row in (item.get("shopify_image_alts") or [])
		if cstr(row.file_url).strip() and cstr(row.alt_text).strip()
	}


def desired_image_order(item) -> list[str]:
	"""The file_urls the merchant has given a position, lowest first."""
	rows = [row for row in (item.get("shopify_image_alts") or []) if cstr(row.file_url).strip()]
	rows = [row for row in rows if row.get("position")]
	rows.sort(key=lambda row: (row.position, row.idx))
	return [cstr(row.file_url).strip() for row in rows]


# --------------------------------------------------------------------------------------
# Collections
# --------------------------------------------------------------------------------------


def app_collections(link_name: str) -> list[str]:
	"""The manual collections this app put the product in."""
	import json

	raw = frappe.db.get_value("Shopify Item Link", link_name, "app_collections")
	if not raw:
		return []
	try:
		recorded = json.loads(raw)
	except (TypeError, ValueError):
		return []
	return [cstr(gid) for gid in recorded if cstr(gid)] if isinstance(recorded, list) else []


def record_app_collections(link_name: str, gids: list[str]) -> None:
	import json

	frappe.db.set_value(
		"Shopify Item Link",
		link_name,
		"app_collections",
		json.dumps(sorted(set(gids))) if gids else None,
		update_modified=False,
	)


def collection_plan(store_doc, item, link_name: str, describe) -> dict:
	"""What to join, what to leave, and what could not be done.

	`describe` is called with the collection GIDs and returns `{gid: {title, automatic}}`.
	It is passed in rather than fetched here so this stays testable without a client, and
	so the caller can skip the call entirely when nothing has changed.

	Three rules, in order of how much damage breaking them would do:

	* **A tax collection is never touched.** The GST 5% and GST 3% collections carry price
	  overrides, and membership of them is decided by what ERPNext would actually book for
	  the item -- `outbound.collections` owns that, from the item's tax template. Letting a
	  hand-typed row move a product between tax bands would charge the customer the wrong
	  tax at checkout. Listed ones are refused by name.
	* **An automatic collection is skipped.** Shopify decides its membership from its own
	  rules and refuses to be joined; saying so is more use than an API error.
	* **Only the app's own additions are removed.** A collection the merchant added by hand
	  in the Shopify admin is theirs. The record on the link is what tells them apart.
	"""
	from shopify_integration.outbound.collections import tax_collection_map

	problems: list[str] = []
	reserved = {gid for gid in tax_collection_map(store_doc).values() if gid}

	wanted: list[str] = []
	for row in item.get("shopify_collections") or []:
		gid = cstr(row.collection_gid).strip()
		if not gid:
			continue
		if gid in reserved:
			problems.append(
				f"{item.name}: {cstr(row.collection_title) or gid} is one of this store's GST tax "
				"collections. Membership of those follows the item's tax template and is not "
				"set by hand -- remove the row."
			)
			continue
		if gid not in wanted:
			wanted.append(gid)

	previously = app_collections(link_name)
	known = describe(sorted(set(wanted) | set(previously))) if (wanted or previously) else {}

	joinable = []
	for gid in wanted:
		detail = known.get(gid)
		if detail is None:
			problems.append(f"{item.name}: collection {gid} does not exist on this store.")
			continue
		if detail.get("automatic"):
			problems.append(
				f"{item.name}: {detail.get('title') or gid} is an automatic collection and "
				"decides its own membership; it cannot be joined."
			)
			continue
		joinable.append(gid)

	# Only ever out of what we put in. Anything else on the product is the merchant's.
	to_leave = [gid for gid in previously if gid not in joinable]
	to_join = [gid for gid in joinable if gid not in previously]

	return {
		"join": to_join,
		"leave": to_leave,
		"owned": joinable,
		"problems": problems,
	}


# --------------------------------------------------------------------------------------
# Shopify's category attributes
# --------------------------------------------------------------------------------------


def _attribute_key(name: str) -> str:
	"""Shopify's metafield key for a category attribute, from its display name.

	"Embellishment technique" is stored under `shopify.embellishment-technique`. Lowercased,
	non-alphanumerics collapsed to single hyphens, which is the same shape as a handle.
	"""
	return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", cstr(name).strip().lower())).strip("-")


def category_attributes(client, category_gid: str) -> dict[str, dict]:
	"""key -> {id, name, kind} for the attributes this taxonomy category defines."""
	from shopify_integration.api.client import load_query

	data = client.execute(
		load_query("taxonomy_category_attributes"), {"id": category_gid}, cost_hint=10
	)
	node = data.get("node") or {}
	if not node.get("id"):
		return {}

	found = {}
	for attribute in ((node.get("attributes") or {}).get("nodes") or []):
		name = cstr(attribute.get("name")).strip()
		if not name:
			# The bare `TaxonomyAttribute` in the union carries only an id; there is nothing
			# to match a metafield key against, so it cannot be validated either way.
			continue
		found[_attribute_key(name)] = {"id": attribute.get("id"), "name": name}
	return found


def attribute_values(client, attribute_gid: str) -> dict[str, str]:
	"""value GID -> name, for one choice-list attribute, every page of it."""
	from shopify_integration.api.client import load_query

	values = {}
	for node in client.paginate(
		load_query("taxonomy_attribute_values"),
		{"id": attribute_gid},
		"node.values",
		cost_hint=10,
	):
		if node.get("id"):
			values[cstr(node["id"])] = cstr(node.get("name"))
	return values


def _referenced_values(value: str) -> list[str]:
	"""The taxonomy value GIDs a metafield value names, single or list."""
	import json

	text = cstr(value).strip()
	if text.startswith("["):
		try:
			parsed = json.loads(text)
		except (TypeError, ValueError):
			return []
		return [cstr(entry) for entry in parsed if cstr(entry)]
	return [text] if text else []


def validate_category_metafields(client, item) -> list[str]:
	"""Check every `shopify.*` metafield against what the category actually allows.

	Reported rather than sent. Shopify refuses the whole `metafieldsSet` call for one bad
	taxonomy reference, so an unchecked typo in one attribute would silently cost the
	product all of its metafields -- and the merchant would see a generic API error naming
	none of them.

	Only choice-list attributes can be checked this way; a measurement carries a number and
	a unit, and Shopify validates those itself.
	"""
	category = cstr(item.get("shopify_product_category")).strip()
	rows = [
		row
		for row in (item.get("shopify_metafields") or [])
		if cstr(row.namespace).strip() == TAXONOMY_NAMESPACE
	]
	if not rows:
		return []
	if not category:
		return [
			f"{item.name}: {len(rows)} category attribute(s) are set but no Product Category is."
		]

	defined = category_attributes(client, category)
	if not defined:
		return [f"{item.name}: {category} is not a category Shopify recognises."]

	problems: list[str] = []
	cache: dict[str, dict[str, str]] = {}
	for row in rows:
		key = cstr(row.key).strip()
		attribute = defined.get(key)
		if not attribute:
			problems.append(
				f"{item.name}: {category} has no attribute {key!r}. It allows: "
				f"{', '.join(sorted(defined)) or 'none'}."
			)
			continue

		referenced = _referenced_values(row.value)
		if not referenced:
			continue
		if attribute["id"] not in cache:
			cache[attribute["id"]] = attribute_values(client, attribute["id"])
		allowed = cache[attribute["id"]]
		if not allowed:
			# A measurement, or an attribute with no list. Shopify checks those itself.
			continue
		for gid in referenced:
			if gid not in allowed:
				problems.append(
					f"{item.name}: {gid} is not a value of {attribute['name']}. "
					f"Allowed: {', '.join(sorted(allowed.values())[:8])}"
					+ (" ..." if len(allowed) > 8 else "")
				)
	return problems


# --------------------------------------------------------------------------------------
# The store's own metafield definitions
# --------------------------------------------------------------------------------------


#: The product metafields this shop keeps its own details in. Created on the store so they
#: show up as proper fields in the Shopify admin rather than loose key/value pairs, and so
#: the storefront can read and filter on them.
CUSTOM_DEFINITIONS = [
	{"key": "saree_length", "name": "Saree length", "type": "dimension"},
	{"key": "blouse_piece", "name": "Blouse piece", "type": "single_line_text_field"},
	{"key": "care_instructions", "name": "Care instructions", "type": "multi_line_text_field"},
	{"key": "country_of_origin", "name": "Country of origin", "type": "single_line_text_field"},
	{"key": "manufacturer", "name": "Manufacturer", "type": "multi_line_text_field",
	 "description": "Name and address, as the Legal Metrology rules require on the listing."},
	{"key": "net_quantity", "name": "Net quantity", "type": "single_line_text_field"},
	{"key": "occasion", "name": "Occasion", "type": "list.single_line_text_field"},
	{"key": "work", "name": "Work", "type": "list.single_line_text_field"},
]

#: How Shopify says a definition is already there. The code is the reliable half; the
#: message is matched too because a code is not always set on this one.
ALREADY_DEFINED = "TAKEN"
ALREADY_DEFINED_TEXT = "key is in use"


def _is_taken(error: dict) -> bool:
	return (
		cstr(error.get("code")).upper() == ALREADY_DEFINED
		or ALREADY_DEFINED_TEXT in cstr(error.get("message")).lower()
	)


def _already_defined(exc) -> bool:
	"""Whether this refusal is just Shopify saying the definition is already there."""
	for error in getattr(exc, "user_errors", None) or []:
		if isinstance(error, dict) and _is_taken(error):
			return True
	return ALREADY_DEFINED_TEXT in str(exc).lower()


def ensure_metafield_definitions(client) -> dict:
	"""Create this shop's product metafield definitions, idempotently.

	Needs `write_products`, which the app already asks for: a product-owned metafield
	definition is part of the product resource and carries no scope of its own. Nothing
	here asks for anything wider.

	Storefront visibility and filtering are requested where Shopify allows them. A
	definition it will not make filterable is still created -- the field is the point, and
	the filter is a bonus.
	"""
	from shopify_integration.api.client import load_query

	created, existed, failed = [], [], []
	for definition in CUSTOM_DEFINITIONS:
		payload = {
			"namespace": "custom",
			"key": definition["key"],
			"name": definition["name"],
			"description": definition.get("description"),
			"type": definition["type"],
			"ownerType": "PRODUCT",
			"access": {"storefront": "PUBLIC_READ"},
			"capabilities": {"adminFilterable": {"enabled": True}},
		}
		try:
			result = client.execute(
				load_query("metafield_definition_create"), {"definition": payload}, cost_hint=10
			)
		except ShopifyUserError as exc:
			# The client raises on `userErrors` before the caller sees them, so the one
			# error that means "nothing to do" has to be recognised here. Without this,
			# pressing the button a second time failed the whole run on the first
			# definition that already existed.
			if _already_defined(exc):
				existed.append(definition["key"])
				continue
			failed.append(f"custom.{definition['key']}: {exc}")
			continue

		body = result.get("metafieldDefinitionCreate") or {}
		if body.get("createdDefinition"):
			created.append(definition["key"])
		else:
			errors = body.get("userErrors") or []
			if any(_is_taken(error) for error in errors):
				existed.append(definition["key"])
			else:
				failed.append(
					f"custom.{definition['key']}: "
					+ "; ".join(cstr(error.get("message")) for error in errors)
				)

	return {"created": created, "already_there": existed, "failed": failed}
