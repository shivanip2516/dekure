import frappe


PROPERTY_SETTERS = (
	{
		"name": "Item-item_code-reqd",
		"doctype_or_field": "DocField",
		"doc_type": "Item",
		"field_name": "item_code",
		"property": "reqd",
		"property_type": "Check",
		"value": "0",
	},
	{
		"name": "Item-item_code-read_only",
		"doctype_or_field": "DocField",
		"doc_type": "Item",
		"field_name": "item_code",
		"property": "read_only",
		"property_type": "Check",
		"value": "1",
	},
)


def execute():
	for property_setter in PROPERTY_SETTERS:
		create_or_update_property_setter(property_setter)

	frappe.clear_cache(doctype="Item")


def create_or_update_property_setter(property_setter):
	if frappe.db.exists("Property Setter", property_setter["name"]):
		frappe.db.set_value(
			"Property Setter",
			property_setter["name"],
			property_setter,
			update_modified=False,
		)
		return

	doc = frappe.get_doc({"doctype": "Property Setter", **property_setter})
	doc.insert(ignore_permissions=True)
