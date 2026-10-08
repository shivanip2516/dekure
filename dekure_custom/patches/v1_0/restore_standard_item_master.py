import frappe


ITEM_CUSTOM_FIELDS = (
	"Item-spare_part",
	"Item-services",
	"Item-item_abbreviation",
	"Item Attribute-use_for_item_code",
	"Item Attribute Value-spare_part",
	"Item Variant Attribute-item_attribute_value",
)


def execute():
	delete_custom_fields()
	delete_item_property_setters()

	for doctype in (
		"Item",
		"Item Attribute",
		"Item Attribute Value",
		"Item Variant Attribute",
	):
		frappe.clear_cache(doctype=doctype)


def delete_custom_fields():
	for custom_field in ITEM_CUSTOM_FIELDS:
		if frappe.db.exists("Custom Field", custom_field):
			frappe.delete_doc(
				"Custom Field",
				custom_field,
				ignore_permissions=True,
				force=True,
			)


def delete_item_property_setters():
	for property_setter in frappe.get_all(
		"Property Setter",
		filters={"doc_type": "Item"},
		pluck="name",
	):
		frappe.delete_doc(
			"Property Setter",
			property_setter,
			ignore_permissions=True,
			force=True,
		)
