"""Outbound product sync: ERPNext Item -> Shopify (spec §9.1).

Two jobs. It updates products that are already linked, and it creates Shopify products for
items ticked **Publish to Shopify** on a store set to **Publish New Items**. Both switches
have to be on: an ERPNext catalogue is mostly raw materials, packaging and internal parts,
and a single careless default would put all of it in a live shop.

Creation handles both shapes. A plain item becomes a product with one variant. A template
item -- `has_variants`, with an attribute table -- becomes a product whose options are built
from the attributes its variants actually use, and one Shopify variant per ERPNext variant.

The options come from the variants that exist, never from the Item Attribute's full value
list: an attribute may hold every size a business has ever stocked, and a shop offering two
of them should show two.

The hook here is the other half of echo suppression: it is what would loop forever if the
inbound writer did not mark its saves.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr

from shopify_integration.api.client import ShopifyClient, load_query
from shopify_integration.catalogue.echo import is_echo
from shopify_integration.exceptions import PartialFailure
from shopify_integration.outbound.content import (
	ContentError,
	desired_metafields,
	record_app_collections,
	validate_category_metafields,
	website_payload,
)
from shopify_integration.outbound.media import enqueue_media
from shopify_integration.shopify_integration.doctype.shopify_item_link.shopify_item_link import (
	linked_stores,
)
from shopify_integration.sync.engine import enqueue_sync

#: Variants per productVariantsBulkCreate call. Shopify caps a product at 100 variants, and
#: smaller batches keep one rejected value from costing the whole product.
VARIANT_BATCH = 25


def on_item_change(doc, method=None):
	"""doc_event on Item. Computes a dedupe key and enqueues -- nothing else.

	Runs inside the user's save transaction, so it must stay cheap and must never touch the
	network (spec §7.1).
	"""
	if is_echo(doc):
		# Written by an inbound handler. Pushing it back is the infinite loop.
		return

	if doc.get("has_variants") and not doc.get("publish_to_shopify"):
		# A template is not sellable and has no Shopify variant of its own, so there is nothing
		# to update. It does reach the queue when it is asked to be published, because that is
		# the document carrying the options the whole product is built from.
		return

	disabled_changed = bool(doc.get_doc_before_save()) and doc.has_value_changed("disabled")

	for store in _stores_for(doc):
		enqueue_sync(
			store,
			"product",
			dedupe_key=f"product:{store}:{doc.name}",
			ref_doctype="Item",
			ref_docname=doc.name,
		)

		# Taking a size out of production moves no stock, so nothing else would ever tell
		# Shopify about it. Without this the variant stayed on the storefront at its last
		# known quantity and kept selling -- and because one disabled size rightly does not
		# archive the whole range, nothing anywhere said otherwise.
		if disabled_changed and not doc.get("has_variants"):
			from shopify_integration.outbound.inventory import enqueue_for_all_locations

			enqueue_for_all_locations(store, doc.name)

	_republish_template_if_waiting(doc)


def _republish_template_if_waiting(doc) -> None:
	"""A variant whose template is meant to be published, but is not yet, re-queues the template.

	A template on its own has nothing to sell, so `_create_product` declines it and the queue
	row is marked Done. That is correct -- but it means the publish decision is spent. Saving
	the template first and adding sizes afterwards is exactly how anyone works in the UI, and
	a worker draining between the two steps consumed the only attempt.

	The variants cannot rescue it themselves: they carry no `publish_to_shopify` of their own,
	and `_stores_for` finds a store through the template only once the template *has* a link.
	Before the first publish there is none, so nothing fires and the product never appears.

	So a variant of an unpublished, publish-marked template queues the template again. The
	dedupe key is the template's, so six sizes arriving together coalesce into one push.
	"""
	template = doc.get("variant_of")
	if not template or doc.get("has_variants"):
		return
	if not frappe.db.get_value("Item", template, "publish_to_shopify"):
		return

	for store in frappe.get_all(
		"Shopify Store",
		filters={"enabled": 1, "sync_items": 1, "publish_new_items": 1},
		pluck="name",
	):
		already = frappe.db.exists(
			"Shopify Item Link", {"store": store, "template_item": template, "product_gid": ("is", "set")}
		)
		if already:
			continue
		enqueue_sync(
			store,
			"product",
			dedupe_key=f"product:{store}:{template}",
			ref_doctype="Item",
			ref_docname=template,
		)


def _stores_for(doc) -> list[str]:
	"""Stores that should hear about this item: the ones it is on, plus the ones it is for.

	An item already linked goes to those stores. An item ticked Publish to Shopify and not yet
	on a store goes to every enabled store that accepts new items -- which is how a product
	created in ERPNext reaches a shop at all.
	"""
	stores = [
		store
		for store in linked_stores(doc.name)
		if frappe.db.get_value("Shopify Store", store, "sync_items")
	]

	# A new variant of a published range inherits the decision. Nobody ticks a box per size:
	# the template already said this product belongs in the shop, and a size added to it is
	# part of that product. Without this a new variant sits unlinked for ever -- the template
	# is already published, so nothing takes the create path again.
	template = doc.get("variant_of")
	if template:
		for store in frappe.get_all(
			"Shopify Item Link",
			filters={"template_item": template, "product_gid": ["is", "set"]},
			pluck="store",
			distinct=True,
		):
			if store not in stores and frappe.db.get_value("Shopify Store", store, "sync_items"):
				stores.append(store)

	if not doc.get("publish_to_shopify"):
		return stores

	for store in frappe.get_all(
		"Shopify Store",
		filters={"enabled": 1, "sync_items": 1, "publish_new_items": 1},
		pluck="name",
	):
		if store not in stores:
			stores.append(store)

	return stores


def push_products(store: str, rows: list[dict]) -> None:
	"""Queue handler for the ``product`` operation.

	Reads current state at drain time rather than trusting the queue payload, so a row that
	waited through a backlog pushes what the item looks like now.
	"""
	client = ShopifyClient.for_store(store)

	failures: dict[str, Exception] = {}

	for row in rows:
		try:
			item_code = row.get("ref_docname")
			if not item_code or not frappe.db.exists("Item", item_code):
				continue

			link = product_link_for(store, item_code)
			if not link or not link.product_gid:
				if _attach_to_published_template(client, store, item_code):
					continue

				if _should_publish(store, item_code):
					_create_product(client, store, item_code)
					continue

				# Unmapped and not meant for this shop. Usually a products/delete webhook unlinked
				# it while this row was still pending. Skipped rather than raised: raising here
				# fails every *other* row in the claimed batch too, and the queue's whole promise
				# is that a poison row never blocks the ones behind it.
				frappe.logger("shopify_integration").info(
					f"Skipping {item_code}: no Shopify product mapping for store {store}"
				)
				continue

			# The product belongs to the template, not to the variant that was saved. A variant's
			# link carries the *parent* product's gid -- every child of a template shares one --
			# so building the payload from the variant sent the whole product's status and title
			# from one size. Disabling a single out-of-production size archived the entire range
			# from the storefront, and with sync_item_titles on it renamed the product to
			# "Kurta Set - Red / XXL".
			subject = frappe.get_cached_value("Item", item_code, "variant_of") or item_code
			item = frappe.get_doc("Item", subject)

			# Only what ERPNext actually owns. Shopify holds the storefront copy: the title a
			# customer reads and the description someone wrote for the product page. Sending
			# ERPNext's item_name and description on every save destroyed both -- a merchant
			# adjusting an item's *weight* replaced "Banarasi Silk Saree, Festive Edition" with
			# the plain item name, and wiped the marketing HTML under it.
			#
			# Whether the item is sellable is ERPNext's to say, so status still goes.
			store_doc = frappe.get_cached_doc("Shopify Store", store)
			payload = {
				"id": link.product_gid,
				"status": "ARCHIVED" if _product_is_dead(subject, item) else "ACTIVE",
			}

			# The storefront copy, and the manual collections it belongs to, folded into the
			# same mutation. ERPNext is the master for all of it; where a field is empty the
			# Item's own name and description stand in.
			problems: list[str] = []
			plan = None
			if store_doc.get("sync_website_content"):
				payload.update(website_payload(store_doc, item, creating=False))
				plan = _collection_plan(client, store_doc, item, link)
				problems += plan["problems"]
				if plan["join"]:
					payload["collectionsToJoin"] = plan["join"]
				if plan["leave"]:
					payload["collectionsToLeave"] = plan["leave"]

			# Read Shopify's current status only for a product this app has never considered
			# publishing -- one created before it did so. Once the decision is recorded, no later
			# save costs an extra call, and none revisits it: a product that is ACTIVE and off the
			# channel was taken off by the merchant, and an ERPNext save must not overrule that.
			pending = payload["status"] == "ACTIVE" and not _publish_decided(store, link.product_gid)
			was = _shopify_status(client, link.product_gid) if pending else None

			client.execute(load_query("product_update"), {"product": payload}, cost_hint=10)

			if pending:
				if was in ("DRAFT", "ARCHIVED"):
					publish_to_online_store(client, store, link.product_gid)
				_mark_publish_decided(store, link.product_gid)

			if plan is not None:
				# Recorded only after Shopify accepted the move, and only what the app put
				# there: a collection the merchant added by hand is never ours to remove.
				record_app_collections(link.name, plan["owned"])

				# Committed before the two calls below, which can fail. Raising afterwards
				# rolls the transaction back, and that used to take this record with it --
				# while Shopify kept the collection move it had already accepted. The two
				# then disagreed, and the next sync could neither re-join nor leave.
				frappe.db.commit()

				# Both, not one-then-stop. A bad metafield is no reason for the images to
				# go without their alt text.
				problems += _push_metafields(client, store_doc, item, link)

			enqueue_media(store, item_code)

			if problems:
				# Raised after the copy has gone, so one bad metafield does not cost the
				# product its title. The row carries the reason in plain words.
				raise ContentError("\n".join(problems))
		except Exception as exc:
			# One row's fault is one row's fault. Letting it escape failed every other row in
			# the claimed group -- including the ones after it, which were never attempted at
			# all -- and nothing reconciles product, price or media, so those were simply lost
			# until somebody found them in the dashboard.
			failures[row["name"]] = exc

	if failures:
		raise PartialFailure(failures)


def _product_is_dead(subject: str, item) -> bool:
	"""Whether the whole Shopify product should be archived.

	A template is dead when it is disabled itself, or when every variant under it is --
	there is then nothing left to sell. One disabled size is not a reason to take the range
	off the storefront; it is a reason for that variant to stop being sellable, which its
	own inventory push already handles.
	"""
	if item.disabled:
		return True
	if not item.get("has_variants"):
		return False

	children = frappe.get_all("Item", filters={"variant_of": subject}, pluck="disabled")
	return bool(children) and all(children)


# --------------------------------------------------------------------------------------
# Creating a Shopify product from an ERPNext item
# --------------------------------------------------------------------------------------


#: Shopify's own Online Store channel. Matched on the app's handle rather than its title,
#: which is localised.
ONLINE_STORE_APP_HANDLE = "online_store"


def online_store_publication(client: ShopifyClient, store: str) -> str | None:
	"""The Online Store publication's id for this shop, or None if there is not one.

	Remembered for the life of the job, which is the scale that matters: one drain can create
	fifty products, and they would otherwise ask the same unchanging question fifty times.
	A miss is remembered too, so a shop with no Online Store is not re-queried either.
	"""
	memo = getattr(frappe.local, "shopify_online_store_publication", None)
	if memo is None:
		memo = frappe.local.shopify_online_store_publication = {}
	if store in memo:
		return memo[store]

	found = None
	data = client.execute(load_query("publications"), {}, cost_hint=5)
	for node in (data.get("publications") or {}).get("nodes") or []:
		catalog = node.get("catalog") or {}
		apps = (catalog.get("apps") or {}).get("nodes") or []
		handles = {cstr(app.get("handle")).lower() for app in apps}
		titles = {cstr(app.get("title")).strip().lower() for app in apps}
		titles.add(cstr(catalog.get("title")).strip().lower())
		if ONLINE_STORE_APP_HANDLE in handles or "online store" in titles:
			found = node["id"]
			break

	memo[store] = found
	return found


def publish_to_online_store(client: ShopifyClient, store: str, product_gid: str) -> bool:
	"""Put a product on the Online Store, so a customer can actually see it.

	Creating a product leaves it in the admin with ``onlineStoreUrl`` null: every field
	correct, and invisible to the storefront. Publishing is the separate step nobody notices
	is missing until they look at the shop.

	Never raises. A shop that installed the app before ``read_publications`` and
	``write_publications`` were asked for will refuse this call, and losing the product sync
	over a channel assignment would be the worse failure -- the product is created either way,
	and the store form already warns about missing scopes.
	"""
	try:
		publication = online_store_publication(client, store)
		if not publication:
			frappe.logger("shopify_integration").warning(
				f"{store} has no Online Store publication; leaving the product unpublished."
			)
			return False

		client.execute(
			load_query("publishable_publish"),
			{"id": product_gid, "input": [{"publicationId": publication}]},
			cost_hint=10,
		)
		return True
	except Exception as exc:
		frappe.logger("shopify_integration").warning(
			f"Could not publish {product_gid} to the Online Store for {store}: {exc}. "
			"The product exists but customers cannot see it. If the app was installed before "
			"it asked for read_publications and write_publications, reinstall it."
		)
		return False


def _should_publish(store: str, item_code: str) -> bool:
	"""Both switches on, and the item actually sellable."""
	if not frappe.db.get_value("Item", item_code, "publish_to_shopify"):
		return False
	if not frappe.db.get_value("Shopify Store", store, "publish_new_items"):
		return False
	return True


def _create_product(client: ShopifyClient, store: str, item_code: str) -> str | None:
	"""Create the Shopify product for an ERPNext item, and link the two.

	Held behind a lock and re-checked inside it. Creating a product is the one operation here
	that cannot be undone by doing it again -- a second product is a second listing, and
	Shopify will happily make one. Two drains claiming rows for the same item, or a retry
	racing the row that is already running, would otherwise each find no link and each create.
	"""
	from frappe.utils.synchronization import filelock

	with filelock(f"shopify-create-product-{store}-{item_code}"[:120], timeout=60):
		existing = product_link_for(store, item_code)
		if existing and existing.product_gid:
			frappe.logger("shopify_integration").info(
				f"{item_code} is already {existing.product_gid} on {store}; not creating a second."
			)
			return existing.product_gid
		return _create_product_unlocked(client, store, item_code)


def _create_product_unlocked(client: ShopifyClient, store: str, item_code: str) -> str | None:
	"""The creation itself. Only ever called with the lock held -- see _create_product.

	Two calls, because Shopify splits them: ``productCreate`` makes the product and its one
	default variant, and only ``productVariantsBulkUpdate`` can give that variant a SKU, a
	price and inventory tracking. Without the second call the product exists but cannot be
	sold or counted.
	"""
	item = frappe.get_doc("Item", item_code)
	store_doc = frappe.get_cached_doc("Shopify Store", store)

	children = _variants_of(item_code) if item.get("has_variants") else []
	if item.get("has_variants") and not children:
		frappe.logger("shopify_integration").info(
			f"Not publishing {item_code}: a template with no variants has nothing to sell."
		)
		return None

	# A product with no price anywhere must not go live. Shopify starts a new variant at 0.00,
	# so publishing it ACTIVE puts a free saree in the shop -- worse than not publishing at
	# all, and nothing in Shopify flags it. Draft keeps the mapping, the SKU and the inventory
	# link, and leaves the merchant to price it and hit Activate.
	#
	# For a template the price is on the *variants*, not on the template, which normally has
	# none. Judging the template by its own price made almost the entire catalogue go live as
	# DRAFT -- correctly priced variants and all -- and DRAFT products are never published to
	# the Online Store, so they were invisible twice over.
	children, unpriced, any_variant_priced = _sellable_variants(children, store_doc)
	has_price = (
		any_variant_priced if item.get("has_variants") else (_selling_price(item_code, store_doc) is not None)
	)

	if unpriced:
		frappe.logger("shopify_integration").warning(
			f"{item_code}: leaving {len(unpriced)} variant(s) off {store} until they are priced "
			f"-- {', '.join(unpriced)}. Shopify would otherwise sell them at 0.00. Price them "
			"and save the Item to add them."
		)
	if not has_price:
		frappe.logger("shopify_integration").info(
			f"Publishing {item_code} to {store} as DRAFT: no selling price on "
			f"'{store_doc.selling_price_list}' and no standard rate"
			+ (" on any variant." if item.get("has_variants") else " on the Item.")
		)

	options = _options_from(children) if children else []

	payload = {
		"title": cstr(item.item_name or item_code)[:255],
		"descriptionHtml": cstr(item.description or ""),
		"productType": cstr(item.item_group or "")[:255],
		"vendor": cstr(item.get("brand") or "")[:255] or None,
		"status": "ARCHIVED" if item.disabled else ("ACTIVE" if has_price else "DRAFT"),
	}
	if options:
		payload["productOptions"] = options

	payload.update(website_payload(store_doc, item, creating=True))

	data = client.execute(load_query("product_create"), {"product": payload}, cost_hint=15)
	product = (data.get("productCreate") or {}).get("product") or {}
	if not product.get("id"):
		return None

	if item.get("has_variants"):
		# Before the variants, not after them. Everything below this line takes seconds and
		# makes API calls, and Shopify has already announced the product to us.
		link_template(store, item_code, product["id"])

	if children:
		_create_variants(client, product["id"], children, store_doc, first=True)
	else:
		variants = (product.get("variants") or {}).get("nodes") or []
		if variants:
			_fill_variant(client, product["id"], variants[0]["id"], item, store_doc)

	# Re-read: SKUs, prices and inventory items only exist after the second call, and a link
	# without the inventory item id cannot push stock.
	product = _reread(client, product["id"]) or product

	if cstr(payload.get("status")) == "ACTIVE":
		publish_to_online_store(client, store, product["id"])
	_publish_pending[product["id"]] = cstr(payload.get("status")) == "ACTIVE"

	if children:
		_link_variants(store, item_code, product, children)
	else:
		_link(store, item_code, product)

	# Now that it is linked, put it in the collection whose tax override matches what ERPNext
	# will book for it. A no-op on any store that has not filled the map in.
	from shopify_integration.outbound.collections import enqueue_for_item as enqueue_collection

	enqueue_collection(store, item_code)

	# And one more pass over the product itself. The manual collections, the metafields and
	# the image alt text all need the link -- and, for the alt text, the media -- that only
	# come into existence on the lines above. The update path already does all three, so it
	# is given a row rather than having its work duplicated here.
	if store_doc.get("sync_website_content"):
		enqueue_sync(
			store,
			"product",
			dedupe_key=f"product:{store}:{item_code}",
			ref_doctype="Item",
			ref_docname=item_code,
		)

	# And its photographs, here rather than only from `push_initial_state`. That runs from
	# `upsert_link`, which a template never goes through -- its children do -- and asking
	# whether a *variant* holds images is asking the wrong item: the range's photographs
	# hang on the template. So a published template's picture was never sent at all.
	enqueue_media(store, item_code)

	frappe.logger("shopify_integration").info(
		f"Published {item_code} to {store} as {product['id']}"
		+ (f" with {len(children)} variants" if children else "")
	)
	return product["id"]


def _fill_variant(client: ShopifyClient, product_gid: str, variant_gid: str, item, store_doc) -> None:
	variant: dict = {
		"id": variant_gid,
		"inventoryItem": {"sku": cstr(item.item_code)[:255], "tracked": bool(item.is_stock_item)},
	}

	price = _selling_price(item.item_code, store_doc)
	if price is not None:
		variant["price"] = str(price)

	client.execute(
		load_query("product_variants_bulk_update"),
		{"productId": product_gid, "variants": [variant]},
		cost_hint=15,
	)


def item_image_url(item) -> str | None:
	"""An absolute, publicly fetchable URL for the item's Image field, or None.

	The single-image question the product payload still asks. Which files are public and
	how they are addressed is `outbound.media`'s to answer, because it has to answer it
	for attachments too.
	"""
	from shopify_integration.outbound.media import public_url

	return public_url(cstr(item.get("image")), cstr(item.name))


def _sellable_variants(children: list[dict], store_doc) -> tuple[list[dict], list[str], bool]:
	"""Split a template's variants into the ones that can be sold and the ones that cannot.

	Returns the variants to create, the item codes held back, and whether any price was found
	at all -- which is what decides ACTIVE or DRAFT for the template.

	A variant with no price would be listed at 0.00, which is worse than not being listed. So
	priced variants go up and the rest are held back by name, for somebody to price.

	The exception is a template where *nothing* is priced. Those go up whole, as DRAFT: the
	product sells nothing either way, and keeping every variant means the SKUs, the mapping
	and the inventory links all exist ready for the day it is priced.
	"""
	if not children:
		return [], [], False

	priced = [child for child in children if _selling_price(child["item_code"], store_doc) is not None]
	if not priced:
		return children, [], False

	unpriced = [child["item_code"] for child in children if child not in priced]
	return priced, unpriced, True


def _selling_price(item_code: str, store_doc):
	"""The item's price on the store's own list, or None to let Shopify keep its default.

	None rather than zero: a product listed at nothing is worse than one a merchant has yet
	to price.
	"""
	from shopify_integration.outbound.price import current_price

	return current_price(store_doc, item_code)


def _reread(client: ShopifyClient, product_gid: str) -> dict | None:
	data = client.execute(load_query("product_by_id"), {"id": product_gid}, cost_hint=10)
	product = data.get("product")
	if not product:
		return None

	product["variants"] = [edge["node"] for edge in ((product.get("variants") or {}).get("edges") or [])]
	return product


#: Products published during this job, so the links created a moment later can record it.
_publish_pending: dict[str, bool] = {}


def product_link_for(store: str, item_code: str) -> frappe._dict | None:
	"""The Shopify product an ERPNext item belongs to, template or not.

	A template has no link of its own -- only its variants do, each recording ``template_item``.
	Looking one up by item_code alone therefore found nothing for a template, concluded it had
	never been published, and created a **second** Shopify product for it. That duplicate came
	back through products/create as an unknown product, was imported as two more ERPNext
	templates, and took the original's five variant links with it.

	Any one of the variants' links will do: they all name the same product, and the product is
	what the caller is after.
	"""
	fields = ["name", "product_gid", "is_variant", "template_item"]

	link = frappe.db.get_value(
		"Shopify Item Link", {"store": store, "item_code": item_code}, fields, as_dict=True
	)
	if link and link.product_gid:
		return link

	through_variants = frappe.get_all(
		"Shopify Item Link",
		filters={"store": store, "template_item": item_code, "product_gid": ["is", "set"]},
		fields=fields,
		order_by="creation asc",
		limit=1,
	)
	return through_variants[0] if through_variants else None


def _publish_decided(store: str, product_gid: str) -> bool:
	"""Whether this app has already made its one publish decision for a product.

	Asked of the product, not of a link. A template's links are its variants', so a
	link-keyed question is unanswerable for exactly the products this matters most for.
	"""
	return bool(
		product_gid
		and frappe.db.exists(
			"Shopify Item Link",
			{"store": store, "product_gid": product_gid, "online_store_publish_done": 1},
		)
	)


def _mark_publish_decided(store: str, product_gid: str) -> None:
	"""Record that this product's one publish decision has been made.

	Per product rather than per link: a variant product has one Shopify product and many
	links, and the channel is a property of the product.
	"""
	for name in frappe.get_all(
		"Shopify Item Link", filters={"store": store, "product_gid": product_gid}, pluck="name"
	):
		frappe.db.set_value("Shopify Item Link", name, "online_store_publish_done", 1, update_modified=False)


def _shopify_status(client: ShopifyClient, product_gid: str) -> str | None:
	"""Shopify's current status for a product, or None if it cannot be read."""
	try:
		data = client.execute(load_query("product_status"), {"id": product_gid}, cost_hint=2)
		return cstr((data.get("product") or {}).get("status")) or None
	except Exception:
		return None


