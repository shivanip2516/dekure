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
	make_safe_standard_variant_item_code,
)


def create_variant(item, args, use_template_image=False):
	use_template_image = frappe.parse_json(use_template_image)
	if isinstance(args, str):
		args = frappe.parse_json(args)

	template = frappe.get_doc("Item", item)
	variant = frappe.new_doc("Item")
	variant.variant_based_on = "Item Attribute"
	variant_attributes = []

	for row in template.attributes:
		attribute_value = args.get(_(row.attribute)) or args.get(row.attribute)
		if attribute_value:
			variant_attributes.append({"attribute": row.attribute, "attribute_value": attribute_value})

	variant.set("attributes", variant_attributes)
	erpnext_item_variant.copy_attributes_to_variant(template, variant)

	if use_template_image and template.image:
		variant.image = template.image

	make_safe_standard_variant_item_code(template.item_code, template.item_name, variant)
	set_variant_item_name_from_attributes(item, variant)
	clear_standard_variant_item_code(variant)
	return variant


def create_variant_doc_for_quick_entry(template, args):
	variant_based_on = frappe.db.get_value("Item", template, "variant_based_on")
	args = frappe.parse_json(args) if isinstance(args, str) else args
	if variant_based_on == "Manufacturer":
		variant = erpnext_item_variant.get_variant(template, **args)
	else:
		existing_variant = erpnext_item_variant.get_variant(template, args)
		if existing_variant:
			return existing_variant

		variant = create_variant(template, args=args)
		variant.name = variant.item_code
		erpnext_item_variant.validate_item_variant_attributes(variant, args)

	return variant.as_dict()


def set_variant_item_name_from_attributes(template, variant):
	doc = frappe._dict(variant) if isinstance(variant, dict) else variant
	template_item_name = frappe.db.get_value("Item", template, "item_name")
	if not template_item_name:
		return

	attribute_parts = []
	for row in doc.get("attributes") or []:
		attribute_name = cstr(row.get(VARIANT_ATTRIBUTE_FIELD)).strip()
		attribute_value = cstr(row.get(VARIANT_ATTRIBUTE_VALUE_FIELD)).strip()
		if not attribute_name or not attribute_value:
			continue

		attribute_parts.extend([attribute_name, attribute_value])

	if not attribute_parts:
		return

	item_name = "{} - {}".format(template_item_name, " - ".join(attribute_parts))
	if isinstance(variant, dict):
		variant["item_name"] = item_name
	else:
		variant.item_name = item_name


@frappe.whitelist()
def enqueue_multiple_variant_creation(item, args, use_template_image=False):
	use_template_image = frappe.parse_json(use_template_image)
	if isinstance(args, str):
		args = frappe.parse_json(args)

	total_variants = get_independent_variant_count(args)

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

	for attribute_values in get_independent_attribute_values(args):
		if not erpnext_item_variant.get_variant(item, args=attribute_values):
			variant = create_variant(item, attribute_values)
			if use_template_image and template_item.image:
				variant.image = template_item.image
			variant.save()
			count += 1

	return count


def get_independent_variant_count(args):
	return sum(len(values) for values in args.values() if values)


def get_independent_attribute_values(args):
	for attribute, values in args.items():
		for value in values or []:
			yield {attribute: value}


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
