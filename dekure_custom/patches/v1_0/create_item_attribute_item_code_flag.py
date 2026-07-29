import frappe


def execute():
	if frappe.get_meta("Item Attribute", cached=False).has_field("use_for_item_code"):
		return

	if frappe.db.exists("Custom Field", "Item Attribute-use_for_item_code"):
		frappe.clear_cache(doctype="Item Attribute")
		return

	frappe.get_doc(
		{
			"doctype": "Custom Field",
			"dt": "Item Attribute",
			"fieldname": "use_for_item_code",
			"fieldtype": "Check",
			"label": "Use for Item Code",
			"insert_after": "disabled",
			"default": "0",
		}
	).insert(ignore_permissions=True)

	frappe.clear_cache(doctype="Item Attribute")