def push_initial_stock(store: str, item_code: str) -> int:
	"""Send what ERPNext already has of a newly linked item.

	Inventory is otherwise pushed only when stock *moves*. A product published with three on
	the shelf therefore went live showing zero, and stayed at zero until someone sold or
	received one, or the 03:00 reconciliation came round -- an item listed as out of stock on
	the day it appears being close to the worst version of that.

	Every mapped warehouse, because the store may sell one item from several, and the normal
	enqueue so it batches and dedupes with everything else.
	"""
	from shopify_integration.outbound.inventory import enqueue_for_item

	store_doc = frappe.get_cached_doc("Shopify Store", store)
	if not store_doc.sync_inventory:
		return 0

	queued = 0
	for row in store_doc.location_map or []:
		if row.warehouse:
			queued += enqueue_for_item(item_code, row.warehouse, "Item", item_code)
	return queued


def push_initial_price(store: str, item_code: str) -> int:
	"""Send the price ERPNext already holds, for the same reason as the stock.

	Creating the variant carries the price Shopify was given at the time. A price typed
	before the item was ever published, or changed while the product row waited in the
	queue, is not in that figure.
	"""
	from shopify_integration.outbound.price import enqueue_price

	return enqueue_price(store, item_code) or 0


def push_initial_state(store: str, item_code: str) -> None:
	"""Send the stock and the price ERPNext already holds for a freshly linked item.

	Called from `upsert_link`, so it runs from whichever path writes the link first -- and
	they race. Publishing a product makes Shopify fire `products/create` straight back, and
	that webhook writes its own link through the catalogue mapping, which creating the
	product knows nothing about. On 1 October it wrote STOITEM202605498's link at 21:47:02.96
	while the job publishing that very item still had 130ms to run. The job then found a link
	where it expected none, took the update branch -- which sends neither stock nor price --
	and the item went live at zero with no queue row anywhere to correct it.

	Doing it here instead of at each call site is the point: there is one place a link comes
	into existence, and this is it.

	Idempotent twice over. A link past its watermark is left alone, so this stops firing once
	the figure has actually gone; and both pushes are deduplicated enqueues, so however many
	paths call this for one item, one row is queued and one row drains.
	"""
	link = frappe.db.get_value(
		"Shopify Item Link",
		{"store": store, "item_code": item_code},
		["name", "initial_state_pushed", "inventory_synced_on", "price_synced_on"],
		as_dict=True,
	)
	if not link or link.initial_state_pushed:
		return

	if not link.inventory_synced_on and _erpnext_holds_stock(store, item_code):
		push_initial_stock(store, item_code)
	if not link.price_synced_on and _erpnext_holds_a_price(store, item_code):
		push_initial_price(store, item_code)
	# And its photographs. Guarded like the rest: an Item created *by* a catalogue import
	# has no images at all, and asking Shopify what media each of 87,000 products has in
	# order to discover that is a cost with nothing at the end of it.
	if _erpnext_holds_images(item_code):
		enqueue_media(store, item_code)

	# Written here, not when those rows drain. The two watermarks above are stamped on
	# drain, and media had no watermark at all, so every `products/update` that arrived in
	# the meantime queued the same three pushes again -- each of which mutates the product,
	# each of which makes Shopify send another `products/update`. That is the loop, and the
	# marker is what ends it: one link, one opening push, whatever calls this afterwards.
	#
	# After the enqueues rather than before, so a failure to queue is retried rather than
	# swallowed. Two callers racing both enqueue, and `enqueue_sync` collapses them on the
	# dedupe key, which is the behaviour this relied on before and still does.
	frappe.db.set_value(
		"Shopify Item Link", link.name, "initial_state_pushed", 1, update_modified=False
	)


