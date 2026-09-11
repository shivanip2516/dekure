from erpnext.controllers import item_variant as erpnext_item_variant
import frappe
from frappe import _
from frappe.utils import cstr

from dekure_custom.overrides.item import (
	ITEM_GROUP_FIELD,
	SUPPORTED_ITEM_GROUPS,
	VARIANT_ATTRIBUTE_FIELD,
	VARIANT_ATTRIBUTE_VALUE_FIELD,
	is_standard_variant_item_code,
	is_temporary_item_code,
)


def create_variant(item, args, use_template_image=False):
	variant = erpnext_item_variant.create_variant(item, args, use_template_image=use_template_image)
	set_variant_item_name_from_attributes(item, variant)
	clear_standard_variant_item_code(variant)
	return variant


def create_variant_doc_for_quick_entry(template, args):
	variant = erpnext_item_variant.create_variant_doc_for_quick_entry(template, args)

	if isinstance(variant, dict):
		set_variant_item_name_from_attributes(template, variant)
		clear_standard_variant_item_code(variant)

	return variant


def set_variant_item_name_from_attributes(template, variant):
	doc = frappe._dict(variant) if isinstance(variant, dict) else variant
	template_item_name = frappe.db.get_value("Item", template, "item_name")
	if not template_item_name:
		return

	attribute_values = []
	for row in doc.get("attributes") or []:
		attribute_name = cstr(row.get(VARIANT_ATTRIBUTE_FIELD)).strip()
		attribute_value = cstr(row.get(VARIANT_ATTRIBUTE_VALUE_FIELD)).strip()
		if not attribute_name or not attribute_value:
			continue

		attribute_values.append(attribute_value)

	if not attribute_values:
		return

	item_name = "{}-{}".format(template_item_name, "-".join(attribute_values))
	if isinstance(variant, dict):
		variant["item_name"] = item_name
	else:
		variant.item_name = item_name


@frappe.whitelist()
def enqueue_multiple_variant_creation(item, args, use_template_image=False):
	use_template_image = frappe.parse_json(use_template_image)
	if isinstance(args, str):
		args = frappe.parse_json(args)

	total_variants = 1
	for key in args:
		total_variants *= len(args[key])

	if total_variants >= 600:
		frappe.throw(_("Please do not create more than 500 items at a time"))

	if total_variants < 10:
		return create_multiple_variants(item, args, use_template_image)

	frappe.enqueue(
		"dekure_custom.overrides.item_variant.create_multiple_variants",
		item=item,
		args=args,
		use_template_image=use_template_image,
		now=frappe.in_test,
	)
	return "queued"


def create_multiple_variants(item, args, use_template_image=False):
	count = 0
	if isinstance(args, str):
		args = frappe.parse_json(args)

	template_item = frappe.get_doc("Item", item)

	for attribute_values in erpnext_item_variant.generate_keyed_value_combinations(args):
		if not erpnext_item_variant.get_variant(item, args=attribute_values):
			variant = create_variant(item, attribute_values)
			if use_template_image and template_item.image:
				variant.image = template_item.image
			variant.save()
			count += 1

	return count


def clear_standard_variant_item_code(variant):
	doc = frappe._dict(variant) if isinstance(variant, dict) else variant
	item_group = doc.get(ITEM_GROUP_FIELD)
	item_code = cstr(doc.get("item_code")).strip()

	if item_group not in SUPPORTED_ITEM_GROUPS or not item_code:
		return

	if not (is_temporary_item_code(item_code) or is_standard_variant_item_code(doc, item_code)):
		return

	if isinstance(variant, dict):
		variant["item_code"] = None
		variant["name"] = None
	else:
		variant.item_code = None
		variant.name = None
