"""Every image on an Item, mirrored to its Shopify product.

Staff keep the main photograph in the Item's Image field and the rest as attachments. Only
the Image field was ever sent, and clearing it left the old picture on the storefront for
good -- so a saree with four photographs in ERPNext had one in the shop, and a corrected
image never replaced the wrong one.

The line this must not cross is the merchant's own photography, uploaded in the Shopify
admin. The app records which media it created, per file, and that record is the only thing
separating an image it may delete from one it must not touch.
"""

from __future__ import annotations

import itertools
import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from shopify_integration.outbound import media as media_module
from shopify_integration.outbound.media import (
	IMAGE_EXTENSIONS,
	MediaSyncError,
	desired_files,
	media_plan,
	owned_media,
	sync_item_media,
)

PRODUCT = "gid://shopify/Product/media-1"
THEIRS = "gid://shopify/MediaImage/merchant-9"

_nth = itertools.count()


def _png() -> bytes:
	"""A 1x1 PNG, a different colour every call, so Frappe files them separately."""
	import io

	from PIL import Image

	n = next(_nth)
	buffer = io.BytesIO()
	Image.new("RGB", (1, 1), ((n * 37) % 256, (n * 91) % 256, (n * 53) % 256)).save(buffer, format="PNG")
	return buffer.getvalue()


def _not_a_photograph() -> bytes:
	return f"a size chart, not a photograph {next(_nth)}".encode()


def media_id(file_url: str) -> str:
	"""What `_Client` makes of a file: its media id is derived from the URL it was sent."""
	return f"gid://shopify/MediaImage/{file_url.rsplit('/', 1)[-1]}"


class _Client:
	"""A Shopify product's media, and what the app does to it.

	Media ids are made from the source URL so a test can say which file a given media came
	from, which is the whole thing under test.
	"""

	def __init__(self, media=None, variant_media=None, fail=None):
		#: [{id, status, originalSource}] in product order
		self.media = list(media or [])
		self.variant_media = dict(variant_media or {})
		self.fail = fail
		self.created, self.deleted, self.moves, self.attached = [], [], [], []

	def execute(self, query, variables=None, cost_hint=0):
		variables = variables or {}

		if "media" in variables and "productId" in variables:
			self.created.extend(variables["media"])
			if self.fail == "refuse":
				return {
					"productCreateMedia": {
						"media": [],
						"mediaUserErrors": [{"code": "INVALID", "message": "Invalid image source"}],
					}
				}
			made = []
			for entry in variables["media"]:
				node = {
					"id": f"gid://shopify/MediaImage/{entry['originalSource'].rsplit('/', 1)[-1]}",
					"status": "UPLOADED",
					"originalSource": {"url": entry["originalSource"]},
					"mediaErrors": [],
				}
				self.media.append(node)
				made.append(node)
			return {"productCreateMedia": {"media": made, "mediaUserErrors": []}}

		if "mediaIds" in variables:
			self.deleted.extend(variables["mediaIds"])
			self.media = [m for m in self.media if m["id"] not in variables["mediaIds"]]
			return {"productDeleteMedia": {"deletedMediaIds": variables["mediaIds"], "mediaUserErrors": []}}

		if "moves" in variables:
			self.moves.extend(variables["moves"])
			for move in variables["moves"]:
				node = next((m for m in self.media if m["id"] == move["id"]), None)
				if node:
					self.media.remove(node)
					self.media.insert(int(move["newPosition"]), node)
			return {
				"productReorderMedia": {
					"job": {"id": "gid://shopify/Job/1", "done": False},
					"mediaUserErrors": [],
				}
			}

		if "variantMedia" in variables:
			self.attached.extend(variables["variantMedia"])
			for entry in variables["variantMedia"]:
				self.variant_media.setdefault(entry["variantId"], []).extend(entry["mediaIds"])
			return {
				"productVariantAppendMedia": {
					"productVariants": [{"id": e["variantId"]} for e in variables["variantMedia"]],
					"userErrors": [],
				}
			}

		if "cursor" in variables:
			# The variant-media query, which is paginated and its own call -- asking for it
			# alongside the product's media is a connection inside a connection, and Shopify
			# charges the product of the two.
			return {
				"product": {
					"variants": {
						"pageInfo": {"hasNextPage": False, "endCursor": None},
						"edges": [
							{"node": {"id": gid, "media": {"nodes": [{"id": m} for m in ids]}}}
							for gid, ids in self.variant_media.items()
						],
					}
				}
			}

		return {"product": {"id": PRODUCT, "media": {"nodes": self.media}}}

	def paginate(self, query, variables, connection_path, *, cost_hint=0):
		"""The real client's paginator, in miniature: one page, read from `edges`."""
		page = dict(variables)
		page["cursor"] = None
		data = self.execute(query, page, cost_hint)
		connection = data["product"]["variants"]
		for edge in connection.get("edges") or []:
			yield edge["node"]

	def media_ids(self):
		return [m["id"] for m in self.media]


