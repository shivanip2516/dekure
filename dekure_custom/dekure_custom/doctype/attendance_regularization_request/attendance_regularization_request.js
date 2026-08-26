frappe.ui.form.on("Attendance Regularization Request", {
	onload(frm) {
		if (frm.is_new()) {
			frm.set_df_property("employee", "read_only", 0);
			frm.set_df_property("attendance_date", "read_only", 0);
			frm.set_df_property("request_type", "read_only", 0);
			frm.set_df_property("requested_time", "read_only", 0);
			frm.set_df_property("reason", "read_only", 0);
			
			if (!frm.doc.employee) {
				frappe.db.get_value("Employee", { user_id: frappe.session.user }, "name", (r) => {
					if (r && r.name) {
						frm.set_value("employee", r.name);
					}
				});
			}
		}
	},
	refresh(frm) {
		if (frm.is_new() || frm.doc.status !== "Pending Approval") return;
		
		// Approver Security / Role permission check
		// Ensure the user is the configured approver or has override role
		const has_approval_permission = frappe.user.has_role("HR Manager") || 
			frappe.user.has_role("System Manager") || 
			frappe.session.user === frm.doc.attendance_approver;

		if (has_approval_permission) {
			frm.add_custom_button(__("Approve"), () => review(frm, "Approve"), __("Review"));
			frm.add_custom_button(__("Reject"), () => {
				frappe.prompt({ fieldname: "reason", fieldtype: "Small Text", label: __("Rejection Reason"), reqd: 1 }, ({ reason }) => review(frm, "Reject", reason), __("Reject Request"));
			}, __("Review"));
		}
	},
	employee(frm) {
		if (frm.doc.employee && frm.doc.attendance_date) {
			fetch_details(frm);
		}
	},
	attendance_date(frm) {
		if (frm.doc.employee && frm.doc.attendance_date) {
			fetch_details(frm);
		}
	},
	request_type(frm) {
		if (frm.is_new() && frm.doc.request_type === "Missed Check-out") {
			navigator.geolocation.getCurrentPosition(
				(pos) => {
					frm.set_value("latitude", pos.coords.latitude);
					frm.set_value("longitude", pos.coords.longitude);
				},
				(err) => {
					frappe.msgprint(__("Location access is required for Missed Check-out requests. Please enable location access."));
				},
				{ enableHighAccuracy: true, timeout: 10000, maximumAge: 0 }
			);
		} else if (frm.is_new() && frm.doc.request_type !== "Missed Check-out") {
			frm.set_value("latitude", null);
			frm.set_value("longitude", null);
		}
	}
});

function fetch_details(frm) {
	frappe.call({
		method: "dekure_custom.api.get_attendance_regularization_details",
		args: {
			employee: frm.doc.employee,
			attendance_date: frm.doc.attendance_date
		},
		callback: (r) => {
			if (r.message) {
				frm.set_value("shift", r.message.shift);
				frm.set_value("attendance_approver", r.message.attendance_approver);
				frm.set_value("existing_checkin", r.message.existing_checkin);
			}
		}
	});
}

function review(frm, action, rejection_reason = null) {
	frappe.call({
		method: "dekure_custom.api.review_missed_punch_request",
		args: { name: frm.doc.name, action, rejection_reason },
		freeze: true,
		callback: () => frm.reload_doc(),
	});
}
