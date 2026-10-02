"""Every image an ERPNext Item has, mirrored onto its Shopify product. One way only.

ERPNext is the source of truth for which photographs a product has. Staff keep the main one
in the Item's Image field and the rest as attachments, and before this only the Image field
was ever sent -- so the second, third and fourth photograph of a saree existed in ERPNext
and nowhere a customer could see them, and clearing the Image field left the old picture on
the storefront for good.

Nothing here reads Shopify's images back into ERPNext. What it does read is which media the
app itself put there, recorded per file on the link, so that photography the merchant
uploaded in the Shopify admin is never moved and never deleted. That record is the whole
basis of the feature: without it the only safe rule is "touch nothing", which is where this
started.
"""

from __future__ import annotations

import json
from urllib.parse import quote, urlparse

import frappe
from frappe.utils import cstr, get_url

from shopify_integration.api.client import ShopifyClient, load_query
from shopify_integration.exceptions import PartialFailure

#: What Shopify will take as a product image. Anything else attached to the Item -- a PDF
#: size chart, a supplier's invoice -- is not a photograph and is left alone.
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")

#: Shopify's own ceiling on media per product.
MAX_MEDIA_PER_PRODUCT = 250

#: Variants read per product. A product cannot have more than 100 variants either.
VARIANT_PAGE = 100


# --------------------------------------------------------------------------------------
# What ERPNext says the product should have
# --------------------------------------------------------------------------------------


def public_url(file_url: str, subject: str) -> str | None:
	"""An absolute, publicly fetchable URL for one file, or None with a reason logged.

	Shopify fetches the image itself rather than accepting an upload, so the URL has to be
	reachable from the internet. Three things make that fail, and each is worth saying out
	loud rather than failing silently: there is no file; the file is private, so Frappe
	serves it only to a logged-in session and Shopify would store the login page as the
	product photo; or the site is only on localhost and there is nothing to fetch.
	"""
	file_url = cstr(file_url).strip()
	if not file_url:
		return None

	if file_url.startswith(("http://", "https://")):
		return file_url

	if frappe.db.exists("File", {"file_url": file_url, "is_private": 1}):
		frappe.logger("shopify_integration").info(
			f"Not sending {subject}'s image to Shopify: {file_url} is a private file, and "
			f"Shopify has no session to fetch it with. Re-upload it unticked as private."
		)
		return None

	base = cstr(get_url()).rstrip("/")
	host = urlparse(base).hostname or ""
	if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost"):
		frappe.logger("shopify_integration").info(
			f"Not sending {subject}'s image to Shopify: this site is {base}, which Shopify "
			f"cannot reach. Set host_name in site_config to a public URL."
		)
		return None

	# The path is already URL-ish but filenames routinely carry spaces and commas.
	return base + quote(file_url, safe="/:@&=+$,-_.!~*'()")


def _is_image(file_url: str) -> bool:
	return cstr(file_url).split("?")[0].lower().endswith(IMAGE_EXTENSIONS)


def image_attachments(item_code: str) -> list[str]:
	"""Every image file attached to the Item, oldest first.

	Oldest first because that is the order someone added them in, and it is the only order
	the attachment list offers that does not change when a file is renamed.
	"""
	return [
		row.file_url
		for row in frappe.get_all(
			"File",
			filters={"attached_to_doctype": "Item", "attached_to_name": item_code},
			fields=["file_url", "is_private"],
			order_by="creation asc",
		)
		if row.file_url and _is_image(row.file_url)
	]


def desired_files(item_code: str) -> list[str]:
	"""The file_urls one Item should contribute, featured first.

	The Image field leads -- it is what ERPNext itself shows and what the merchant means by
	the product's photograph -- and the attachments follow. The same file counts once: the
	Image field is usually also an attachment, and uploading it twice would give the
	storefront a duplicate.
	"""
	ordered = []
	seen = set()
	for file_url in [cstr(frappe.db.get_value("Item", item_code, "image")), *image_attachments(item_code)]:
		file_url = cstr(file_url).strip()
		if file_url and file_url not in seen and _is_image(file_url):
			seen.add(file_url)
			ordered.append(file_url)
	return ordered


def product_subject(item_code: str) -> str:
	"""The Item the Shopify product belongs to: a variant's template, or the item itself."""
	return cstr(frappe.db.get_value("Item", item_code, "variant_of")) or item_code


