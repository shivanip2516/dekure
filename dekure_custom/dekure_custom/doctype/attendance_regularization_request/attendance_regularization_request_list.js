frappe.listview_settings["Attendance Regularization Request"] = {
	onload(listview) {
		listview.page.add_inner_button(__("Request Missed Check-in"), () => open_request_dialog("Missed Check-in"));
		listview.page.add_inner_button(__("Request Missed Check-out"), () => open_request_dialog("Missed Check-out"));
	},
};

function open_request_dialog(request_type) {
	frappe.call({
		method: "dekure_custom.api.get_missed_punch_status",
		callback: ({ message }) => {
			if (!message) return;
			const is_checkin = request_type === "Missed Check-in";
			const allowed = is_checkin ? message.can_request_checkin : message.can_request_checkout;
			if (!allowed) {
				frappe.msgprint(__("A request of this type cannot be created for the current attendance state."));
				return;
			}
			frappe.prompt(
				[
					{
						fieldname: "attendance_date",
						fieldtype: "Date",
						label: __("Attendance Date"),
						default: (!is_checkin && message.missed_checkout_date) || frappe.datetime.get_today(),
						reqd: 1,
					},
					{
						fieldname: "requested_time",
						fieldtype: "Time",
						label: is_checkin ? __("Requested Check-in Time") : __("Requested Check-out Time"),
						reqd: 1,
					},
					{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 },
				],
				(values) => {
					frappe.call({
						method: "dekure_custom.api.create_missed_punch_request",
						args: { request_type, ...values },
						freeze: true,
						callback: () => listview.refresh(),
					});
				},
				__(request_type),
				__("Submit"),
			);
		},
	});
}