class MediaCase(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from shopify_integration.tests.test_integration import SECRET_A, make_store

		cls.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)
		frappe.db.set_value("Shopify Store", cls.store, "sync_item_images", 1)
		frappe.db.commit()
		cls.store_doc = frappe.get_cached_doc("Shopify Store", cls.store)

	def setUp(self):
		# The site is a localhost one, so public_url would refuse every file on it. The
		# question here is which files are chosen and what happens to their media, not how a
		# URL is built -- test_product_push covers that.
		patcher = patch.object(
			media_module, "public_url", side_effect=lambda file_url, subject: f"https://cdn.test{file_url}"
		)
		self.public_url_patcher = patcher
		self.public_url = patcher.start()
		self.addCleanup(patcher.stop)
		self.addCleanup(frappe.db.commit)

	def _item(self, image=None, variant_of=None, attributes=None):
		from shopify_integration.tests.test_integration import with_hsn

		code = f"ZZ-MEDIA-{frappe.generate_hash(length=6)}"
		item = frappe.new_doc("Item")
		item.item_code = code
		item.item_name = code
		item.item_group = frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0]
		item.stock_uom = "Nos"
		if variant_of:
			item.variant_of = variant_of
			for attribute, value in (attributes or {}).items():
				item.append("attributes", {"attribute": attribute, "attribute_value": value})
		with_hsn(item)
		item.flags.ignore_mandatory = True
		item.insert(ignore_permissions=True)
		if image:
			item.db_set("image", image, update_modified=False)
		self.addCleanup(self._forget, code)
		frappe.db.commit()
		return code

	def _attach(self, item_code, file_name, private=0):
		"""A real attachment. Frappe opens an uploaded .png with PIL, and gives two files with
		identical bytes the same file_url -- so each one here is a genuine image, and each is
		a different colour."""
		doc = frappe.new_doc("File")
		doc.file_name = file_name
		doc.attached_to_doctype = "Item"
		doc.attached_to_name = item_code
		doc.is_private = private
		doc.content = _png() if file_name.lower().endswith(IMAGE_EXTENSIONS) else _not_a_photograph()
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc

	def _link(self, item_code, product=PRODUCT, variant=None, owned=None, template=None):
		doc = frappe.new_doc("Shopify Item Link")
		doc.store = self.store
		doc.item_code = item_code
		doc.sku = item_code
		doc.product_gid = product
		doc.variant_gid = variant or f"gid://shopify/ProductVariant/{item_code}"
		doc.is_variant = 1 if template else 0
		doc.template_item = template
		doc.app_media = json.dumps(owned) if owned else None
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc

	def _forget(self, item_code):
		for name in frappe.get_all("Shopify Item Link", filters={"item_code": item_code}, pluck="name"):
			frappe.delete_doc("Shopify Item Link", name, force=True, ignore_permissions=True)
		frappe.db.delete("Shopify Sync Queue", {"ref_docname": item_code})
		frappe.db.delete("File", {"attached_to_doctype": "Item", "attached_to_name": item_code})


