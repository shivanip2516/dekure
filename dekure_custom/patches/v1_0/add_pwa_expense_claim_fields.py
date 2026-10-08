import frappe


CUSTOM_FIELDS = (
	{
		"name": "Expense Claim-custom_pwa_expense_claim",
		"dt": "Expense Claim",
		"fieldname": "custom_pwa_expense_claim",
		"fieldtype": "Check",
		"label": "PWA Expense Claim",
		"insert_after": "posting_date",
		"hidden": 1,
		"default": "0",
	},
	{
		"name": "Expense Claim Detail-custom_attachment",
		"dt": "Expense Claim Detail",
		"fieldname": "custom_attachment",
		"fieldtype": "Attach",
		"label": "Attachment",
		"insert_after": "amount",
		"in_list_view": 1,
		"reqd": 0,
		"read_only": 0,
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

	for doctype in ("Expense Claim", "Expense Claim Detail"):
		frappe.clear_cache(doctype=doctype)
