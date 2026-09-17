import frappe


def execute():
	if frappe.get_meta("Item Attribute Value", cached=False).has_field("spare_part"):
		return

	if frappe.db.exists("Custom Field", "Item Attribute Value-spare_part"):
		frappe.clear_cache(doctype="Item Attribute Value")
		return

	frappe.get_doc(
		{
			"doctype": "Custom Field",
			"dt": "Item Attribute Value",
			"fieldname": "spare_part",
			"fieldtype": "Link",
			"label": "Spare Part",
			"options": "Spare Part",
			"insert_after": "abbr",
			"reqd": 0,
			"module": "Dekure Custom",
		}
	).insert(ignore_permissions=True)

	frappe.clear_cache(doctype="Item Attribute Value")