class TestWhatErpnextSaysTheProductShouldHave(MediaCase):
	def test_the_image_field_leads_and_attachments_follow_oldest_first(self):
		item = self._item(image="/files/main.png")
		second = self._attach(item, "second.png")
		third = self._attach(item, "third.jpg")

		self.assertEqual(desired_files(item), ["/files/main.png", second.file_url, third.file_url])

	def test_the_same_file_in_both_places_counts_once(self):
		"""The Image field is usually also an attachment. Sending it twice would give the
		storefront a duplicate of the main photograph."""
		item = self._item()
		attachment = self._attach(item, "only.png")
		frappe.db.set_value("Item", item, "image", attachment.file_url)

		self.assertEqual(desired_files(item), [attachment.file_url])

	def test_things_that_are_not_photographs_are_left_alone(self):
		item = self._item(image="/files/main.png")
		self._attach(item, "size-chart.txt")
		self._attach(item, "invoice.csv")

		self.assertEqual(desired_files(item), ["/files/main.png"])

	def test_a_private_file_is_skipped_with_a_reason(self):
		"""Frappe serves a private file only to a logged-in session, so Shopify would store
		the login page as the product photo."""
		self.public_url_patcher.stop()
		item = self._item()
		private = self._attach(item, "secret.png", private=1)

		with patch.object(media_module.frappe, "logger") as logger:
			self.assertIsNone(media_module.public_url(private.file_url, item))

		said = " ".join(str(c) for c in logger.return_value.info.call_args_list)
		self.assertIn("private file", said)
		self.public_url_patcher.start()

	def test_a_templates_plan_is_its_own_images_then_each_variants(self):
		attribute = _an_attribute()
		template = self._template(attribute)
		small = self._item(image="/files/small.png", variant_of=template, attributes={attribute: "Small"})
		large = self._item(image="/files/large.png", variant_of=template, attributes={attribute: "Large"})
		frappe.db.set_value("Item", template, "image", "/files/range.png")

		plan = media_plan(template)
		self.assertEqual(plan["order"][0], "/files/range.png", "the template's image is the featured one")
		self.assertEqual(sorted(plan["order"][1:]), ["/files/large.png", "/files/small.png"])
		self.assertEqual(plan["by_variant"][small], ["/files/small.png"])
		self.assertEqual(plan["by_variant"][large], ["/files/large.png"])

	def _template(self, attribute):
		from shopify_integration.tests.test_integration import with_hsn

		code = f"ZZ-MEDIA-TPL-{frappe.generate_hash(length=6)}"
		doc = frappe.new_doc("Item")
		doc.item_code = code
		doc.item_name = code
		doc.item_group = frappe.get_all("Item Group", filters={"is_group": 0}, limit=1, pluck="name")[0]
		doc.stock_uom = "Nos"
		doc.has_variants = 1
		doc.append("attributes", {"attribute": attribute})
		with_hsn(doc)
		doc.insert(ignore_permissions=True)
		self.addCleanup(self._forget, code)
		frappe.db.commit()
		return code


def _an_attribute(name="ZZ Media Size", values=("Small", "Large")):
	if not frappe.db.exists("Item Attribute", name):
		doc = frappe.new_doc("Item Attribute")
		doc.attribute_name = name
		for value in values:
			doc.append("item_attribute_values", {"attribute_value": value, "abbr": value[:3]})
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
	return name


