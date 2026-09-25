"""Carry the old ``match_customers_by_mobile`` value onto ``match_existing_customers``.

The setting was added matching on the mobile alone, then widened to try the buyer's email when
the mobile finds nobody, so the old name described only half of it. A rename loses whatever the
merchant chose unless the value is carried over first -- and losing it silently would turn the
setting back on for anyone who had deliberately switched it off.
"""

import frappe


def execute():
	table = "tabShopify Store"
	columns = {column.Field for column in frappe.db.sql(f"DESCRIBE `{table}`", as_dict=True)}

	if "match_customers_by_mobile" not in columns:
		return

	if "match_existing_customers" in columns:
		frappe.db.sql(f"UPDATE `{table}` SET match_existing_customers = match_customers_by_mobile")
		frappe.db.sql_ddl(f"ALTER TABLE `{table}` DROP COLUMN `match_customers_by_mobile`")
	else:
		frappe.db.sql_ddl(
			f"ALTER TABLE `{table}` CHANGE `match_customers_by_mobile` `match_existing_customers` int(1) NOT NULL DEFAULT 1"
		)
