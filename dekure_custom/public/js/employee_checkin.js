if (frappe.ui?.form) {
	frappe.ui.form.on("Employee Checkin", {
		refresh(frm) {
			if (frm.is_new() || frm.doc.log_type !== "IN") return;
			frm.add_custom_button(__("Missed Check-out Requests"), () => {
				const attendance_date = frappe.datetime
					.obj_to_str(frappe.datetime.str_to_obj(frm.doc.time))
					.split(" ")[0];
				frappe.set_route("List", "Attendance Regularization Request", {
					employee: frm.doc.employee,
					attendance_date,
					request_type: "Missed Check-out",
				});
			}, __("View"));
		},
	});
}

if (frappe.listview_settings) {
	const employee_checkin_settings = frappe.listview_settings["Employee Checkin"] || {};
	if (!employee_checkin_settings.missed_checkout_requests_added) {
		const existing_onload = employee_checkin_settings.onload;

		employee_checkin_settings.onload = function (listview) {
			if (existing_onload) existing_onload(listview);
			listview.page.add_inner_button(__("Pending Missed Check-out Requests"), () => {
				frappe.set_route("List", "Attendance Regularization Request", {
					request_type: "Missed Check-out",
					status: "Pending Approval",
				});
			});
		};
		employee_checkin_settings.missed_checkout_requests_added = true;
	}

	frappe.listview_settings["Employee Checkin"] = employee_checkin_settings;
}
