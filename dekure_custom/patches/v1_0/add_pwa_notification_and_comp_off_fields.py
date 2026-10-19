import frappe


CUSTOM_FIELDS = (
	{
		"name": "Compensatory Leave Request-custom_status",
		"dt": "Compensatory Leave Request",
		"fieldname": "custom_status",
		"fieldtype": "Select",
		"label": "Status",
		"options": "Draft\nPending Approval\nApproved\nRejected",
		"default": "Draft",
		"read_only": 1,
		"in_list_view": 1,
		"insert_after": "reason",
	},
	{
		"name": "Compensatory Leave Request-custom_leave_approver",
		"dt": "Compensatory Leave Request",
		"fieldname": "custom_leave_approver",
		"fieldtype": "Link",
		"label": "Leave Approver",
		"options": "User",
		"read_only": 1,
		"insert_after": "custom_status",
	},
	{
		"name": "Compensatory Leave Request-custom_pwa_comp_off",
		"dt": "Compensatory Leave Request",
		"fieldname": "custom_pwa_comp_off",
		"fieldtype": "Check",
		"label": "PWA Comp-Off Request",
		"default": "0",
		"hidden": 1,
		"insert_after": "custom_leave_approver",
	},
	{
		"name": "PWA Notification-custom_action_type",
		"dt": "PWA Notification",
		"fieldname": "custom_action_type",
		"fieldtype": "Select",
		"label": "Action Type",
		"options": "\nApproval\nCompletion",
		"hidden": 1,
		"read_only": 1,
		"insert_after": "read",
	},
	{
		"name": "PWA Notification-custom_resolved",
		"dt": "PWA Notification",
		"fieldname": "custom_resolved",
		"fieldtype": "Check",
		"label": "Resolved",
		"default": "0",
		"hidden": 1,
		"read_only": 1,
		"insert_after": "custom_action_type",
	},
)


def execute():
	for custom_field in CUSTOM_FIELDS:
		if frappe.db.exists("Custom Field", custom_field["name"]):
			frappe.db.set_value(
				"Custom Field",
				custom_field["name"],
				custom_field,
				update_modified=False,
			)
		else:
			frappe.get_doc({"doctype": "Custom Field", **custom_field}).insert(
				ignore_permissions=True
			)

	for doctype in ("Compensatory Leave Request", "PWA Notification"):
		frappe.clear_cache(doctype=doctype)

	frappe.db.sql(
		"""
		update `tabPWA Notification`
		set custom_action_type = 'Approval'
		where coalesce(custom_action_type, '') = ''
			and lower(message) like '%%for approval%%'
		"""
	)