def media_plan(subject: str) -> dict:
	"""What the product should hold, in order, and which variant owns which file.

	The template's own images come first, so the storefront leads with the photograph set on
	the template, and each variant's images follow in variant order. A variant's image has
	to be on the product regardless -- Shopify will only attach media to a variant once the
	product already has it.
	"""
	order: list[str] = []
	seen: set[str] = set()

	def add(file_urls):
		for file_url in file_urls:
			if file_url not in seen:
				seen.add(file_url)
				order.append(file_url)

	add(desired_files(subject))

	by_variant = {}
	for variant in frappe.get_all(
		"Item", filters={"variant_of": subject, "disabled": 0}, order_by="item_code asc", pluck="name"
	):
		files = desired_files(variant)
		if files:
			by_variant[variant] = files
			add(files)

	return {"order": order, "by_variant": by_variant}


# --------------------------------------------------------------------------------------
# What the app put there, recorded per file
# --------------------------------------------------------------------------------------


def owned_media(link_name: str) -> dict[str, str]:
	"""file_url -> Shopify media id, for media this app created."""
	raw = frappe.db.get_value("Shopify Item Link", link_name, "app_media")
	if not raw:
		return {}
	try:
		recorded = json.loads(raw)
	except (TypeError, ValueError):
		return {}
	return {cstr(k): cstr(v) for k, v in recorded.items() if k and v} if isinstance(recorded, dict) else {}


def _record_owned(link_name: str, owned: dict[str, str]) -> None:
	frappe.db.set_value(
		"Shopify Item Link",
		link_name,
		"app_media",
		json.dumps(owned, indent=0, sort_keys=True) if owned else None,
		update_modified=False,
	)


# --------------------------------------------------------------------------------------
# The sync
# --------------------------------------------------------------------------------------


def _product_media_nodes(client: ShopifyClient, product_gid: str) -> list[dict]:
	data = client.execute(load_query("product_media"), {"id": product_gid}, cost_hint=10)
	product = data.get("product") or {}
	return [node for node in ((product.get("media") or {}).get("nodes") or []) if node.get("id")]


def _variant_media(client: ShopifyClient, product_gid: str) -> dict[str, list[str]]:
	"""variant gid -> the media already attached to it.

	Its own call, and only made when there is a variant image to attach: asking for it
	alongside the product's media is a connection inside a connection, and Shopify charges
	the product of the two.
	"""
	attached = {}
	for node in client.paginate(
		load_query("product_variant_media"),
		{"id": product_gid},
		"product.variants",
		cost_hint=20,
	):
		if node.get("id"):
			attached[node["id"]] = [
				m["id"] for m in ((node.get("media") or {}).get("nodes") or []) if m.get("id")
			]
	return attached


def _create(client: ShopifyClient, product_gid: str, sources: list[tuple[str, str]], subject: str) -> dict:
	"""Upload images and return file_url -> media id for the ones Shopify accepted."""
	if not sources:
		return {}

	result = client.execute(
		load_query("product_create_media"),
		{
			"productId": product_gid,
			"media": [
				{"originalSource": url, "mediaContentType": "IMAGE", "alt": subject}
				for _file_url, url in sources
			],
		},
		cost_hint=10 + len(sources),
	)
	payload = result.get("productCreateMedia") or {}
	errors = payload.get("mediaUserErrors") or []
	if errors:
		raise MediaSyncError(f"{subject}: Shopify refused the images -- {_readable(errors)}")

	# Shopify returns them in the order they were sent.
	created = [node for node in (payload.get("media") or []) if node.get("id")]
	return {file_url: node["id"] for (file_url, _url), node in zip(sources, created, strict=False)}


def _readable(errors: list[dict]) -> str:
	return "; ".join(
		filter(
			None,
			(f"{cstr(e.get('code') or '').lower() or 'error'}: {cstr(e.get('message'))}" for e in errors),
		)
	)


class MediaSyncError(frappe.ValidationError):
	"""An image ERPNext holds did not end up on the product, and somebody should know."""