def _erpnext_holds_images(item_code: str) -> bool:
	from shopify_integration.outbound.media import desired_files

	return bool(desired_files(item_code))


def _erpnext_holds_stock(store: str, item_code: str) -> bool:
	"""Whether ERPNext has a quantity worth sending.

	Nothing on hand means nothing to say -- and saying it would be destructive. Importing a
	catalogue *from* Shopify creates ERPNext Items with no stock at all, and this runs on
	those links too; pushing their zero over the merchant's real Shopify quantity is the one
	mistake it must never make. A product ERPNext publishes starts at zero on Shopify anyway,
	so staying quiet about a zero costs nothing either way.

	An import that matches an item ERPNext already stocks does push, which is the contract
	`sync_inventory` describes: once the two are linked, ERPNext owns the count, and the
	nightly reconciliation would send the same figure a few hours later regardless.
	"""
	from shopify_integration.outbound.inventory import available_for_location

	store_doc = frappe.get_cached_doc("Shopify Store", store)
	if not store_doc.sync_inventory:
		return False

	return any(
		available_for_location(store_doc, item_code, row.location_gid)
		for row in store_doc.location_map or []
		if row.location_gid
	)


def _erpnext_holds_a_price(store: str, item_code: str) -> bool:
	"""Same rule for the price: send one if there is one, and never overwrite with nothing."""
	store_doc = frappe.get_cached_doc("Shopify Store", store)
	if not store_doc.sync_prices:
		return False
	return _selling_price(item_code, store_doc) is not None


