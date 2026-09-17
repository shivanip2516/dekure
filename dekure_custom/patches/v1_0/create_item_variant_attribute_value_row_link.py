import frappe


def execute():
	if frappe.get_meta("Item Variant Attribute", cached=False).has_field("item_attribute_value"):
		return

	if frappe.db.exists("Custom Field", "Item Variant Attribute-item_attribute_value"):
		frappe.clear_cache(doctype="Item Variant Attribute")
		return

	frappe.get_doc(
		{
			"doctype": "Custom Field",
			"dt": "Item Variant Attribute",
			"fieldname": "item_attribute_value",
			"fieldtype": "Link",
			"label": "Item Attribute Value Row",
			"options": "Item Attribute Value",
			"insert_after": "attribute_value",
			"hidden": 1,
			"read_only": 1,
			"reqd": 0,
			"module": "Dekure Custom",
		}
	).insert(ignore_permissions=True)

	frappe.clear_cache(doctype="Item Variant Attribute")