def sync_item_media(client: ShopifyClient, store_doc, item_code: str) -> dict:
	"""Make one Shopify product's images match what ERPNext holds.

	Uploads what is new, deletes the app's own media whose file ERPNext no longer has, puts
	the main image first, and attaches each variant's image to that variant. Media the
	merchant added in the Shopify admin is not in the app's record, so none of the three
	touch it.
	"""
	from shopify_integration.outbound.product import product_link_for

	if not store_doc.sync_item_images:
		return {"skipped": "image sync is off for this store"}

	subject = product_subject(item_code)
	link = product_link_for(store_doc.name, subject)
	if not link or not link.product_gid:
		return {"skipped": f"{subject} has no Shopify product"}

	plan = media_plan(subject)
	on_product = {node["id"]: node for node in _product_media_nodes(client, link.product_gid)}

	# Media the merchant deleted in Shopify is no longer ours to account for.
	owned = {url: gid for url, gid in owned_media(link.name).items() if gid in on_product}

	failures = []

	# Processing is asynchronous, so a FAILED image is only ever discovered on a later pass.
	# Taking it down and forgetting it is what lets a corrected file be uploaded again; the
	# reason is reported so a file Shopify will never accept does not just vanish quietly.
	failed = {url: gid for url, gid in owned.items() if cstr(on_product[gid].get("status")) == "FAILED"}
	for url, gid in failed.items():
		reason = _readable(on_product[gid].get("mediaErrors") or []) or "Shopify gave no reason"
		failures.append(f"{subject}: Shopify could not process {url} -- {reason}")
		owned.pop(url, None)

	# Merchant media counts against the ceiling too, so what is left is what the app may use.
	theirs = [gid for gid in on_product if gid not in owned.values()]
	room = max(MAX_MEDIA_PER_PRODUCT - len(theirs), 0)
	wanted = plan["order"][:room]
	if len(plan["order"]) > room:
		failures.append(
			f"{subject}: {len(plan['order']) - room} image(s) left off -- the product is at "
			f"Shopify's limit of {MAX_MEDIA_PER_PRODUCT} media, {len(theirs)} of them added in Shopify."
		)

	to_delete = [gid for url, gid in owned.items() if url not in wanted] + list(failed.values())
	if to_delete:
		result = client.execute(
			load_query("product_delete_media"),
			{"productId": link.product_gid, "mediaIds": to_delete},
			cost_hint=10,
		)
		payload = result.get("productDeleteMedia") or {}
		errors = payload.get("mediaUserErrors") or []
		if errors:
			failures.append(f"{subject}: Shopify refused to remove an image -- {_readable(errors)}")

		# Only what Shopify says it actually deleted. Forgetting a media id that is still on
		# the product is worse than leaving the record: the next sync sees media it has no
		# record of, reads it as the merchant's own, and will never delete it or count it
		# against the ceiling again.
		gone = set(payload.get("deletedMediaIds") or [])
		owned = {url: gid for url, gid in owned.items() if gid not in gone}

	sources = []
	for file_url in wanted:
		if file_url in owned or file_url in failed:
			# Not `failed`: Shopify has just told us it cannot process that exact file, and
			# sending it again in the same breath would only fail again. The record of it is
			# gone, so the next thing that touches the item tries once more -- which is what
			# makes a corrected file reach the shop without a permanently broken one being
			# re-uploaded on a loop.
			continue
		url = public_url(file_url, subject)
		if url:
			sources.append((file_url, url))

	created = _create(client, link.product_gid, sources, subject)
	owned.update(created)
	_record_owned(link.name, owned)

	_feature_the_main_image(client, link.product_gid, wanted, owned, on_product, created)
	attached = _attach_to_variants(client, store_doc.name, link.product_gid, plan, owned)

	return {
		"added": len(created),
		"removed": len(to_delete),
		"variants": attached,
		"failures": failures,
	}


def _feature_the_main_image(client, product_gid, wanted, owned, on_product, created) -> bool:
	"""Move ERPNext's main image to position 0, if it is not already there.

	Only that one move. The app's images are created in ERPNext's order, so they are already
	in it; reshuffling the rest would displace whatever arrangement the merchant made of
	their own photographs for the sake of nothing.
	"""
	if not wanted:
		return False
	featured = owned.get(wanted[0])
	if not featured:
		return False

	# A media id just created is at the end of the product's list, so it always needs moving.
	order = [gid for gid in on_product]
	if featured not in created.values() and order and order[0] == featured:
		return False

	result = client.execute(
		load_query("product_reorder_media"),
		{"id": product_gid, "moves": [{"id": featured, "newPosition": "0"}]},
		cost_hint=10,
	)
	errors = (result.get("productReorderMedia") or {}).get("mediaUserErrors") or []
	if errors:
		# Not fatal -- every image is on the product, one of them is in the wrong place --
		# but silence here is how the main photograph stays wrong for ever.
		frappe.logger("shopify_integration").warning(
			f"{product_gid}: could not feature the main image -- {_readable(errors)}"
		)
		return False
	return True