def link_template(store: str, template_item: str, product_gid: str) -> str:
	"""Record that this template owns this Shopify product.

	Written the moment `productCreate` returns, and committed on the spot, because the gap
	it closes is measured in seconds and another process has to see across it. Publishing a
	template is several calls -- create the product, create its variants, re-read them --
	and the variant links are written only at the end. Shopify fires products/create the
	instant the product exists, and that webhook is handled by a different worker in a
	different transaction, so it used to arrive when nothing whatsoever identified the
	product as ours. The inbound writer then named a template after the Shopify handle and
	built a second ERPNext template for a range that already had one.

	A template's row carries a product and no variant: it has no Shopify variant of its own
	to point at. `is_template` is what keeps it out of everything that walks links expecting
	something sellable.
	"""
	existing = frappe.db.get_value(
		"Shopify Item Link", {"store": store, "item_code": template_item}, "name"
	)
	if existing:
		frappe.db.set_value(
			"Shopify Item Link",
			existing,
			{"product_gid": product_gid, "is_template": 1, "is_variant": 0},
			update_modified=False,
		)
		name = existing
	else:
		link = frappe.new_doc("Shopify Item Link")
		link.store = store
		link.item_code = template_item
		link.product_gid = product_gid
		link.is_template = 1
		link.is_variant = 0
		link.origin = "ERPNext"
		link.insert(ignore_permissions=True)
		name = link.name

	# The commit is the point. An uncommitted row is invisible to the webhook worker, which
	# is the only reader that matters here.
	frappe.db.commit()
	return name


