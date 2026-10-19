frappe.ui.form.on("Compensatory Leave Request", {
	refresh(frm) {
		if (
			frm.doc.docstatus === 0 &&
			frm.doc.custom_status === "Draft" &&
			frm.doc.owner === frappe.session.user
		) {
			frm.add_custom_button(__("Send for Approval"), () => request_approval(frm), __("Actions"))
		}

		if (
			frm.doc.docstatus !== 0 ||
			frm.doc.custom_status !== "Pending Approval" ||
			frm.doc.custom_leave_approver !== frappe.session.user
		) {
			return
		}

		frm.add_custom_button(__("Approve"), () => review_comp_off(frm, "Approved"), __("Actions"))
		frm.add_custom_button(__("Reject"), () => review_comp_off(frm, "Rejected"), __("Actions"))
	},
})

function request_approval(frm) {
	frappe.call({
		method: "dekure_custom.comp_off.request_comp_off_approval",
		args: { name: frm.doc.name },
		freeze: true,
		callback() {
			frm.reload_doc()
		},
	})
}

function review_comp_off(frm, action) {
	frappe.call({
		method: "dekure_custom.comp_off.review_pwa_comp_off",
		args: { name: frm.doc.name, action },
		freeze: true,
		callback() {
			frm.reload_doc()
		},
	})
}