class TestSendingThem(MediaCase):
	def test_three_attachments_become_three_media_with_the_main_one_featured(self):
		item = self._item(image="/files/main.png")
		second = self._attach(item, "b.png")
		third = self._attach(item, "c.png")
		link = self._link(item)
		client = _Client()

		result = sync_item_media(client, self.store_doc, item)

		self.assertEqual(result["added"], 3)
		self.assertEqual(
			[m["originalSource"]["url"] for m in client.media],
			["https://cdn.test" + u for u in ("/files/main.png", second.file_url, third.file_url)],
		)
		self.assertEqual(client.media_ids()[0], media_id("/files/main.png"), "featured first")
		self.assertEqual(
			sorted(owned_media(link.name)), sorted(["/files/main.png", second.file_url, third.file_url])
		)

	def test_deleting_an_attachment_deletes_its_media(self):
		item = self._item(image="/files/main.png")
		going = self._attach(item, "going.png")
		link = self._link(item)
		client = _Client()
		sync_item_media(client, self.store_doc, item)
		self.assertEqual(len(client.media), 2)

		frappe.delete_doc("File", going.name, force=True, ignore_permissions=True)
		frappe.db.commit()
		sync_item_media(client, self.store_doc, item)

		self.assertEqual(client.deleted, [media_id(going.file_url)])
		self.assertEqual(client.media_ids(), [media_id("/files/main.png")])
		self.assertNotIn(going.file_url, owned_media(link.name))

	def test_clearing_the_image_field_removes_only_that_one(self):
		item = self._item(image="/files/main.png")
		keep = self._attach(item, "keep.png")
		link = self._link(item)
		client = _Client()
		sync_item_media(client, self.store_doc, item)

		frappe.db.set_value("Item", item, "image", None)
		sync_item_media(client, self.store_doc, item)

		self.assertEqual(client.deleted, [media_id("/files/main.png")])
		self.assertEqual(client.media_ids(), [media_id(keep.file_url)])
		self.assertEqual(list(owned_media(link.name)), [keep.file_url])

	def test_photography_the_merchant_added_is_never_deleted(self):
		"""The line the whole feature turns on. Media the app did not create is not in its
		record, so it is not a candidate for deletion -- even when ERPNext has no images at
		all and every one of the app's own is going."""
		item = self._item(image="/files/main.png")
		link = self._link(item)
		client = _Client(
			[{"id": THEIRS, "status": "READY", "originalSource": {"url": "https://merchant/x.jpg"}}]
		)
		sync_item_media(client, self.store_doc, item)

		frappe.db.set_value("Item", item, "image", None)
		sync_item_media(client, self.store_doc, item)

		self.assertNotIn(THEIRS, client.deleted)
		self.assertEqual(client.media_ids(), [THEIRS])
		self.assertEqual(owned_media(link.name), {})

	def test_media_already_sent_is_not_sent_again(self):
		item = self._item(image="/files/main.png")
		self._link(item)
		client = _Client()
		sync_item_media(client, self.store_doc, item)
		sent_once = len(client.created)

		sync_item_media(client, self.store_doc, item)

		self.assertEqual(len(client.created), sent_once, "a second sync must upload nothing")
		self.assertEqual(client.deleted, [])

	def test_a_changed_main_image_is_moved_to_the_front(self):
		item = self._item(image="/files/a.png")
		promoted = self._attach(item, "b.png")
		self._link(item)
		client = _Client()
		sync_item_media(client, self.store_doc, item)

		# b.png is promoted to the Item's Image field; it is already on the product.
		frappe.db.set_value("Item", item, "image", promoted.file_url)
		client.moves.clear()
		sync_item_media(client, self.store_doc, item)

		self.assertEqual(client.moves, [{"id": media_id(promoted.file_url), "newPosition": "0"}])
		self.assertEqual(client.media_ids()[0], media_id(promoted.file_url))

	def test_a_store_with_image_sync_off_is_not_touched(self):
		item = self._item(image="/files/main.png")
		self._link(item)
		frappe.db.set_value("Shopify Store", self.store, "sync_item_images", 0)
		frappe.clear_document_cache("Shopify Store", self.store)
		self.addCleanup(frappe.db.set_value, "Shopify Store", self.store, "sync_item_images", 1)
		self.addCleanup(frappe.clear_document_cache, "Shopify Store", self.store)
		client = _Client()

		result = sync_item_media(client, frappe.get_cached_doc("Shopify Store", self.store), item)

		self.assertIn("skipped", result)
		self.assertEqual(client.created, [])


