import frappe
from frappe import _
from frappe.utils import cstr

from erpnext.stock.doctype.item_attribute.item_attribute import ItemAttribute


class DekureItemAttribute(ItemAttribute):
	def validate_duplication(self):
		values = []
		abbrs = []

		for row in self.item_attribute_values:
			attribute_value = cstr(row.attribute_value).strip()
			if attribute_value.lower() in map(str.lower, values):
				frappe.throw(
					_("Attribute value: {0} must appear only once").format(attribute_value.title())
				)
			values.append(attribute_value)

			abbr = cstr(row.abbr).strip()
			if not abbr:
				continue

			if abbr.lower() in map(str.lower, abbrs):
				frappe.throw(_("Abbreviation: {0} must appear only once").format(abbr.title()))
			abbrs.append(abbr)
