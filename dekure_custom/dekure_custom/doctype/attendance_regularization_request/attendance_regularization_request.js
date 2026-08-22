frappe.ui.form.on("Attendance Regularization Request", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.status !== "Pending Approval") return;
		if (frappe.session.user !== frm.doc.attendance_approver) return;
		frm.add_custom_button(__("Approve"), () => review(frm, "Approve"), __("Review"));
		frm.add_custom_button(__("Reject"), () => {
			frappe.prompt({ fieldname: "reason", fieldtype: "Small Text", label: __("Rejection Reason"), reqd: 1 }, ({ reason }) => review(frm, "Reject", reason), __("Reject Request"));
		}, __("Review"));
	},
});

function review(frm, action, rejection_reason = null) {
	frappe.call({
		method: "dekure_custom.api.review_missed_punch_request",
		args: { name: frm.doc.name, action, rejection_reason },
		freeze: true,
		callback: () => frm.reload_doc(),
	});
}
