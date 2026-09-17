import frappe
from frappe import _
from frappe.utils import cstr

from erpnext.controllers.item_variant import (
	ItemVariantExistsError,
	validate_item_variant_attributes,
)
from erpnext.stock.doctype.item.item import Item

from dekure_custom.overrides.item_variant import get_existing_variant_for_variant_doc


class DekureItem(Item):
	def validate_variant_attributes(self):
		if self.is_new() and self.variant_of and self.variant_based_on == "Item Attribute":
			self.attributes = [row for row in self.attributes if cstr(row.attribute_value).strip()]

			args = {}
			for index, row in enumerate(self.attributes):
				row.idx = index + 1
				args[row.attribute] = row.attribute_value

			variant = get_existing_variant_for_variant_doc(self, args)
			if variant:
				frappe.throw(
					_("Item variant {0} exists with same attributes").format(variant),
					ItemVariantExistsError,
				)

			validate_item_variant_attributes(self, args)

			for row in self.attributes:
				row.variant_of = self.variant_of
