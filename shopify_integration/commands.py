"""Bench commands.

Operational commands an administrator runs deliberately, from a terminal.
"""

from __future__ import annotations

import json

import click
import frappe
from frappe.commands import get_site, pass_context


@click.command("shopify-reconcile")
@click.option("--store", help="One store, or omit for every enabled store.")
@pass_context
def shopify_reconcile(context, store):
	"""Run the drift check now instead of waiting for tonight."""
	from shopify_integration.sync import reconcile

	site = get_site(context)
	frappe.init(site=site)
	frappe.connect()
	try:
		result = reconcile.reconcile_store(store) if store else reconcile.reconcile_all_stores()
		click.echo(json.dumps(result, indent=2, default=str))
	finally:
		frappe.destroy()


commands = [
	shopify_reconcile,
]