def _link(store: str, item_code: str, product: dict) -> None:
	from shopify_integration.catalogue.mapping import upsert_link

	variants = product.get("variants") or []
	variant = variants[0] if variants else {}
	upsert_link(
		store,
		item_code=item_code,
		product=product,
		variant=variant,
		is_variant=False,
		origin="ERPNext",
	)
	if _publish_pending.pop(cstr(product.get("id")), False):
		_mark_publish_decided(store, cstr(product.get("id")))


# --------------------------------------------------------------------------------------
# Templates and their variants
# --------------------------------------------------------------------------------------


def _variants_of(template: str) -> list[dict]:
	"""Every sellable variant of a template, each with the attribute values that define it.

	Ordered by item code so the Shopify variants land in a stable order, and so re-running
	against the same template produces the same product twice.
	"""
	children = []
	for code in frappe.get_all(
		"Item",
		filters={"variant_of": template, "disabled": 0},
		order_by="item_code",
		pluck="name",
	):
		values = frappe.get_all(
			"Item Variant Attribute",
			filters={"parent": code, "parenttype": "Item"},
			fields=["attribute", "attribute_value"],
			order_by="idx",
		)
		if not values:
			# A variant with no attribute values cannot be placed on any option, and Shopify
			# would reject the whole batch for it. Skipping one is better than losing all.
			frappe.logger("shopify_integration").info(
				f"Skipping variant {code}: it declares no attribute values."
			)
			continue
		children.append({"item_code": code, "values": values})
	return children


