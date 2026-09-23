frappe.ui.form.on("Shopify Sync Queue", {
	refresh(frm) {
		// §14: every failure must be replayable. The server method existed from phase 1, but
		// without this button there was no way for a user to actually reach it.
		if (["Failed", "Superseded"].includes(frm.doc.state)) {
			frm.add_custom_button(__("Requeue"), () => {
				frm.call({
					doc: frm.doc,
					method: "requeue",
					freeze: true,
					freeze_message: __("Putting it back in the queue..."),
					callback: ({ message }) => {
						if (!message) return;
						if (message.state === "Superseded") {
							frappe.msgprint({
								title: __("Superseded"),
								indicator: "orange",
								message: __(
									"A newer pending row already covers this key ({0}), so it will push the same current state. This row has been marked Superseded rather than retried.",
									[message.superseded_by]
								),
							});
						} else {
							frappe.show_alert({ message: __("Requeued."), indicator: "green" });
						}
						frm.reload_doc();
					},
				});
			}).addClass("btn-primary");
		}

		if (frm.doc.state === "Failed" && frm.doc.attempts) {
			frm.dashboard.add_indicator(
				__("Gave up after {0} attempts", [frm.doc.attempts]),
				"red"
			);
		}
	},
});