class TestVariants(MediaCase):
	def test_a_variants_image_goes_to_the_variant_and_the_product(self):
		attribute = _an_attribute()
		template = TestWhatErpnextSaysTheProductShouldHave._template(self, attribute)
		variant = self._item(image="/files/small.png", variant_of=template, attributes={attribute: "Small"})
		frappe.db.set_value("Item", template, "image", "/files/range.png")
		self._link(template)
		self._link(variant, variant="gid://shopify/ProductVariant/small", template=template)
		client = _Client()

		sync_item_media(client, self.store_doc, variant)

		self.assertEqual(
			client.media_ids(),
			["gid://shopify/MediaImage/range.png", "gid://shopify/MediaImage/small.png"],
			"both are on the product, the template's first",
		)
		self.assertEqual(
			client.attached,
			[
				{
					"variantId": "gid://shopify/ProductVariant/small",
					"mediaIds": ["gid://shopify/MediaImage/small.png"],
				}
			],
			"and only the variant's own is attached to the variant",
		)

	def test_it_is_not_attached_twice(self):
		attribute = _an_attribute()
		template = TestWhatErpnextSaysTheProductShouldHave._template(self, attribute)
		variant = self._item(image="/files/small.png", variant_of=template, attributes={attribute: "Small"})
		self._link(template)
		self._link(variant, variant="gid://shopify/ProductVariant/small", template=template)
		client = _Client()

		sync_item_media(client, self.store_doc, variant)
		client.attached.clear()
		sync_item_media(client, self.store_doc, variant)

		self.assertEqual(client.attached, [])


class TestWhenShopifyCannotProcessIt(MediaCase):
	def test_a_refused_upload_says_why(self):
		item = self._item(image="/files/main.png")
		self._link(item)

		with self.assertRaises(MediaSyncError) as caught:
			sync_item_media(_Client(fail="refuse"), self.store_doc, item)

		self.assertIn("Invalid image source", str(caught.exception))

	def test_media_that_failed_processing_is_reported_and_taken_down(self):
		"""Shopify processes media asynchronously, so an image it cannot decode fails after
		the mutation returned no errors at all. Leaving it up shows the customer a blank
		where a photograph should be, and leaving it recorded means it is never retried."""
		item = self._item(image="/files/main.png")
		link = self._link(item)
		client = _Client()
		sync_item_media(client, self.store_doc, item)

		client.media[0]["status"] = "FAILED"
		client.media[0]["mediaErrors"] = [
			{"code": "IMAGE_DOWNLOAD_FAILURE", "details": None, "message": "Could not download image"}
		]
		result = sync_item_media(client, self.store_doc, item)

		self.assertTrue(any("Could not download image" in f for f in result["failures"]))
		self.assertIn(media_id("/files/main.png"), client.deleted)
		self.assertNotIn("/files/main.png", owned_media(link.name))

	def test_the_queue_row_carries_the_reason(self):
		from shopify_integration.api.client import ShopifyClient
		from shopify_integration.outbound.media import push_media

		item = self._item(image="/files/main.png")
		self._link(item)
		client = _Client()
		sync_item_media(client, self.store_doc, item)
		client.media[0]["status"] = "FAILED"
		client.media[0]["mediaErrors"] = [{"code": "IMAGE_PROCESSING_FAILURE", "message": "Corrupt file"}]

		from shopify_integration.exceptions import PartialFailure

		with (
			patch.object(ShopifyClient, "for_store", return_value=client),
			self.assertRaises(PartialFailure) as caught,
		):
			push_media(self.store, [{"name": "ROW-1", "ref_docname": item}])

		# Per row, so one product's bad image cannot fail every other product claimed with
		# it -- and the engine writes each row's own exception to its own last_error.
		failed = caught.exception.failures["ROW-1"]
		self.assertIsInstance(failed, MediaSyncError)
		self.assertIn("Corrupt file", str(failed))

	def test_the_product_ceiling_is_respected(self):
		item = self._item(image="/files/main.png")
		self._link(item)
		theirs = [
			{
				"id": f"gid://shopify/MediaImage/theirs-{n}",
				"status": "READY",
				"originalSource": {"url": f"m{n}"},
			}
			for n in range(media_module.MAX_MEDIA_PER_PRODUCT)
		]
		client = _Client(theirs)

		result = sync_item_media(client, self.store_doc, item)

		self.assertEqual(client.created, [], "there is no room, and nothing of theirs may go")
		self.assertTrue(any("limit" in f for f in result["failures"]))


