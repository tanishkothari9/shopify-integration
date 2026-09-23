"""Integration tests against a real site (spec §16).

These cover the phase-1 entries from the spec's mandatory test list -- the ones that need a
database because they are about idempotency, claim semantics and multi-store isolation
rather than pure logic.

Run with:
    bench --site <site> run-tests --app shopify_integration
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from shopify_integration.sync import engine

SECRET_A = "secret-store-a"
SECRET_B = "secret-store-b"


def sign(body: bytes, secret: str) -> str:
	return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def ensure_erpnext_masters() -> None:
	"""Install ERPNext's standard masters if the site has none.

	`bench new-site --install-app erpnext` does not run the setup wizard, so a fresh site has
	zero Item Groups and zero UOMs -- and CI builds exactly such a site. Calling ERPNext's own
	fixture installer gives the tests a site that resembles a real one, rather than a
	hand-rolled approximation that could drift from what ERPNext actually ships.
	"""
	if frappe.db.count("Item Group"):
		return

	from erpnext.setup.setup_wizard.operations.install_fixtures import install

	install(country="United States")
	frappe.db.commit()


def with_hsn(item):
	"""Give a bare test Item an HSN code when the site demands one.

	`india_compliance` makes HSN/SAC mandatory on every sales item, so a test that builds an
	Item by hand fails on an Indian site and nowhere else. A no-op everywhere the field does
	not exist.
	"""
	if item.meta.has_field("gst_hsn_code") and not item.get("gst_hsn_code"):
		if not frappe.db.exists("GST HSN Code", "61091000"):
			frappe.get_doc(
				{"doctype": "GST HSN Code", "hsn_code": "61091000", "description": "Test goods"}
			).insert(ignore_permissions=True, ignore_if_duplicate=True)
		item.gst_hsn_code = "61091000"
	return item


def ensure_company() -> str:
	"""Create a Company if the site has none.

	A site built by `bench new-site --install-app erpnext` has zero Companies until the setup
	wizard runs, and CI builds exactly such a site. Depending on ambient site state would make
	these tests pass locally and fail in CI, so they create what they need.

	Creating a Company makes ERPNext build its default warehouse tree, which includes a
	Transit warehouse -- and that needs the "Transit" Warehouse Type, another record the setup
	wizard would normally have created. Seed it first or the insert fails on a link.
	"""
	ensure_erpnext_masters()

	existing = frappe.get_all("Company", limit=1, pluck="name")
	if existing:
		ensure_company_accounting(existing[0])
		return existing[0]

	if not frappe.db.exists("Warehouse Type", "Transit"):
		frappe.get_doc({"doctype": "Warehouse Type", "name": "Transit"}).insert(
			ignore_permissions=True, ignore_if_duplicate=True
		)

	company = frappe.new_doc("Company")
	company.company_name = "Shopify Test Co"
	company.abbr = "STC"
	company.default_currency = "USD"
	company.country = "United States"
	company.insert(ignore_permissions=True)
	frappe.db.commit()

	ensure_company_accounting(company.name)
	return company.name


def ensure_company_accounting(company: str) -> None:
	"""Finish the accounting setup the wizard would normally have done.

	Creating a Company on a site with no masters leaves it half-configured: a chart of
	accounts but no cost centres, no default income/receivable accounts, and no fiscal year.
	Any document that posts to the ledger then fails on whichever piece is missing. Ordering
	matters -- cost centres first, then defaults, then the fiscal year.
	"""
	if not frappe.db.exists("Cost Center", {"company": company, "is_group": 0}):
		frappe.get_doc("Company", company).create_default_cost_center()
		frappe.db.commit()

	leaf = frappe.db.get_value("Cost Center", {"company": company, "is_group": 0}, "name")
	if leaf and not frappe.db.get_value("Company", company, "cost_center"):
		frappe.db.set_value("Company", company, "cost_center", leaf)
		frappe.db.set_value("Company", company, "round_off_cost_center", leaf)
		frappe.db.commit()

	if not frappe.db.get_value("Company", company, "default_income_account"):
		doc = frappe.get_doc("Company", company)
		doc.update_default_account = True
		doc.set_default_accounts()
		frappe.db.commit()

	ensure_fiscal_year()
	frappe.clear_cache()


def ensure_fiscal_year() -> None:
	"""A fiscal year covering today, so ledger postings have somewhere to land."""
	today = frappe.utils.getdate(frappe.utils.nowdate())
	if frappe.db.exists("Fiscal Year", {"year_start_date": ["<=", today], "year_end_date": [">=", today]}):
		return

	year = frappe.new_doc("Fiscal Year")
	year.year = str(today.year)
	year.year_start_date = f"{today.year}-01-01"
	year.year_end_date = f"{today.year}-12-31"
	year.flags.ignore_mandatory = True
	year.insert(ignore_permissions=True)
	frappe.db.commit()


def make_store(name: str, domain: str, secret: str) -> str:
	if frappe.db.exists("Shopify Store", name):
		# on_trash refuses to delete an enabled store, so that disabling always gets a chance
		# to remove its webhooks. Clear the flag in the database rather than via the document,
		# which would enqueue a deregistration call to Shopify.
		frappe.db.set_value("Shopify Store", name, "enabled", 0)
		frappe.delete_doc("Shopify Store", name, force=True)

	doc = frappe.new_doc("Shopify Store")
	doc.store_name = name
	doc.shop_domain = domain
	doc.api_version = "2026-01"
	doc.admin_api_token = "shpat_EXAMPLE_NOT_A_REAL_TOKEN"
	doc.api_secret = secret
	doc.company = ensure_company()
	# Harmless everywhere, and required on a site with india_compliance: without an HSN code a
	# newly created Item cannot be saved at all, so every test that imports a product would
	# fail there and nowhere else.
	doc.default_hsn_code = "61091000"
	# Insert disabled, then enable without the on_update webhook registration firing at
	# Shopify -- these tests never touch the network.
	doc.enabled = 0
	doc.insert(ignore_permissions=True)
	frappe.db.set_value("Shopify Store", doc.name, "enabled", 1)
	frappe.db.commit()
	return doc.name


class TestWebhookReceiver(FrappeTestCase):
	"""The publicly reachable endpoint (spec §8.1)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.store_a = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)
		cls.store_b = make_store("Test Store B", "test-b.myshopify.com", SECRET_B)

	def setUp(self):
		frappe.db.delete("Shopify Event Log")
		frappe.db.delete("Shopify Sync Queue")
		frappe.db.commit()

	# Also on the way out: the receiver commits what it logs, so clearing only on the way in
	# leaves the final test's rows sitting in the Event Log list afterwards.
	tearDown = setUp

	def post(
		self,
		body: bytes,
		*,
		domain: str,
		secret: str,
		topic: str,
		webhook_id: str,
		signature: str | None = None,
	):
		"""Drive the receiver the way Shopify does, through a faked request object."""
		from shopify_integration.inbound import webhook as receiver

		headers = {
			receiver.HEADER_DOMAIN: domain,
			receiver.HEADER_TOPIC: topic,
			receiver.HEADER_WEBHOOK_ID: webhook_id,
			receiver.HEADER_HMAC: signature if signature is not None else sign(body, secret),
		}

		class FakeRequest:
			def __init__(self):
				self.headers = headers

			def get_data(self):
				return body

		frappe.local.response = frappe._dict()
		with patch.object(frappe.local, "request", FakeRequest(), create=True):
			result = receiver.webhook()
		return frappe.local.response.get("http_status_code", 200), result

	def test_valid_webhook_is_logged(self):
		body = json.dumps({"id": 1001, "name": "#1001"}).encode()
		status, _ = self.post(
			body,
			domain="test-a.myshopify.com",
			secret=SECRET_A,
			topic="orders/create",
			webhook_id="wh-001",
		)
		self.assertEqual(status, 200)

		logs = frappe.get_all("Shopify Event Log", filters={"webhook_id": "wh-001"}, fields=["*"])
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].store, self.store_a)
		self.assertEqual(logs[0].topic, "orders/create")

	def test_duplicate_delivery_produces_exactly_one_log(self):
		"""Shopify retries for ~48h; duplicates are normal traffic, not an error."""
		body = json.dumps({"id": 1002}).encode()
		args = dict(
			domain="test-a.myshopify.com", secret=SECRET_A, topic="orders/create", webhook_id="wh-dup"
		)

		first_status, _ = self.post(body, **args)
		second_status, second = self.post(body, **args)

		self.assertEqual(first_status, 200)
		self.assertEqual(second_status, 200)
		self.assertEqual(second["message"], "Duplicate")
		self.assertEqual(frappe.db.count("Shopify Event Log", {"webhook_id": "wh-dup"}), 1)

	def test_raw_body_is_stored_verbatim_for_replay(self):
		body = b'{"id":1003,  "name":"#1003"}'
		self.post(
			body, domain="test-a.myshopify.com", secret=SECRET_A, topic="orders/create", webhook_id="wh-raw"
		)
		stored = frappe.db.get_value("Shopify Event Log", {"webhook_id": "wh-raw"}, "payload")
		self.assertEqual(stored.encode(), body)

	def test_bad_hmac_is_rejected_and_logs_nothing(self):
		"""Nothing may touch the database on a caller's behalf before HMAC verifies."""
		body = json.dumps({"id": 1004}).encode()
		status, _ = self.post(
			body,
			domain="test-a.myshopify.com",
			secret=SECRET_A,
			topic="orders/create",
			webhook_id="wh-bad",
			signature="forged-signature",
		)
		self.assertEqual(status, 401)
		self.assertEqual(frappe.db.count("Shopify Event Log", {"webhook_id": "wh-bad"}), 0)

	def test_another_stores_secret_does_not_verify(self):
		"""Multi-store: the secret is resolved from the shop domain, per store."""
		body = json.dumps({"id": 1005}).encode()
		status, _ = self.post(
			body,
			domain="test-a.myshopify.com",
			secret=SECRET_A,
			topic="orders/create",
			webhook_id="wh-cross",
			signature=sign(body, SECRET_B),
		)
		self.assertEqual(status, 401)

	def test_unknown_shop_domain_is_unauthorised(self):
		body = json.dumps({"id": 1006}).encode()
		status, _ = self.post(
			body,
			domain="not-connected.myshopify.com",
			secret=SECRET_A,
			topic="orders/create",
			webhook_id="wh-unknown",
		)
		self.assertEqual(status, 401)

	def test_unknown_topic_returns_200_and_is_skipped(self):
		"""A 5xx would make Shopify retry a topic we will never handle, for 48 hours."""
		body = json.dumps({"id": 1007}).encode()
		status, result = self.post(
			body,
			domain="test-a.myshopify.com",
			secret=SECRET_A,
			topic="carts/update",
			webhook_id="wh-skip",
		)
		self.assertEqual(status, 200)
		self.assertEqual(result["message"], "Skipped")
		self.assertEqual(
			frappe.db.get_value("Shopify Event Log", {"webhook_id": "wh-skip"}, "status"), "Skipped"
		)

	def test_same_webhook_id_from_two_stores_is_still_deduplicated(self):
		"""Webhook ids are globally unique at Shopify, so the unique index is global too."""
		body_a = json.dumps({"id": 2001}).encode()
		self.post(
			body_a,
			domain="test-a.myshopify.com",
			secret=SECRET_A,
			topic="orders/create",
			webhook_id="wh-shared",
		)
		status, result = self.post(
			body_a,
			domain="test-b.myshopify.com",
			secret=SECRET_B,
			topic="orders/create",
			webhook_id="wh-shared",
		)
		self.assertEqual(status, 200)
		self.assertEqual(result["message"], "Duplicate")

	def test_two_stores_log_independently(self):
		self.post(
			json.dumps({"id": 3001}).encode(),
			domain="test-a.myshopify.com",
			secret=SECRET_A,
			topic="orders/create",
			webhook_id="wh-a-1",
		)
		self.post(
			json.dumps({"id": 3002}).encode(),
			domain="test-b.myshopify.com",
			secret=SECRET_B,
			topic="orders/create",
			webhook_id="wh-b-1",
		)

		self.assertEqual(frappe.db.count("Shopify Event Log", {"store": self.store_a}), 1)
		self.assertEqual(frappe.db.count("Shopify Event Log", {"store": self.store_b}), 1)


