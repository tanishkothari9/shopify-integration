app_name = "shopify_integration"
app_title = "Shopify Integration"
app_publisher = "Tanish Kothari"
app_description = "Multi-store, GraphQL-first Shopify integration for ERPNext with real-time inventory."
app_email = "tanishkothari324@gmail.com"
app_license = "GPL-3.0-or-later"

required_apps = ["frappe/erpnext"]

# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------
# Both entries are safety nets, not the primary path. Sync is event-driven; these exist so
# that a worker restart or a dropped job degrades into a delay rather than into lost work.
scheduler_events = {
	"cron": {
		# Return rows orphaned by a killed worker to Pending (spec §7.2).
		"*/10 * * * *": ["shopify_integration.sync.engine.recover_stale_running"],
		# Nudge any store with pending rows, in case an enqueued drain was lost. Every minute
		# rather than every fifteen: a drain now reschedules itself while work remains, so this
		# is a true safety net, and the longest a dropped signal can strand a push is how long
		# a customer waits to see stock they can buy.
		"* * * * *": ["shopify_integration.sync.engine.drain_all_stores"],
		# Re-poll bulk operations whose poll job was lost, so an import cannot stall at
		# Running forever with its results never applied.
		"*/5 * * * *": ["shopify_integration.api.bulk.poll_running_operations"],
		# Re-run inbound handlers that failed and are due another attempt. The outbound queue
		# has always retried; the inbound side never did, so a webhook that failed for any
		# reason at all stayed failed until a person opened the log.
		"*/2 * * * *": ["shopify_integration.inbound.webhook.retry_failed_events"],
		# Nightly drift check. Not a safety net like the others but a feature in its own
		# right: it is what lets an operator trust the integration rather than watch it.
		"0 3 * * *": ["shopify_integration.sync.reconcile.reconcile_all_stores"],
	}
}

# ---------------------------------------------------------------------------
# Document events
# ---------------------------------------------------------------------------
# Deliberately empty in phase 1. Outbound triggers arrive with the features that need them
# (spec §9.1): Stock Ledger Entry and Sales Order in phase 4, Item in phase 2, Item Price in
# phase 5. Every handler added here must do exactly one thing -- compute a dedupe key and
# call enqueue_sync() -- because these run inside the user's save transaction, including at
# the POS counter.
#
# Note for whoever adds them: do NOT hook Bin. ERPNext v15 updates it via bin.db_update(),
# a direct database write that fires no doc events, so the hook would silently never run.
#
# The Item hooks below are also the live half of echo suppression: on_item_change returns
# immediately for anything an inbound handler wrote. Removing that check loops forever.
doc_events: dict = {
	"Item": {
		"after_insert": [
			"shopify_integration.outbound.product.on_item_change",
			"shopify_integration.outbound.price.on_item_change",
		],
		"on_update": [
			"shopify_integration.outbound.product.on_item_change",
			# Its photographs, when the Image field moves. Attachments come through the File
			# events below instead -- saving the Item is not what adds one.
			"shopify_integration.outbound.media.on_item_change",
			# The Item carries a price too -- Standard Selling Rate is where ERPNext puts a
			# rate typed on the form. Without this it was readable and never sent.
			"shopify_integration.outbound.price.on_item_change",
			# Its group or its own tax rows decide which tax band it falls in, and the band
			# decides which Shopify collection carries the right override.
			"shopify_integration.outbound.collections.on_item_change",
		],
	},
	# Staff keep a product's other photographs as attachments on the Item. Adding or
	# removing one is a File event and nothing else: the Item is never saved for it, so
	# without these the extra images would reach Shopify only if something else happened to
	# touch the item.
	"File": {
		"after_insert": "shopify_integration.outbound.media.on_file_change",
		"on_trash": "shopify_integration.outbound.media.on_file_change",
	},
	# A group's tax rules move every published item under it, which is why this is queued
	# per item rather than done in the save.
	"Item Group": {
		"on_update": "shopify_integration.outbound.collections.on_item_group_change",
	},
	# Every physical stock movement: POS, Delivery Note, Stock Entry, Purchase Receipt,
	# Reconciliation. ERPNext creates SLEs via sle.submit(), so doc events do fire here.
	"Stock Ledger Entry": {
		"on_submit": "shopify_integration.outbound.inventory.on_stock_movement",
	},
	# Selling price changes on the store's own price list.
	"Item Price": {
		"on_change": [
			"shopify_integration.outbound.price.on_price_change",
			# And the price is what decides the tax band on a banded catalogue.
			"shopify_integration.outbound.collections.on_price_change",
		],
	},
	# reserved_qty changes, which no stock ledger entry reflects. Without this, a web order
	# would leave its units looking sellable until the delivery note was made.
	"Sales Order": {
		"on_submit": "shopify_integration.outbound.inventory.on_reservation_change",
		"on_cancel": "shopify_integration.outbound.inventory.on_reservation_change",
		# Submit and cancel are not the only ways reserved_qty moves. Closing or re-opening an
		# order frees or re-takes its units, and Update Items changes them on an order already
		# submitted -- neither is a submit or a cancel, so both used to leave Shopify short (or
		# long) until the 03:00 reconciliation noticed.
		#
		# Two events because they are reached differently: Close and Re-open go through
		# `update_status`, which writes with `db_set` and so runs `on_change` but never
		# `on_update_after_submit`; Update Items calls `parent.save()` on a submitted document,
		# which runs `on_update_after_submit` but not `on_change`. Both land in the same
		# handler, and the queue's dedupe key collapses whatever overlaps.
		"on_change": "shopify_integration.outbound.inventory.on_reservation_change",
		"on_update_after_submit": "shopify_integration.outbound.inventory.on_reservation_change",
	},
	# What shipped. Marks the Shopify order fulfilled, which is what sends the customer their
	# dispatch email -- so it is behind a store setting that starts off.
	"Delivery Note": {
		"on_submit": "shopify_integration.outbound.fulfillment.on_delivery_note_submit",
	},
	# Who is carrying it, and under what number. ERPNext records this separately from the
	# Delivery Note, and so does Shopify.
	"Shipment": {
		"on_submit": "shopify_integration.outbound.fulfillment.on_shipment_submit",
	},
}

after_install = "shopify_integration.install.after_install"
after_migrate = ["shopify_integration.install.after_migrate"]

fixtures: list = []

# Bench commands: bench --site <site> shopify-reconcile.
# See shopify_integration/commands.py.

# Every accepted webhook stores its raw body, and order and customer payloads carry names,
# emails, phone numbers and addresses. Without this the table grows for ever and the personal
# data in it is kept indefinitely -- neither is defensible for something a merchant installs.
#
# 90 days is chosen deliberately, not as a round number: replay protection is the uniqueness of
# `webhook_id` in this table, so purging a row makes that webhook replayable. Shopify retries a
# failed delivery for about 48 hours, so anything past a few days is long outside the window a
# captured request could be replayed in.
#
# A site can change this in Log Settings; it is a default, not a lock.
default_log_clearing_doctypes = {
	"Shopify Event Log": 90,
}
