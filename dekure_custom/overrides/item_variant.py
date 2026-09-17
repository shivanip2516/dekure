from erpnext.controllers import item_variant as erpnext_item_variant
import frappe
from frappe import _
from frappe.utils import cstr

from dekure_custom.overrides.item import (
	ITEM_GROUP_FIELD,
	SPARE_PART_FIELD,
	SUPPORTED_ITEM_GROUPS,
	VARIANT_ATTRIBUTE_FIELD,
	VARIANT_ATTRIBUTE_VALUE_FIELD,
	VARIANT_ATTRIBUTE_VALUE_ROW_FIELD,
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
	selected_spare_parts = set()

	for row in template.attributes:
		selected_value = args.get(_(row.attribute)) or args.get(row.attribute)
		if selected_value:
			selection = resolve_attribute_value_selection(row.attribute, selected_value)
			if selection.spare_part:
				selected_spare_parts.add(selection.spare_part)
			variant_attributes.append(
				{
					"attribute": row.attribute,
					"attribute_value": selection.attribute_value,
					VARIANT_ATTRIBUTE_VALUE_ROW_FIELD: selection.item_attribute_value,
				}
			)

	variant.set("attributes", variant_attributes)
	erpnext_item_variant.copy_attributes_to_variant(template, variant)

	if use_template_image and template.image:
		variant.image = template.image

	if len(selected_spare_parts) == 1:
		variant.spare_part = next(iter(selected_spare_parts))

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
		if not get_existing_variant(item, attribute_values):
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


def resolve_attribute_value_selection(attribute, selected_value):
	row = frappe.db.get_value(
		"Item Attribute Value",
		selected_value,
		["parent", "attribute_value", "spare_part"],
		as_dict=True,
	)
	if row and cstr(row.get("parent")).strip() == cstr(attribute).strip():
		return frappe._dict(
			{
				"attribute_value": row.get("attribute_value"),
				"item_attribute_value": selected_value,
				"spare_part": row.get("spare_part"),
			}
		)

	return frappe._dict({"attribute_value": selected_value, "item_attribute_value": None, "spare_part": None})


def get_existing_variant(item, args):
	resolved_args = {}
	selected_rows = {}

	for attribute, selected_value in args.items():
		selection = resolve_attribute_value_selection(attribute, selected_value)
		resolved_args[attribute] = selection.attribute_value
		if selection.item_attribute_value:
			selected_rows[attribute] = selection.item_attribute_value

	if not selected_rows:
		return erpnext_item_variant.get_variant(item, args=resolved_args)

	candidate = get_existing_variant_for_selected_rows(item, resolved_args, selected_rows)
	if candidate:
		return candidate

	return None


def get_existing_variant_for_variant_doc(doc, args=None):
	if not doc.get("variant_of"):
		return None

	resolved_args = args or {
		row.get(VARIANT_ATTRIBUTE_FIELD): row.get(VARIANT_ATTRIBUTE_VALUE_FIELD)
		for row in doc.get("attributes") or []
		if row.get(VARIANT_ATTRIBUTE_FIELD) and row.get(VARIANT_ATTRIBUTE_VALUE_FIELD)
	}
	selected_rows = {
		row.get(VARIANT_ATTRIBUTE_FIELD): row.get(VARIANT_ATTRIBUTE_VALUE_ROW_FIELD)
		for row in doc.get("attributes") or []
		if row.get(VARIANT_ATTRIBUTE_FIELD) and row.get(VARIANT_ATTRIBUTE_VALUE_ROW_FIELD)
	}

	if selected_rows:
		return get_existing_variant_for_selected_rows(
			doc.get("variant_of"),
			resolved_args,
			selected_rows,
			exclude_variant=doc.get("name"),
		)

	return erpnext_item_variant.get_variant(doc.get("variant_of"), args=resolved_args, variant=doc.get("name"))


def get_existing_variant_for_selected_rows(item, resolved_args, selected_rows, exclude_variant=None):
	first_attribute = next(iter(selected_rows))
	parent_items = frappe.get_all(
		"Item Variant Attribute",
		filters={
			"attribute": first_attribute,
			"attribute_value": resolved_args[first_attribute],
		},
		pluck="parent",
	)

	for parent_item in parent_items:
		if parent_item == exclude_variant:
			continue

		if frappe.db.get_value("Item", parent_item, "variant_of") != item:
			continue

		variant = frappe.get_doc("Item", parent_item)
		if len(variant.get("attributes") or []) != len(resolved_args):
			continue

		matched = True
		for attribute, attribute_value in resolved_args.items():
			expected_row = selected_rows.get(attribute)
			expected_spare_part = get_selected_attribute_value_spare_part(expected_row)
			if not any(
				cstr(row.get(VARIANT_ATTRIBUTE_FIELD)).strip() == cstr(attribute).strip()
				and cstr(row.get(VARIANT_ATTRIBUTE_VALUE_FIELD)).strip() == cstr(attribute_value).strip()
				and (
					not expected_row
					or cstr(row.get(VARIANT_ATTRIBUTE_VALUE_ROW_FIELD)).strip() == cstr(expected_row).strip()
					or (
						not row.get(VARIANT_ATTRIBUTE_VALUE_ROW_FIELD)
						and cstr(variant.get(SPARE_PART_FIELD)).strip() == cstr(expected_spare_part).strip()
					)
				)
				for row in variant.get("attributes") or []
			):
				matched = False
				break

		if matched:
			return parent_item

	return None


def get_selected_attribute_value_spare_part(attribute_value_row):
	if not attribute_value_row:
		return None

	return frappe.db.get_value("Item Attribute Value", attribute_value_row, "spare_part")


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