class TestSyncQueue(FrappeTestCase):
	"""The outbox (spec §5.5, §7)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.store_a = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)
		cls.store_b = make_store("Test Store B", "test-b.myshopify.com", SECRET_B)

	def setUp(self):
		frappe.db.delete("Shopify Sync Queue")
		frappe.db.commit()

	def enqueue(self, store=None, key="inventory:a:ITEM-1:loc-1", operation="inventory"):
		with patch.object(engine, "schedule_drain"):
			return engine.enqueue_sync(store or self.store_a, operation, key)

	def test_enqueue_creates_a_pending_row(self):
		name = self.enqueue()
		self.assertIsNotNone(name)
		row = frappe.get_doc("Shopify Sync Queue", name)
		self.assertEqual(row.state, "Pending")
		self.assertEqual(row.pending_dedupe_key, row.dedupe_key)

	def test_duplicate_dedupe_key_coalesces_into_one_row(self):
		"""A 20-line invoice must produce one row per item, not twenty API calls."""
		first = self.enqueue()
		second = self.enqueue()

		self.assertIsNotNone(first)
		self.assertIsNone(second)
		self.assertEqual(frappe.db.count("Shopify Sync Queue", {"state": "Pending"}), 1)

	def test_two_stores_selling_one_sku_enqueue_independently(self):
		"""Coalescing is scoped by the store being part of the dedupe key.

		The unique index is global, so isolation comes from the key convention
		``inventory:<store>:<item>:<location_gid>`` rather than from a composite index.
		Two shops selling the same SKU must each get their own row.
		"""
		a = self.enqueue(store=self.store_a, key=f"inventory:{self.store_a}:ITEM-1:loc-1")
		b = self.enqueue(store=self.store_b, key=f"inventory:{self.store_b}:ITEM-1:loc-1")

		self.assertIsNotNone(a)
		self.assertIsNotNone(b)
		self.assertEqual(frappe.db.count("Shopify Sync Queue", {"state": "Pending"}), 2)

	def test_an_identical_key_across_stores_would_collide(self):
		"""Documents the consequence of the global index: if a caller ever builds a key
		without the store in it, the second store's row is silently swallowed. This is why
		every dedupe key in the app must be store-prefixed."""
		self.assertIsNotNone(self.enqueue(store=self.store_a, key="inventory:ITEM-1"))
		self.assertIsNone(self.enqueue(store=self.store_b, key="inventory:ITEM-1"))

	def test_completing_a_row_releases_the_key_for_a_new_one(self):
		"""pending_dedupe_key is NULLed on completion, so the next change can enqueue."""
		first = self.enqueue()
		engine._succeed(first)

		second = self.enqueue()
		self.assertIsNotNone(second)
		self.assertNotEqual(first, second)

	def test_claim_flips_pending_to_running(self):
		name = self.enqueue()
		claimed = engine.claim_rows(self.store_a, 10)

		self.assertEqual([row["name"] for row in claimed], [name])
		self.assertEqual(frappe.db.get_value("Shopify Sync Queue", name, "state"), "Running")

	def test_claiming_releases_the_pending_key(self):
		"""A Running row no longer blocks a fresh enqueue for the same key -- otherwise a
		change made during a push would be silently dropped."""
		self.enqueue()
		engine.claim_rows(self.store_a, 10)
		self.assertIsNotNone(self.enqueue())

	def test_claim_does_not_take_rows_that_are_not_due_yet(self):
		name = self.enqueue()
		frappe.db.set_value(
			"Shopify Sync Queue", name, "next_attempt_at", add_to_date(now_datetime(), minutes=5)
		)
		frappe.db.commit()
		self.assertEqual(engine.claim_rows(self.store_a, 10), [])

	def test_claim_is_scoped_to_one_store(self):
		"""Per-store isolation: a throttled shop must never stall another."""
		self.enqueue(store=self.store_a, key="inventory:a:ITEM-1")
		self.enqueue(store=self.store_b, key="inventory:b:ITEM-1")

		claimed = engine.claim_rows(self.store_a, 10)
		self.assertEqual(len(claimed), 1)
		self.assertEqual(frappe.db.get_value("Shopify Sync Queue", claimed[0]["name"], "store"), self.store_a)

	def test_row_with_no_handler_fails_rather_than_looping(self):
		"""A row whose operation has no registered handler must be parked, not retried.

		The handler table is emptied for the duration rather than relying on some operation
		still being unimplemented -- otherwise this test quietly stops testing anything as
		later phases fill the table in.
		"""
		self.enqueue()
		with patch.object(engine, "schedule_drain"), patch.dict(engine.OPERATION_HANDLERS, {}, clear=True):
			result = engine.drain_store(self.store_a)

		self.assertEqual(result["failed"], 1)
		row = frappe.get_all("Shopify Sync Queue", fields=["state", "last_error"])[0]
		self.assertEqual(row.state, "Failed")
		self.assertIn("No handler registered", row.last_error)

	def test_stale_running_rows_are_recovered(self):
		"""A worker killed mid-drain leaves Running rows; without the janitor the work is
		silently lost (spec §7.2)."""
		name = self.enqueue()
		engine.claim_rows(self.store_a, 10)
		frappe.db.set_value(
			"Shopify Sync Queue",
			name,
			"modified",
			add_to_date(now_datetime(), minutes=-30),
			update_modified=False,
		)
		frappe.db.commit()

		recovered = engine.recover_stale_running(minutes=15)

		self.assertEqual(recovered, 1)
		row = frappe.get_doc("Shopify Sync Queue", name)
		self.assertEqual(row.state, "Pending")
		self.assertEqual(row.pending_dedupe_key, row.dedupe_key)

	def test_fresh_running_rows_are_left_alone(self):
		self.enqueue()
		engine.claim_rows(self.store_a, 10)
		self.assertEqual(engine.recover_stale_running(minutes=15), 0)

	def test_requeue_puts_a_failed_row_back(self):
		name = self.enqueue()
		engine._fail(name, "boom")

		with patch.object(engine, "schedule_drain"):
			frappe.get_doc("Shopify Sync Queue", name).requeue()

		row = frappe.get_doc("Shopify Sync Queue", name)
		self.assertEqual(row.state, "Pending")
		self.assertEqual(row.attempts, 0)

	def test_requeue_supersedes_when_a_newer_row_covers_the_key(self):
		"""Requeueing into an occupied key would trip the unique index and surface a raw
		database error to the user."""
		old = self.enqueue()
		engine._fail(old, "boom")
		new = self.enqueue()

		frappe.get_doc("Shopify Sync Queue", old).requeue()

		self.assertEqual(frappe.db.get_value("Shopify Sync Queue", old, "state"), "Superseded")
		self.assertEqual(frappe.db.get_value("Shopify Sync Queue", new, "state"), "Pending")


class TestShopifyStore(FrappeTestCase):
	"""Store configuration (spec §5.1)."""

	def test_shop_domain_is_normalised(self):
		"""A stored https:// prefix would silently never match the webhook header, and every
		delivery would be rejected as unauthorised."""
		from shopify_integration.shopify_integration.doctype.shopify_store.shopify_store import (
			normalise_shop_domain,
		)

		for raw in (
			"https://My-Shop.myshopify.com/",
			"My-Shop.myshopify.com",
			"http://my-shop.myshopify.com",
		):
			self.assertEqual(normalise_shop_domain(raw), "my-shop.myshopify.com")

	def test_api_version_must_look_like_a_shopify_release(self):
		doc = frappe.new_doc("Shopify Store")
		doc.store_name = "Bad Version Store"
		doc.shop_domain = "bad-version.myshopify.com"
		doc.api_version = "v1"
		doc.admin_api_token = "t"
		doc.api_secret = "s"
		doc.company = ensure_company()
		self.assertRaises(frappe.ValidationError, doc.insert)

	def test_credentials_are_stored_encrypted(self):
		"""Tokens must never sit in the table in plain text (spec §15)."""
		store = make_store("Test Store Secrets", "secrets.myshopify.com", "the-secret")
		stored = frappe.db.get_value("Shopify Store", store, "api_secret")
		self.assertNotEqual(stored, "the-secret")
		self.assertEqual(frappe.get_doc("Shopify Store", store).get_password("api_secret"), "the-secret")


class TestOAuthInstall(FrappeTestCase):
	"""The install nonce and token storage, which need Redis and a real store."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)

	def test_state_is_single_use(self):
		"""Without this, a captured install link could be replayed to write a token again."""
		from shopify_integration.api import oauth

		state = oauth.issue_state(self.store)
		self.assertEqual(oauth.consume_state(state), self.store)
		self.assertIsNone(oauth.consume_state(state), "a nonce must not work twice")

	def test_an_unknown_state_resolves_to_nothing(self):
		from shopify_integration.api import oauth

		self.assertIsNone(oauth.consume_state("never-issued"))
		self.assertIsNone(oauth.consume_state(""))

	def test_state_identifies_which_store_the_install_was_for(self):
		from shopify_integration.api import oauth

		state = oauth.issue_state(self.store)
		self.assertEqual(oauth.consume_state(state), self.store)

	def test_install_refuses_without_a_client_id(self):
		"""Dev Dashboard apps have one; legacy custom apps did not."""
		from shopify_integration.api import oauth
		from shopify_integration.exceptions import ShopifyConfigurationError

		frappe.db.set_value("Shopify Store", self.store, "client_id", None)
		frappe.clear_cache(doctype="Shopify Store")
		with self.assertRaises(ShopifyConfigurationError) as caught:
			oauth.begin_install(self.store)
		self.assertIn("Client ID", str(caught.exception))

	def test_storing_a_token_records_what_was_granted(self):
		from shopify_integration.api import oauth

		oauth.store_token(self.store, "shpat_EXAMPLE_NOT_A_REAL_TOKEN", "read_orders,read_products")

		doc = frappe.get_doc("Shopify Store", self.store)
		self.assertEqual(doc.get_password("admin_api_token"), "shpat_EXAMPLE_NOT_A_REAL_TOKEN")
		self.assertEqual(doc.granted_scopes, "read_orders,read_products")
		self.assertIsNotNone(doc.installed_on)

	def test_missing_scopes_are_reported(self):
		"""A merchant can approve fewer scopes than requested. Saying so beats an opaque
		ACCESS_DENIED days later."""
		from shopify_integration.api import oauth

		oauth.store_token(self.store, "shpat_EXAMPLE_NOT_A_REAL_TOKEN", "read_orders,read_products")
		missing = oauth.missing_scopes(self.store)

		self.assertIn("write_inventory", missing)
		self.assertNotIn("read_orders", missing)

	def test_nothing_is_reported_missing_when_all_scopes_are_granted(self):
		from shopify_integration.api import oauth

		oauth.store_token(self.store, "shpat_EXAMPLE_NOT_A_REAL_TOKEN", ",".join(oauth.REQUIRED_SCOPES))
		self.assertEqual(oauth.missing_scopes(self.store), [])


