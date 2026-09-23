"""OAuth install flow for Dev Dashboard apps.

Beyond the build specification, and necessary because Shopify changed underneath it. §5.1
assumes an Admin API token pasted into a field, which is how *legacy custom apps* worked --
and merchants can no longer create those as of 2026-01-01. Apps created in the Dev Dashboard
issue tokens only through OAuth, so without this there is no supported way for a new customer
to connect a store at all.

Only the acquisition of the token changes. It lands in the same ``admin_api_token`` field, is
sent in the same header, and every other part of the app is untouched.

Two HMACs, computed differently
-------------------------------
This is the detail that bites people. Both use the app's secret, but:

* **Webhooks** -- base64 of HMAC-SHA256 over the raw request *body*.
* **OAuth callback** -- hex of HMAC-SHA256 over the sorted *query string*.

Using the webhook routine here silently rejects every install.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from urllib.parse import urlencode

import frappe
import requests
from frappe import _
from frappe.utils import cstr, get_url, now_datetime

from shopify_integration.exceptions import ShopifyConfigurationError

#: Scopes requested at install. Least privilege: every one of these is used by a feature that
#: ships today, and nothing is requested speculatively.
REQUIRED_SCOPES = (
	"read_products",
	"write_products",
	"read_orders",
	"read_inventory",
	"write_inventory",
	"read_locations",
	"read_customers",
	# Needed to mark an order fulfilled and to push a tracking number. Asked for at install
	# even though fulfilment is off by default: the alternative is a merchant who ticks
	# "Sync Fulfilments" months later and has to re-authorise the whole app to use it.
	"write_merchant_managed_fulfillment_orders",
)

#: A shop domain must look exactly like this. The value arrives in a query parameter on a
#: public endpoint and is then used to build a URL we POST a client secret to, so anything
#: looser is a request-forgery hole.
SHOP_DOMAIN_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]*\.myshopify\.com$")

#: How long an install may sit half-finished before its nonce expires.
STATE_TTL_SECONDS = 600


def callback_url() -> str:
	return f"{get_url().rstrip('/')}/api/method/shopify_integration.api.oauth.callback"


def authorize_url(shop_domain: str, client_id: str, state: str, scopes=REQUIRED_SCOPES) -> str:
	"""Where the merchant is sent to approve the install.

	No ``grant_options``, which is what asks Shopify for an *offline* token -- one that keeps
	working when nobody is logged in. An online token would expire and break every background
	sync this app performs.
	"""
	query = urlencode(
		{
			"client_id": client_id,
			"scope": ",".join(scopes),
			"redirect_uri": callback_url(),
			"state": state,
		}
	)
	return f"https://{shop_domain}/admin/oauth/authorize?{query}"


def normalise_shop_domain(shop_domain: str) -> str | None:
	"""Return the cleaned shop domain, or None if it is not a valid one.

	Pure, so the rule that guards a public endpoint can be tested on its own rather than
	through a frappe.throw that needs a site context -- a test asserting only "something was
	raised" passes just as readily when the raise came from the missing context instead of
	from the validation.
	"""
	cleaned = cstr(shop_domain).strip().lower()
	return cleaned if SHOP_DOMAIN_PATTERN.match(cleaned) else None


def validate_shop_domain(shop_domain: str) -> str:
	cleaned = normalise_shop_domain(shop_domain)
	if not cleaned:
		frappe.throw(
			_("'{0}' is not a valid Shopify shop domain. It must look like my-shop.myshopify.com.").format(
				shop_domain
			)
		)
	return cleaned


def verify_callback_hmac(params: dict, secret: str) -> bool:
	"""Verify Shopify's signature on the callback query parameters.

	Hex digest over the sorted query string, not base64 over a body -- see the module
	docstring. ``compare_digest`` because a naive comparison leaks the expected value.
	"""
	provided = cstr(params.get("hmac"))
	if not provided or not secret:
		return False

	rest = {k: v for k, v in params.items() if k not in ("hmac", "signature")}
	message = "&".join(f"{key}={rest[key]}" for key in sorted(rest))
	expected = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
	return hmac.compare_digest(expected, provided)


def _state_key(state: str) -> str:
	return f"shopify_integration:oauth_state:{state}"


def issue_state(store: str) -> str:
	"""A single-use nonce tying the callback back to the install we started.

	Without it, anyone can invoke the public callback and have us exchange a code of their
	choosing -- and we would write the resulting token onto a store of theirs.
	"""
	state = secrets.token_urlsafe(32)
	frappe.cache().set_value(_state_key(state), store, expires_in_sec=STATE_TTL_SECONDS)
	return state


def consume_state(state: str) -> str | None:
	"""Return the store this nonce belongs to and burn it. One use only."""
	if not state:
		return None
	key = _state_key(state)
	store = frappe.cache().get_value(key)
	frappe.cache().delete_value(key)
	return cstr(store) or None


@frappe.whitelist()
def begin_install(store: str) -> dict:
	"""Start an install. Returns the URL the browser should be sent to."""
	frappe.has_permission("Shopify Store", "write", doc=store, throw=True)

	doc = frappe.get_doc("Shopify Store", store)
	client_id = cstr(doc.client_id).strip()
	if not client_id:
		raise ShopifyConfigurationError(
			f"Store {store} has no Client ID. Copy it from your app in the Shopify Dev Dashboard."
		)
	if not doc.get_password("api_secret", raise_exception=False):
		raise ShopifyConfigurationError(
			f"Store {store} has no Client Secret. Copy it from your app in the Shopify Dev Dashboard."
		)

	shop_domain = validate_shop_domain(doc.shop_domain)
	state = issue_state(store)
	return {"url": authorize_url(shop_domain, client_id, state), "redirect_uri": callback_url()}


@frappe.whitelist(allow_guest=True)
def callback(**kwargs):
	"""Where Shopify sends the merchant back after they approve the install.

	Public, like the webhook receiver, and treated with the same suspicion: nothing is trusted
	until the nonce matches and the signature verifies.
	"""
	params = dict(frappe.local.request.args) if frappe.local.request else dict(kwargs)
	params = {k: (v[0] if isinstance(v, list) else v) for k, v in params.items()}

	state = cstr(params.get("state"))
	store = consume_state(state)
	if not store:
		return _fail(_("This install link has expired or was already used. Start the install again."))

	if not frappe.db.exists("Shopify Store", store):
		return _fail(_("The store this install belongs to no longer exists."))

	doc = frappe.get_doc("Shopify Store", store)
	secret = doc.get_password("api_secret", raise_exception=False)

	if not verify_callback_hmac(params, secret or ""):
		frappe.logger("shopify_integration").warning(
			f"Rejected OAuth callback with an invalid signature for store {store}"
		)
		return _fail(_("Shopify's signature on this response did not verify. Nothing was saved."))

	shop_domain = validate_shop_domain(params.get("shop"))
	if shop_domain != cstr(doc.shop_domain).strip().lower():
		# The callback names a different shop than the store we started the install for.
		return _fail(
			_("This response is for {0}, but the install was started for {1}.").format(
				shop_domain, doc.shop_domain
			)
		)

	code = cstr(params.get("code"))
	if not code:
		return _fail(_("Shopify did not return an authorisation code."))

	try:
		token, granted = exchange_code(shop_domain, doc.client_id, secret, code)
	except Exception as exc:
		frappe.log_error(title=f"Shopify OAuth exchange failed for {store}", message=frappe.get_traceback())
		return _fail(_("Could not exchange the code for a token: {0}").format(exc))

	store_token(store, token, granted)
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = f"/app/shopify-store/{frappe.utils.quoted(store)}"
	return None


def exchange_code(shop_domain: str, client_id: str, client_secret: str, code: str) -> tuple[str, str]:
	"""Trade the one-time code for a long-lived offline access token."""
	response = requests.post(
		f"https://{shop_domain}/admin/oauth/access_token",
		json={"client_id": client_id, "client_secret": client_secret, "code": code},
		timeout=30,
	)
	response.raise_for_status()
	payload = response.json()

	token = cstr(payload.get("access_token"))
	if not token:
		raise ShopifyConfigurationError("Shopify's response contained no access token")
	return token, cstr(payload.get("scope"))


def store_token(store: str, token: str, granted_scopes: str) -> None:
	"""Save the token and record what was actually granted.

	Recording the granted scopes matters: a merchant can approve a narrower set than we asked
	for, and the resulting failure is otherwise an opaque ACCESS_DENIED days later.
	"""
	doc = frappe.get_doc("Shopify Store", store)
	doc.admin_api_token = token
	doc.granted_scopes = granted_scopes
	doc.installed_on = now_datetime()
	doc.flags.ignore_mandatory = True
	doc.save(ignore_permissions=True)
	frappe.db.commit()


def effective_scopes(granted: set[str]) -> set[str]:
	"""Expand granted scopes to everything they actually confer.

	Shopify treats ``write_x`` as including ``read_x`` and reports only the write scope, so a
	store granted ``write_products`` comes back without ``read_products`` while being perfectly
	able to read them. Comparing the raw lists reports two scopes missing on every correctly
	installed store -- verified against a live shop, which read products and locations happily
	with only the write scopes granted.
	"""
	expanded = set(granted)
	for scope in granted:
		if scope.startswith("write_"):
			expanded.add("read_" + scope[len("write_") :])
	return expanded


def missing_scopes(store: str) -> list[str]:
	"""Scopes we asked for that the merchant genuinely did not grant."""
	raw = cstr(frappe.db.get_value("Shopify Store", store, "granted_scopes")).split(",")
	granted = {s.strip() for s in raw if s.strip()}
	if not granted:
		return []
	effective = effective_scopes(granted)
	return [scope for scope in REQUIRED_SCOPES if scope not in effective]


def _fail(message: str):
	"""Render a readable failure to the merchant's browser, with a 400.

	``respond_as_web_page`` rather than hand-setting the response dict: Frappe's page renderer
	expects a ``route`` key, and setting ``type = "page"`` without one raises a KeyError that
	surfaces as a 500 -- which on a public endpoint means an error page where a refusal should
	be, and a stack trace where neither should be.
	"""
	frappe.respond_as_web_page(
		_("Shopify install failed"),
		message,
		success=False,
		http_status_code=400,
		indicator_color="red",
	)
	return None
