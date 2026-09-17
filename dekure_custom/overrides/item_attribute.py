import frappe
from frappe import _
from frappe.utils import cstr

from erpnext.stock.doctype.item_attribute.item_attribute import ItemAttribute


class DekureItemAttribute(ItemAttribute):
	def validate_duplication(self):
		value_spare_part_pairs = set()
		abbr_spare_part_pairs = set()

		for row in self.item_attribute_values:
			attribute_value = cstr(row.attribute_value).strip()
			spare_part = cstr(row.get("spare_part")).strip()
			value_spare_part_pair = (attribute_value.lower(), spare_part.lower())

			if value_spare_part_pair in value_spare_part_pairs:
				frappe.throw(
					_("Attribute value: {0} with Spare Part: {1} must appear only once").format(
						attribute_value,
						spare_part or _("blank"),
					)
				)
			value_spare_part_pairs.add(value_spare_part_pair)

			abbr = cstr(row.abbr).strip()
			if not abbr:
				continue

			abbr_spare_part_pair = (abbr.lower(), spare_part.lower())
			if abbr_spare_part_pair in abbr_spare_part_pairs:
				frappe.throw(
					_("Abbreviation: {0} with Spare Part: {1} must appear only once").format(
						abbr,
						spare_part or _("blank"),
					)
				)
			abbr_spare_part_pairs.add(abbr_spare_part_pair)
