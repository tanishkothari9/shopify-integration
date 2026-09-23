frappe.ui.form.on("Shopify Store", {
	refresh(frm) {
		frm.trigger("load_series_options");
		frm.trigger("load_status");
		frm.trigger("show_install_state");

		if (!frm.is_new()) {
			// Dev Dashboard apps issue tokens only via OAuth, so this replaces pasting one.
			if (!frm.doc.installed_on) {
				frm.add_custom_button(__("Install with Shopify"), () => {
					frappe.call({
						method: "shopify_integration.api.oauth.begin_install",
						args: { store: frm.doc.name },
						freeze: true,
						callback: ({ message }) => {
							if (!message || !message.url) return;
							// Full page load, not a popup: Shopify refuses to be framed.
							window.location.href = message.url;
						},
					});
				}).addClass("btn-primary");
			} else {
				frm.add_custom_button(__("Reinstall with Shopify"), () => {
					frappe.confirm(
						__("Request a fresh token from Shopify? The current one keeps working until the new one is stored."),
						() => {
							frappe.call({
								method: "shopify_integration.api.oauth.begin_install",
								args: { store: frm.doc.name },
								freeze: true,
								callback: ({ message }) => {
									if (message && message.url) window.location.href = message.url;
								},
							});
						}
					);
				});
			}

			frm.add_custom_button(__("Import Catalogue"), () => {
				frappe.confirm(
					__("Import all products from Shopify into ERPNext? This runs in the background."),
					() => {
						frappe.call({
							method: "shopify_integration.shopify_integration.doctype.shopify_store.shopify_store.import_catalogue",
							args: { store: frm.doc.name },
							freeze: true,
							freeze_message: __("Asking Shopify to start the export..."),
							callback: ({ message }) => {
								if (!message) return;
								frappe.show_alert({
									message: __("Import started. Progress will appear here."),
									indicator: "blue",
								});
								frappe.set_route("Form", "Shopify Bulk Operation", message.operation);
							},
						});
					}
				);
			});

			frm.add_custom_button(__("Reconcile Now"), () => {
				frappe.call({
					method: "shopify_integration.shopify_integration.doctype.shopify_store.shopify_store.run_reconciliation",
					args: { store: frm.doc.name },
					freeze: true,
					freeze_message: __("Checking Shopify against ERPNext..."),
					callback: ({ message }) => {
						if (!message) return;
						const inv = message.inventory || {};
						const ord = message.orders || {};
						frappe.msgprint({
							title: __("Reconciliation complete"),
							indicator: (inv.drifted || ord.missing) ? "orange" : "green",
							message: __(
								"Inventory: {0} checked, {1} drifted, {2} corrected.<br>Orders: {3} checked, {4} missing, {5} replayed.",
								[inv.checked || 0, inv.drifted || 0, inv.corrected || 0,
								 ord.checked || 0, ord.missing || 0, ord.replayed || 0]
							),
						});
						frm.trigger("load_status");
					},
				});
			});

			frm.add_custom_button(__("Test Connection"), () => {
				frappe.call({
					method: "shopify_integration.shopify_integration.doctype.shopify_store.shopify_store.test_connection",
					args: { store: frm.doc.name },
					freeze: true,
					freeze_message: __("Talking to Shopify..."),
					callback: ({ message }) => {
						if (!message) return;
						if (message.ok === false) {
							frappe.msgprint({
								title: __("Could not connect"),
								indicator: "red",
								message: [
									frappe.utils.escape_html(message.problem || ""),
									message.hint ? `<p class="text-muted">${frappe.utils.escape_html(message.hint)}</p>` : "",
								].join("<br>"),
							});
							return;
						}
						frappe.msgprint({
							title: __("Connected"),
							indicator: "green",
							message: __("Shop: {0}<br>Domain: {1}<br>Currency: {2}<br>Timezone: {3}", [
								message.name, message.domain, message.currency, message.timezone,
							]),
						});
					},
				});
			});
		}
	},

	show_install_state(frm) {
		if (frm.is_new()) return;
		if (!frm.doc.installed_on) {
			frm.dashboard.set_headline(
				__("Not installed yet. Add the Client ID and Client Secret from your Dev Dashboard app, save, then click <b>Install with Shopify</b>.")
			);
			return;
		}
		if (frm.doc.granted_scopes) {
			// Shopify reports only write_x when write_x was granted, but it confers read_x
			// too. Comparing the raw list flags two scopes missing on every good install.
			const granted = frm.doc.granted_scopes.split(",").map((s) => s.trim());
			for (const s of [...granted]) {
				if (s.startsWith("write_")) granted.push("read_" + s.slice("write_".length));
			}
			const needed = [
				"read_products", "write_products", "read_orders", "read_inventory",
				"write_inventory", "read_locations", "read_customers",
			];
			const missing = needed.filter((s) => !granted.includes(s));
			if (missing.length) {
				// A merchant can approve fewer scopes than we asked for. Saying so now beats
				// an opaque ACCESS_DENIED days later.
				frm.dashboard.set_headline(
					__("Installed, but these scopes were not granted: {0}. Reinstall to request them.", [
						missing.join(", "),
					])
				);
			}
		}
	},

	load_status(frm) {
		// The dashboard from §14: "it didn't sync" must always have an answer.
		if (frm.is_new()) return;
		frappe.call({
			method: "shopify_integration.shopify_integration.doctype.shopify_store.shopify_store.store_status",
			args: { store: frm.doc.name },
			callback: ({ message }) => {
				if (!message) return;
				frm.dashboard.clear_headline();

				const q = message.queue || {};
				const e = message.events || {};
				// Singular/plural matters here: these sit at the top of the form and read as
				// prose, and "1 webhook errors" looks like a bug in the thing reporting bugs.
				const plural = (n, one, many) => (n === 1 ? __(one, [n]) : __(many, [n]));
				const indicators = [
					[plural(q.pending || 0, "{0} pending", "{0} pending"), q.pending ? "orange" : "green"],
					[plural(q.failed || 0, "{0} failed", "{0} failed"), q.failed ? "red" : "green"],
					[
						plural(e.error || 0, "{0} webhook error", "{0} webhook errors"),
						e.error ? "red" : "green",
					],
				];
				if (message.api_headroom) {
					const h = message.api_headroom;
					indicators.push([
						__("API headroom {0}/{1}", [h.available, h.maximum]),
						h.available < h.maximum * 0.2 ? "orange" : "blue",
					]);
				}
				indicators.push([
					message.last_successful_sync
						? __("Last sync {0}", [frappe.datetime.comment_when(message.last_successful_sync)])
						: __("Never synced"),
					message.last_successful_sync ? "blue" : "grey",
				]);
				indicators.push([
					message.last_reconciled_on
						? __("Reconciled {0}", [frappe.datetime.comment_when(message.last_reconciled_on)])
						: __("Never reconciled"),
					message.last_reconciled_on ? "blue" : "grey",
				]);

				for (const [label, colour] of indicators) {
					frm.dashboard.add_indicator(label, colour);
				}
			},
		});
	},

	onload(frm) {
		// Live import progress, published by api/bulk.py as products are applied.
		frappe.realtime.on("shopify_bulk_progress", (data) => {
			if (!data || data.store !== frm.doc.name) return;
			const total = data.total || 0;
			const pct = total ? Math.round((data.processed / total) * 100) : 0;
			frm.dashboard.show_progress(
				__("Shopify catalogue import"),
				pct,
				__("{0} of {1} products", [data.processed, total || "?"])
			);
			if (data.status === "Completed") {
				frm.dashboard.hide_progress();
				frappe.show_alert({ message: __("Catalogue import finished."), indicator: "green" });
			}
		});
	},

	load_series_options(frm) {
		frappe.call({
			method: "shopify_integration.shopify_integration.doctype.shopify_store.shopify_store.get_series",
			callback: ({ message }) => {
				for (const [fieldname, options] of Object.entries(message || {})) {
					frm.set_df_property(fieldname, "options", options);
				}
			},
		});
	},
});