class TestTheTriggers(MediaCase):
	def test_attaching_an_image_queues_the_product(self):
		item = self._item()
		self._link(item)
		frappe.db.delete("Shopify Sync Queue", {"store": self.store})

		with patch("shopify_integration.sync.engine.schedule_drain"):
			self._attach(item, "new.png")

		self.assertEqual(
			frappe.get_all(
				"Shopify Sync Queue",
				filters={"store": self.store, "operation": "media", "ref_docname": item},
				pluck="dedupe_key",
			),
			[f"media:{self.store}:{item}"],
		)

	def test_a_variants_attachment_queues_its_product_not_the_variant(self):
		attribute = _an_attribute()
		template = TestWhatErpnextSaysTheProductShouldHave._template(self, attribute)
		variant = self._item(variant_of=template, attributes={attribute: "Small"})
		self._link(template)
		self._link(variant, variant="gid://shopify/ProductVariant/small", template=template)
		frappe.db.delete("Shopify Sync Queue", {"store": self.store})

		with patch("shopify_integration.sync.engine.schedule_drain"):
			self._attach(variant, "small.png")

		self.assertEqual(
			frappe.get_all(
				"Shopify Sync Queue", filters={"store": self.store, "operation": "media"}, pluck="ref_docname"
			),
			[template],
			"one row for the product, however many variants changed",
		)

	def test_saving_an_item_without_touching_its_image_queues_nothing(self):
		"""Every other save -- a weight, a description, a reorder level -- must cost nothing."""
		item = self._item(image="/files/main.png")
		self._link(item)
		frappe.db.delete("Shopify Sync Queue", {"store": self.store})
		frappe.db.commit()

		doc = frappe.get_doc("Item", item)
		doc.description = "a new description"
		with patch("shopify_integration.sync.engine.schedule_drain"):
			doc.save(ignore_permissions=True)

		self.assertEqual(
			frappe.get_all(
				"Shopify Sync Queue", filters={"store": self.store, "operation": "media"}, pluck="name"
			),
			[],
		)

	def test_changing_the_image_field_queues_it(self):
		item = self._item(image="/files/main.png")
		self._link(item)
		frappe.db.delete("Shopify Sync Queue", {"store": self.store})
		frappe.db.commit()

		doc = frappe.get_doc("Item", item)
		doc.image = "/files/other.png"
		with patch("shopify_integration.sync.engine.schedule_drain"):
			doc.save(ignore_permissions=True)

		self.assertEqual(
			frappe.get_all(
				"Shopify Sync Queue", filters={"store": self.store, "operation": "media"}, pluck="ref_docname"
			),
			[item],
		)

	def test_an_unlinked_item_queues_nothing(self):
		"""Most of an 87,000-item catalogue was never published. Attaching a photograph to
		one of those must cost one indexed read and nothing more."""
		item = self._item()
		frappe.db.delete("Shopify Sync Queue", {"store": self.store})

		with patch("shopify_integration.sync.engine.schedule_drain"):
			self._attach(item, "new.png")

		self.assertEqual(
			frappe.get_all("Shopify Sync Queue", filters={"operation": "media"}, pluck="name"), []
		)

	def test_the_operation_has_a_handler(self):
		from shopify_integration.sync.engine import OPERATION_HANDLERS

		self.assertEqual(OPERATION_HANDLERS["media"], "shopify_integration.outbound.media.push_media")