def _options_from(children: list[dict]) -> list[dict]:
	"""Shopify product options, built from the values the variants actually use.

	Not from the Item Attribute's own list: an attribute may hold every size a business has
	ever stocked, and a shop selling two of them should offer two.
	"""
	seen: dict[str, list[str]] = {}
	for child in children:
		for row in child["values"]:
			name = cstr(row.attribute).strip()
			value = cstr(row.attribute_value).strip()
			if not name or not value:
				continue
			values = seen.setdefault(name, [])
			if value not in values:
				values.append(value)

	return [
		{"name": name, "position": index + 1, "values": [{"name": v} for v in values]}
		for index, (name, values) in enumerate(seen.items())
	]


def _create_variants(
	client: ShopifyClient, product_gid: str, children: list[dict], store_doc, first: bool
) -> None:
	"""Create one Shopify variant per ERPNext variant, in batches Shopify will accept."""
	payload = []
	for child in children:
		variant: dict = {
			"optionValues": [
				{"optionName": cstr(row.attribute).strip(), "name": cstr(row.attribute_value).strip()}
				for row in child["values"]
				if cstr(row.attribute).strip() and cstr(row.attribute_value).strip()
			],
			"inventoryItem": {
				"sku": cstr(child["item_code"])[:255],
				"tracked": bool(frappe.db.get_value("Item", child["item_code"], "is_stock_item")),
			},
		}
		price = _selling_price(child["item_code"], store_doc)
		if price is not None:
			variant["price"] = str(price)
		payload.append(variant)

	for start in range(0, len(payload), VARIANT_BATCH):
		batch = payload[start : start + VARIANT_BATCH]
		client.execute(
			load_query("product_variants_bulk_create"),
			{
				"productId": product_gid,
				"variants": batch,
				# Only the first call may remove the placeholder variant productCreate leaves
				# behind; Shopify refuses the strategy once real variants exist.
				"strategy": "REMOVE_STANDALONE_VARIANT" if (first and start == 0) else "DEFAULT",
			},
			cost_hint=15 + len(batch),
		)


