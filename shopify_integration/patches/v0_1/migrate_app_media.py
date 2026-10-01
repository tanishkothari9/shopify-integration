"""Carry the one-image record over to the per-file one.

`app_media_gids` held the media ids the app had created and `image_synced_url` the URL it
last sent -- enough when an Item had one image, and nothing once it has several. The new
`app_media` maps each file to its media id, which is what lets an attachment be deleted on
its own.

The old pair cannot be mapped exactly: several gids with one URL between them gives no way
to tell which gid came from which file. The first is the one the URL belongs to -- it is
the one the old code had just created for it -- and any others are dropped from the record,
which leaves them on the product as the merchant's. That is the safe direction: an image
left in place is a cosmetic duplicate the next sync tidies, and an image wrongly deleted is
somebody's photograph gone.
"""

import json
from urllib.parse import unquote

import frappe


def execute():
	if not frappe.db.has_column("Shopify Item Link", "app_media_gids"):
		return

	rows = frappe.db.sql(
		"""SELECT name, app_media_gids, image_synced_url
		   FROM `tabShopify Item Link`
		   WHERE app_media_gids IS NOT NULL AND app_media_gids != ''""",
		as_dict=True,
	)

	migrated = 0
	for row in rows:
		gids = [line.strip() for line in (row.app_media_gids or "").splitlines() if line.strip()]
		url = (row.image_synced_url or "").strip()
		if not gids or not url:
			continue

		# Back to the file_url the Item stores, since that is what app_media is keyed on.
		# An absolute URL from another host is not ours to key on and is left behind.
		file_url = url
		for prefix in (frappe.utils.get_url().rstrip("/"), ""):
			if prefix and url.startswith(prefix):
				file_url = unquote(url[len(prefix) :])
				break
		if not file_url.startswith("/"):
			continue

		frappe.db.set_value(
			"Shopify Item Link",
			row.name,
			"app_media",
			json.dumps({file_url: gids[0]}, indent=0, sort_keys=True),
			update_modified=False,
		)
		migrated += 1

	frappe.db.commit()
	print(f"shopify_integration: carried {migrated} image record(s) over to app_media")
