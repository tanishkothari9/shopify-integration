"""Carry ``sync_item_titles`` onto ``sync_website_content``, and switch it on.

The old setting was a warning label: "Let ERPNext Overwrite Shopify Titles and Descriptions",
off by default, with a description telling you to think hard before using it. That was right
while Shopify held the storefront copy and ERPNext held only a plain item name.

It is no longer right. ERPNext is now the master for website content -- the Website (Shopify)
section on the Item carries the title, description, SEO, handle, tags, category, metafields,
collections and image alt text, written there by the merchant's own tooling. So the setting
becomes "Sync Website Content From ERPNext", and it is on.

Turning it on for existing stores is a deliberate behaviour change and the point of the
change: from here, what ERPNext holds is what the storefront shows. A store that would rather
keep writing its copy in the Shopify admin can switch it off again.
"""

import frappe


def execute():
	table = "tabShopify Store"
	columns = {column.Field for column in frappe.db.sql(f"DESCRIBE `{table}`", as_dict=True)}

	if "sync_item_titles" in columns:
		if "sync_website_content" in columns:
			frappe.db.sql(f"UPDATE `{table}` SET sync_website_content = sync_item_titles")
			frappe.db.sql_ddl(f"ALTER TABLE `{table}` DROP COLUMN `sync_item_titles`")
		else:
			frappe.db.sql_ddl(
				f"ALTER TABLE `{table}` CHANGE `sync_item_titles` `sync_website_content` "
				"int(1) NOT NULL DEFAULT 1"
			)
		columns = {column.Field for column in frappe.db.sql(f"DESCRIBE `{table}`", as_dict=True)}

	if "sync_website_content" not in columns:
		return

	switched = frappe.db.sql(
		f"SELECT COUNT(*) FROM `{table}` WHERE IFNULL(sync_website_content, 0) = 0"
	)[0][0]
	if switched:
		frappe.db.sql(f"UPDATE `{table}` SET sync_website_content = 1")
		frappe.db.commit()
		print(
			f"shopify_integration: ERPNext is now the master for website content; switched it "
			f"on for {switched} store(s)"
		)