def _attach_to_variants(client, store, product_gid, plan, owned) -> int:
	"""Give each variant the media of its own images, where it does not have them yet."""
	if not plan["by_variant"]:
		return 0

	already = _variant_media(client, product_gid)
	variant_media = []
	for item_code, files in plan["by_variant"].items():
		variant_gid = frappe.db.get_value(
			"Shopify Item Link", {"store": store, "item_code": item_code}, "variant_gid"
		)
		if not variant_gid:
			continue
		wanted = [owned[f] for f in files if f in owned and owned[f] not in (already.get(variant_gid) or [])]
		if wanted:
			variant_media.append({"variantId": variant_gid, "mediaIds": wanted})

	if not variant_media:
		return 0

	result = client.execute(
		load_query("product_variant_append_media"),
		{"productId": product_gid, "variantMedia": variant_media},
		cost_hint=10 + len(variant_media),
	)
	errors = (result.get("productVariantAppendMedia") or {}).get("userErrors") or []
	if errors:
		raise MediaSyncError(f"attaching variant images failed -- {_readable(errors)}")
	return len(variant_media)


# --------------------------------------------------------------------------------------
# The queue
# --------------------------------------------------------------------------------------


def push_media(store: str, rows: list[dict]) -> None:
	"""Queue handler for the ``media`` operation.

	One product per row, because a product's media is read and written whole and batching
	two of them would save nothing.
	"""
	store_doc = frappe.get_cached_doc("Shopify Store", store)
	if not store_doc.sync_item_images:
		return

	client = ShopifyClient.for_store(store)

	# Per row, so one product whose images Shopify will not take cannot fail the images of
	# every other product claimed alongside it.
	failures: dict[str, Exception] = {}
	for row in rows:
		item_code = cstr(row.get("ref_docname"))
		if not item_code or not frappe.db.exists("Item", item_code):
			continue
		try:
			reported = sync_item_media(client, store_doc, item_code).get("failures") or []
			if reported:
				raise MediaSyncError("\n".join(reported))
		except Exception as exc:
			failures[row["name"]] = exc

	if failures:
		# Everything that did work is already written. Raising afterwards is what puts the
		# reason somewhere a person will see it -- on the queue row, in plain words.
		frappe.db.commit()
		raise PartialFailure(failures)


def enqueue_media(store: str, item_code: str) -> str | None:
	"""Queue one product's images. Keyed on the product's item, so a template with twelve
	variants is one row however many of them changed at once."""
	from shopify_integration.sync.engine import enqueue_sync

	store_doc = frappe.get_cached_doc("Shopify Store", store)
	if not store_doc.sync_item_images:
		return None

	subject = product_subject(item_code)
	return enqueue_sync(
		store,
		"media",
		dedupe_key=f"media:{store}:{subject}",
		ref_doctype="Item",
		ref_docname=subject,
	)


def enqueue_for_item(item_code: str) -> int:
	"""Queue every store that sells this item. One indexed read when none do, which is the
	common case on a catalogue where most items were never published."""
	queued = 0
	subject = product_subject(item_code)

	# A template has no link of its own -- it has no Shopify variant to point at -- so its
	# children carry `template_item` instead. Looking only at item_code finds nothing for a
	# template, and the images staff attach to the template are the product's lead
	# photographs, so they were the ones that never shipped.
	stores = set(
		frappe.get_all(
			"Shopify Item Link", filters={"item_code": ["in", [item_code, subject]]}, pluck="store"
		)
	) | set(frappe.get_all("Shopify Item Link", filters={"template_item": subject}, pluck="store"))

	for store in stores:
		if enqueue_media(store, item_code):
			queued += 1
	return queued


def on_item_change(doc, method=None) -> None:
	"""doc_event on Item. Only when the Image field actually moved.

	Every other save -- a weight, a description, a reorder level -- must cost nothing here.
	"""
	from shopify_integration.catalogue.echo import is_echo

	if is_echo(doc) or doc.get("__islocal"):
		return
	if not doc.has_value_changed("image"):
		return
	enqueue_for_item(doc.name)


def on_file_change(doc, method=None) -> None:
	"""doc_event on File, for attachments added to or removed from an Item."""
	if cstr(doc.get("attached_to_doctype")) != "Item":
		return
	item_code = cstr(doc.get("attached_to_name"))
	if not item_code or not _is_image(cstr(doc.get("file_url"))):
		return
	if not frappe.db.exists("Item", item_code):
		return
	enqueue_for_item(item_code)