class TestHandlersRunAsAdministrator(FrappeTestCase):
	"""Webhook handlers must not run as the identity the request arrived under.

	The receiver is ``allow_guest=True``, and Frappe copies ``frappe.session.user`` onto the
	background job -- so a handler enqueued directly runs as Guest. Building a Sales Order takes
	ERPNext through ``get_item_details``, which calls ``check_permission()`` on the Item itself,
	below the level ``ignore_permissions`` on the parent document reaches.

	This only ever fails on the worker. A test that calls the handler directly runs as
	Administrator and passes while production is broken, which is exactly what happened.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)

	def tearDown(self):
		frappe.set_user("Administrator")
		# The receiver commits the event log it writes, and these tests stub out enqueue, so
		# nothing ever processes them. Left behind they sit in the Event Log list as Queued for
		# ever, looking exactly like the production stall these tests exist to prevent.
		frappe.db.delete("Shopify Event Log", {"store": self.store})
		frappe.db.commit()

	def test_the_handler_is_elevated_before_it_runs(self):
		from shopify_integration.inbound import webhook as receiver

		seen = {}

		def fake_handler(event_log):
			seen["user"] = frappe.session.user

		frappe.set_user("Guest")
		with patch.object(frappe, "get_attr", return_value=fake_handler):
			receiver.run_handler("EVT-0001", "shopify_integration.inbound.order.on_order_create")

		self.assertEqual(seen["user"], "Administrator")

	def test_the_event_log_name_still_reaches_the_handler(self):
		from shopify_integration.inbound import webhook as receiver

		seen = {}

		def fake_handler(event_log):
			seen["event_log"] = event_log

		with patch.object(frappe, "get_attr", return_value=fake_handler):
			receiver.run_handler("EVT-0002", "shopify_integration.inbound.order.on_order_create")

		self.assertEqual(seen["event_log"], "EVT-0002")

	def test_the_receiver_enqueues_the_elevating_wrapper(self):
		"""Enqueuing the handler itself is the bug; it has to go through run_handler."""
		from shopify_integration.inbound import webhook as receiver

		frappe.db.delete("Shopify Event Log", {"webhook_id": "wh-elevate"})
		frappe.db.commit()

		body = json.dumps({"id": 2002, "name": "#2002"}).encode()
		headers = {
			receiver.HEADER_DOMAIN: "test-a.myshopify.com",
			receiver.HEADER_TOPIC: "orders/create",
			receiver.HEADER_WEBHOOK_ID: "wh-elevate",
			receiver.HEADER_HMAC: sign(body, SECRET_A),
		}

		class FakeRequest:
			def __init__(self):
				self.headers = headers

			def get_data(self):
				return body

		frappe.local.response = frappe._dict()
		with (
			patch.object(frappe.local, "request", FakeRequest(), create=True),
			patch.object(frappe, "enqueue") as enqueued,
		):
			receiver.webhook()

		self.assertTrue(enqueued.called)
		self.assertIs(enqueued.call_args.args[0], receiver.run_handler)
		self.assertEqual(
			enqueued.call_args.kwargs["handler"],
			"shopify_integration.inbound.order.on_order_create",
		)

	def test_retry_also_goes_through_the_wrapper(self):
		"""Retry is clicked by a human, whose permissions are not the app's business either."""
		from shopify_integration.inbound import webhook as receiver

		log = frappe.new_doc("Shopify Event Log")
		log.store = self.store
		log.topic = "orders/create"
		log.webhook_id = "wh-retry-elevate"
		log.status = "Error"
		log.payload = "{}"
		log.insert(ignore_permissions=True)
		frappe.db.commit()

		with patch.object(frappe, "enqueue") as enqueued:
			log.retry()

		self.assertIs(enqueued.call_args.args[0], receiver.run_handler)
		self.assertEqual(
			enqueued.call_args.kwargs["handler"],
			"shopify_integration.inbound.order.on_order_create",
		)