class TestItDoesNotLoopBack(MediaCase):
	def test_an_inbound_product_write_never_touches_the_item_image(self):
		"""Uploading media makes Shopify fire products/update straight back. If that echo
		wrote the Item's image, every sync would provoke the next one."""
		import inspect

		from shopify_integration.catalogue import mapping

		source = inspect.getsource(mapping)
		self.assertNotIn(
			'"image"',
			source,
			"the inbound mapping must not write Item.image, or media sync would loop",
		)

	def test_an_echoed_item_save_queues_nothing(self):
		from shopify_integration.catalogue.echo import mark

		item = self._item(image="/files/main.png")
		self._link(item)
		frappe.db.delete("Shopify Sync Queue", {"store": self.store})
		frappe.db.commit()

		doc = frappe.get_doc("Item", item)
		doc.image = "/files/from-shopify.png"
		mark(doc)
		with patch("shopify_integration.sync.engine.schedule_drain"):
			doc.save(ignore_permissions=True)

		self.assertEqual(
			frappe.get_all(
				"Shopify Sync Queue", filters={"store": self.store, "operation": "media"}, pluck="name"
			),
			[],
		)


class TestCarryingTheOldRecordOver(MediaCase):
	"""`app_media_gids` plus `image_synced_url` held one image between them. The patch has to
	turn that into the per-file map without losing which media the app owns -- getting it
	wrong either orphans a media id (so the app stops managing an image it uploaded) or
	claims one it did not create."""

	def _legacy(self, item_code, gids, url):
		link = self._link(item_code)
		frappe.db.sql(
			"""UPDATE `tabShopify Item Link`
			   SET app_media_gids = %s, image_synced_url = %s, app_media = NULL
			   WHERE name = %s""",
			("\n".join(gids), url, link.name),
		)
		frappe.db.commit()
		return link

	def test_the_one_image_it_knew_about_is_carried_over(self):
		from shopify_integration.patches.v0_1.migrate_app_media import execute

		item = self._item()
		link = self._legacy(item, ["gid://shopify/MediaImage/1"], f"{frappe.utils.get_url()}/files/saree.png")

		execute()

		self.assertEqual(owned_media(link.name), {"/files/saree.png": "gid://shopify/MediaImage/1"})

	def test_an_encoded_filename_comes_back_as_the_file_url(self):
		from shopify_integration.patches.v0_1.migrate_app_media import execute

		item = self._item()
		link = self._legacy(
			item, ["gid://shopify/MediaImage/2"], f"{frappe.utils.get_url()}/files/A%20B%2C%20C.png"
		)

		execute()

		self.assertEqual(list(owned_media(link.name)), ["/files/A B, C.png"])

	def test_extra_gids_are_left_on_the_product_rather_than_guessed_at(self):
		"""Several gids with one URL between them gives no way to tell which came from which
		file. Claiming the wrong one would let a later sync delete somebody's photograph."""
		from shopify_integration.patches.v0_1.migrate_app_media import execute

		item = self._item()
		link = self._legacy(
			item,
			["gid://shopify/MediaImage/3", "gid://shopify/MediaImage/4"],
			f"{frappe.utils.get_url()}/files/one.png",
		)

		execute()

		self.assertEqual(owned_media(link.name), {"/files/one.png": "gid://shopify/MediaImage/3"})

	def test_a_link_that_never_had_an_image_is_left_alone(self):
		from shopify_integration.patches.v0_1.migrate_app_media import execute

		item = self._item()
		link = self._link(item)

		execute()

		self.assertEqual(owned_media(link.name), {})