def _link_variants(store: str, template: str, product: dict, children: list[dict]) -> None:
	"""Link each ERPNext variant to its Shopify variant.

	The template gets no link of its own -- it has no Shopify variant to point at. Each child
	records `template_item` instead, which is how the product is found again later.

	Matched on SKU, which is the item code we just sent. Position is not safe to match on:
	Shopify orders variants by its own option positions, not by the order they were created.
	"""
	from shopify_integration.catalogue.mapping import upsert_link

	by_sku = {cstr(v.get("sku")): v for v in (product.get("variants") or []) if v.get("sku")}

	for child in children:
		variant = by_sku.get(child["item_code"])
		if not variant:
			frappe.logger("shopify_integration").info(
				f"Shopify returned no variant for {child['item_code']}; it will be linked on the "
				"next product sync."
			)
			continue
		upsert_link(
			store,
			item_code=child["item_code"],
			product=product,
			variant=variant,
			is_variant=True,
			template_item=template,
			origin="ERPNext",
		)

	if _publish_pending.pop(cstr(product.get("id")), False):
		_mark_publish_decided(store, cstr(product.get("id")))


def _attach_to_published_template(client: ShopifyClient, store: str, item_code: str) -> bool:
	"""Add a newly created ERPNext variant to a product that is already on Shopify.

	A merchant adding a size to an existing range expects it to appear in the shop. Without
	this the variant sits unlinked for ever: the template is already published, so nothing
	takes the create path again, and nothing else would ever notice the new child.

	Returns True when it handled the item, so the caller stops.
	"""
	template = frappe.db.get_value("Item", item_code, "variant_of")
	if not template:
		return False

	# Found through a sibling, not through the template. A template carries no link of its own --
	# it has no Shopify variant to point at -- so each variant records `template_item` instead.
	# Looking for a link on the template finds nothing, ever, and a newly added size would sit
	# unlinked for good.
	parent = frappe.db.get_value(
		"Shopify Item Link",
		{"store": store, "template_item": template, "product_gid": ["is", "set"]},
		"product_gid",
	)
	if not parent:
		return False

	child = next((c for c in _variants_of(template) if c["item_code"] == item_code), None)
	if not child:
		return False

	store_doc = frappe.get_cached_doc("Shopify Store", store)
	_create_variants(client, parent, [child], store_doc, first=False)

	product = _reread(client, parent)
	if product:
		_link_variants(store, template, product, [child])

	frappe.logger("shopify_integration").info(f"Added {item_code} to the Shopify product for {template}")
	return True


# --------------------------------------------------------------------------------------
# One-off repair
# --------------------------------------------------------------------------------------


def repair_split_template(
	store: str,
	template_item: str,
	keep_product: str,
	*,
	drop_product: str | None = None,
	drop_variants: list[str] | None = None,
	drop_template: str | None = None,
	apply: bool = False,
) -> dict:
	"""Put a range back on one Shopify product. Dry run unless `apply`.

	Written for the 3 October fault, where publishing a template with three sizes produced a
	second ERPNext template, and disabling one size then produced a second Shopify product
	carrying the other two. The code that caused it is fixed; this is for the catalogues it
	already split.

	What it does, in this order, because the order matters:

	1. points every variant link of `template_item` back at `keep_product`, and sets their
	   `template_item` correctly -- including any dragged onto `drop_template`;
	2. writes the template's own link row, which is what stops this happening again;
	3. removes `drop_variants` (ERPNext item codes) from `keep_product` on Shopify;
	4. deletes `drop_product` on Shopify, once nothing points at it any more;
	5. removes `drop_template` from ERPNext, but only if it has no variants of its own.

	Every step is reported before any of it runs. Nothing outside the ids named here is
	touched, and a step whose target has already been cleaned up is skipped rather than
	failing the rest.
	"""
	report: dict = {
		"store": store,
		"template": template_item,
		"keep_product": keep_product,
		"apply": apply,
		"repointed": [],
		"template_link": None,
		"variants_removed": [],
		"product_deleted": None,
		"template_deleted": None,
		"warnings": [],
	}

	if not frappe.db.exists("Item", template_item):
		report["warnings"].append(f"{template_item} is not an Item on this site; nothing to repair.")
		return report

	# -- 1. every link that belongs to this range ------------------------------------
	children = frappe.get_all("Item", filters={"variant_of": template_item}, pluck="name")
	wanted = set(children) | {template_item}
	if drop_template:
		wanted |= set(frappe.get_all("Item", filters={"variant_of": drop_template}, pluck="name"))

	links = frappe.get_all(
		"Shopify Item Link",
		filters={"store": store, "item_code": ["in", sorted(wanted)]},
		fields=["name", "item_code", "product_gid", "template_item", "is_template"],
	)
	for link in links:
		if link.is_template:
			continue
		if link.product_gid == keep_product and link.template_item == template_item:
			continue
		report["repointed"].append(
			{
				"link": link.name,
				"item": link.item_code,
				"from_product": link.product_gid,
				"from_template": link.template_item,
			}
		)
		if apply:
			frappe.db.set_value(
				"Shopify Item Link",
				link.name,
				{"product_gid": keep_product, "template_item": template_item},
				update_modified=False,
			)

	# -- 2. the template's own row ----------------------------------------------------
	existing_template_link = frappe.db.get_value(
		"Shopify Item Link", {"store": store, "item_code": template_item}, "name"
	)
	report["template_link"] = existing_template_link or "would be created"
	if apply:
		report["template_link"] = link_template(store, template_item, keep_product)

	client = ShopifyClient.for_store(store)

	# -- 3. sizes that should not be on the kept product ------------------------------
	for item_code in drop_variants or []:
		variant_gid = frappe.db.get_value(
			"Shopify Item Link", {"store": store, "item_code": item_code}, "variant_gid"
		)
		if not variant_gid:
			report["warnings"].append(f"{item_code} has no variant on {store}; not removing it.")
			continue
		report["variants_removed"].append({"item": item_code, "variant": variant_gid})
		report["warnings"].append(
			f"{item_code}'s link is kept and re-pointed as asked, but its Shopify variant is "
			f"being deleted -- the link will name a variant that no longer exists. Re-publish "
			f"the size to recreate it, or delete the link if the size is gone for good."
		)
		if apply:
			result = client.execute(
				load_query("product_variants_bulk_delete"),
				{"productId": keep_product, "variantsIds": [variant_gid]},
				cost_hint=10,
			)
			errors = (result.get("productVariantsBulkDelete") or {}).get("userErrors") or []
			if errors:
				raise frappe.ValidationError(
					f"Shopify refused to remove {item_code}: "
					+ "; ".join(cstr(e.get("message")) for e in errors)
				)

	# -- 4. the duplicate product -----------------------------------------------------
	if drop_product:
		still_pointing = frappe.get_all(
			"Shopify Item Link",
			filters={"store": store, "product_gid": drop_product},
			pluck="item_code",
		)
		report["product_deleted"] = {"product": drop_product, "links_cleared": still_pointing}
		if apply:
			for name in frappe.get_all(
				"Shopify Item Link", filters={"store": store, "product_gid": drop_product}, pluck="name"
			):
				frappe.delete_doc("Shopify Item Link", name, force=True, ignore_permissions=True)
			result = client.execute(
				load_query("product_delete"), {"input": {"id": drop_product}}, cost_hint=10
			)
			errors = (result.get("productDelete") or {}).get("userErrors") or []
			if errors:
				raise frappe.ValidationError(
					f"Shopify refused to delete {drop_product}: "
					+ "; ".join(cstr(e.get("message")) for e in errors)
				)

	# -- 5. the twin ERPNext template -------------------------------------------------
	if drop_template:
		if not frappe.db.exists("Item", drop_template):
			report["template_deleted"] = f"{drop_template} is already gone"
		else:
			remaining = frappe.get_all("Item", filters={"variant_of": drop_template}, pluck="name")
			if remaining:
				report["warnings"].append(
					f"Not deleting {drop_template}: it still has variants ({', '.join(remaining[:5])}). "
					"Those belong somewhere; sort them out first."
				)
			else:
				report["template_deleted"] = drop_template
				if apply:
					for name in frappe.get_all(
						"Shopify Item Link", filters={"item_code": drop_template}, pluck="name"
					):
						frappe.delete_doc("Shopify Item Link", name, force=True, ignore_permissions=True)
					frappe.delete_doc("Item", drop_template, force=True, ignore_permissions=True)

	if apply:
		frappe.db.commit()
	return report


