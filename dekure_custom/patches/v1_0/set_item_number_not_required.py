import frappe


def execute():
	if not frappe.get_meta("Item", cached=False).has_field("number"):
		return

	property_setter = {
		"name": "Item-number-reqd",
		"doctype_or_field": "DocField",
		"doc_type": "Item",
		"field_name": "number",
		"property": "reqd",
		"property_type": "Check",
		"value": "0",
	}

	if frappe.db.exists("Property Setter", property_setter["name"]):
		frappe.db.set_value(
			"Property Setter",
			property_setter["name"],
			property_setter,
			update_modified=False,
		)
	else:
		frappe.get_doc({"doctype": "Property Setter", **property_setter}).insert(
			ignore_permissions=True
		)

	frappe.clear_cache(doctype="Item")