class TestRequeueCollision(FrappeTestCase):
	"""Claiming a row frees its dedupe key so new work can queue behind it. Writing that key
	back on a retry can therefore collide with the row that took it.

	The collision is raised from inside `drain_store`'s own error handling, where nothing
	catches it -- so the whole batch's successes roll back uncommitted and every claimed row
	is left Running until the janitor comes round. The janitor hits the same collision and
	aborts before its own commit, so it recovers nothing, every tick, forever.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.store = make_store("Test Store A", "test-a.myshopify.com", SECRET_A)

	def setUp(self):
		frappe.db.delete("Shopify Sync Queue", {"store": self.store})
		frappe.db.commit()

	tearDown = setUp

	def _row(self, key, state, pending_key=None):
		doc = frappe.new_doc("Shopify Sync Queue")
		doc.store = self.store
		doc.operation = "inventory"
		doc.dedupe_key = key
		doc.state = state
		doc.attempts = 0
		doc.next_attempt_at = frappe.utils.now_datetime()
		doc.insert(ignore_permissions=True)
		frappe.db.set_value(
			"Shopify Sync Queue", doc.name, "pending_dedupe_key", pending_key, update_modified=False
		)
		frappe.db.commit()
		return doc.name

		# -- the retry path --------------------------------------------------------------

	def test_a_retry_whose_key_was_taken_retires_instead_of_colliding(self):
		from shopify_integration.exceptions import ShopifyTransportError

		key = "inventory:collide:1"
		running = self._row(key, "Running", pending_key=None)
		self._row(key, "Pending", pending_key=key)

		outcome = engine._handle_error(
			{"name": running, "dedupe_key": key, "attempts": 0}, ShopifyTransportError("boom")
		)
		frappe.db.commit()

		self.assertEqual(outcome, "superseded")
		self.assertEqual(
			frappe.db.get_value("Shopify Sync Queue", running, "state"),
			"Done",
			"nothing went wrong -- the newer row covers this work, and calling it Failed buries "
			"the failures that do matter",
		)
		self.assertIn("Superseded", frappe.db.get_value("Shopify Sync Queue", running, "last_error"))

	def test_an_uncontested_retry_still_goes_back_to_pending(self):
		from shopify_integration.exceptions import ShopifyTransportError

		key = "inventory:collide:2"
		running = self._row(key, "Running", pending_key=None)

		outcome = engine._handle_error(
			{"name": running, "dedupe_key": key, "attempts": 0}, ShopifyTransportError("boom")
		)
		frappe.db.commit()

		self.assertEqual(outcome, "requeued")
		self.assertEqual(frappe.db.get_value("Shopify Sync Queue", running, "state"), "Pending")

		# -- the janitor -----------------------------------------------------------------

	def test_the_janitor_recovers_what_it_can_despite_a_collision(self):
		"""One contested row must not stop the others being recovered."""
		contested = "inventory:collide:3"
		free = "inventory:collide:4"

		stale_contested = self._row(contested, "Running", pending_key=None)
		self._row(contested, "Pending", pending_key=contested)
		stale_free = self._row(free, "Running", pending_key=None)

		old = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-2)
		for name in (stale_contested, stale_free):
			frappe.db.set_value("Shopify Sync Queue", name, "modified", old, update_modified=False)
		frappe.db.commit()

		recovered = engine.recover_stale_running(minutes=1)
		frappe.db.commit()

		self.assertEqual(recovered, 1, "the uncontested row is recovered")
		self.assertEqual(frappe.db.get_value("Shopify Sync Queue", stale_free, "state"), "Pending")
		self.assertEqual(
			frappe.db.get_value("Shopify Sync Queue", stale_contested, "state"),
			"Done",
			"the contested row is retired, not taken down with the whole pass",
		)


class TestShopDomainMustBeShopify(FrappeTestCase):
	"""`shop_domain` builds the URL every Admin API token is sent to. It must be a Shopify host.

	There were two functions named `normalise_shop_domain`. The strict one in `api.oauth`
	applied SHOP_DOMAIN_PATTERN; the one the doctype's `validate()` actually called only
	stripped the scheme and path. So a store saved happily pointing at `attacker.example.com`,
	`127.0.0.1:8000` or `169.254.169.254`, and the next sync or Test Connection handed the
	token straight to it.

	The regex existed the whole time. It simply guarded the OAuth path and missed the field
	that builds the URLs.
	"""

	def _store(self, domain):
		doc = frappe.new_doc("Shopify Store")
		doc.store_name = f"_Test Domain {frappe.utils.random_string(6)}"
		doc.shop_domain = domain
		doc.api_version = "2026-01"
		doc.api_secret = "x"
		doc.admin_api_token = "y"
		doc.company = ensure_company()
		doc.enabled = 0
		return doc

	def test_a_non_shopify_host_is_refused(self):
		for domain in (
			"attacker.example.com",
			"169.254.169.254",
			"127.0.0.1:8000",
			"localhost",
			"myshopify.com.evil.test",
			"https://attacker.test/admin",
		):
			with self.assertRaises(frappe.ValidationError, msg=f"{domain} was accepted"):
				self._store(domain).insert(ignore_permissions=True)
			frappe.db.rollback()

	def test_a_pasted_url_is_trimmed_and_accepted(self):
		"""People copy the URL out of the browser. Scheme and path must survive that."""
		for given, expected in (
			("my-shop.myshopify.com", "my-shop.myshopify.com"),
			("https://my-shop.myshopify.com", "my-shop.myshopify.com"),
			("https://my-shop.myshopify.com/", "my-shop.myshopify.com"),
			("https://my-shop.myshopify.com/admin/products", "my-shop.myshopify.com"),
			("  MY-SHOP.MyShopify.com  ", "my-shop.myshopify.com"),
		):
			doc = self._store(given)
			doc.insert(ignore_permissions=True)
			self.assertEqual(doc.shop_domain, expected)
			frappe.delete_doc("Shopify Store", doc.name, force=True, ignore_permissions=True)
			frappe.db.commit()

	def test_an_empty_domain_is_left_to_the_mandatory_check(self):
		"""Blank is a missing-field problem, not a malicious-host one."""
		from shopify_integration.shopify_integration.doctype.shopify_store.shopify_store import (
			normalise_shop_domain,
		)

		self.assertEqual(normalise_shop_domain(""), "")
		self.assertEqual(normalise_shop_domain(None), "")