# --------------------------------------------------------------------------------------
# Website content, sent alongside the product
# --------------------------------------------------------------------------------------


def _collection_plan(client: ShopifyClient, store_doc, item, link) -> dict:
	"""Which manual collections to join and leave for this product."""
	from shopify_integration.outbound.content import collection_plan

	def describe(gids: list[str]) -> dict[str, dict]:
		if not gids:
			return {}
		data = client.execute(
			load_query("collections_by_id"), {"ids": gids}, cost_hint=5 + len(gids)
		)
		found = {}
		for node in data.get("nodes") or []:
			if node and node.get("id"):
				found[cstr(node["id"])] = {
					"title": cstr(node.get("title")),
					# A ruleSet is what makes a collection automatic. Shopify decides its
					# membership from the rules and refuses to be joined to a product.
					"automatic": bool(node.get("ruleSet")),
				}
		return found

	return collection_plan(store_doc, item, link.name, describe)


def _push_metafields(client: ShopifyClient, store_doc, item, link) -> list[str]:
	"""Write the product's metafields, once everything in them is known to be acceptable.

	Validated first and sent only if clean, because `metafieldsSet` is all-or-nothing: one
	bad taxonomy reference costs the product every other metafield in the call, and the
	error Shopify returns names none of them.
	"""
	wanted = desired_metafields(item)
	if not wanted:
		return []

	problems = validate_category_metafields(client, item)

	# Shopify refuses a key it has no definition for, and refuses the whole call with it --
	# so one unwritable key would cost the product every other metafield in the batch. The
	# definitions are read first and anything undefined is reported and held back, which
	# keeps the rest of them going.
	defined = _defined_metafields(client, {metafield["namespace"] for metafield in wanted})
	sendable = []
	for metafield in wanted:
		pair = (metafield["namespace"], metafield["key"])
		if pair in defined:
			sendable.append(metafield)
		else:
			problems.append(
				f"{item.name}: {pair[0]}.{pair[1]} has no metafield definition on this store, "
				"so Shopify will not accept it. Create the definition first -- the shop's own "
				"fields are on the Shopify Store form under Create Metafield Definitions."
			)

	if not sendable:
		return problems

	payload = [dict(metafield, ownerId=link.product_gid) for metafield in sendable]
	result = client.execute(
		load_query("metafields_set"), {"metafields": payload}, cost_hint=10 + len(payload)
	)
	errors = (result.get("metafieldsSet") or {}).get("userErrors") or []
	return problems + [
		f"{item.name}: metafield {cstr(error.get('field'))} -- {cstr(error.get('message'))}"
		for error in errors
	]


def _defined_metafields(client: ShopifyClient, namespaces: set[str]) -> set[tuple[str, str]]:
	"""(namespace, key) for every product metafield this store has defined."""
	defined: set[tuple[str, str]] = set()
	for namespace in sorted(namespaces):
		data = client.execute(
			load_query("metafield_definitions"), {"namespace": namespace}, cost_hint=10
		)
		for node in ((data.get("metafieldDefinitions") or {}).get("nodes") or []):
			defined.add((cstr(node.get("namespace")), cstr(node.get("key"))))
	return defined


