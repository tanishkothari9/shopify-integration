frappe.ui.form.on("Shopify Event Log", {
	refresh(frm) {
		// §5.6 requires a Retry that re-runs the handler against the stored payload. Storing
		// the raw body is what makes this an exact replay of what Shopify actually sent.
		if (["Error", "Queued"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Retry"), () => {
				frm.call({
					doc: frm.doc,
					method: "retry",
					freeze: true,
					freeze_message: __("Replaying the webhook..."),
					callback: () => {
						frappe.show_alert({
							message: __("Queued for replay."),
							indicator: "blue",
						});
						frm.reload_doc();
					},
				});
			}).addClass("btn-primary");
		}

		if (frm.doc.status === "Skipped") {
			frm.dashboard.set_headline(
				__("No handler is registered for topic {0}, so this event was recorded and ignored.", [
					frm.doc.topic,
				])
			);
		}

		if (frm.doc.ref_doctype && frm.doc.ref_docname) {
			frm.add_custom_button(__("Open {0}", [frm.doc.ref_doctype]), () => {
				frappe.set_route("Form", frm.doc.ref_doctype, frm.doc.ref_docname);
			});
		}
	},
});
