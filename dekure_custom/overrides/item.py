import re

import frappe
from frappe import _
from frappe.utils import cstr
from frappe.utils.synchronization import filelock
from erpnext.controllers.item_variant import make_variant_item_code


ITEM_GROUP_FIELD = "item_group"
BRAND_FIELD = "brand"
BRAND_DOCTYPE = "Brand"
BRAND_ABBREVIATION_FIELD = "b_abbreviation"
ITEM_NAME_ABBREVIATION_FIELD = "item_abbreviation"
ITEM_VARIANT_FIELD = "variant_of"
ITEM_VARIANT_DOCTYPE = "Item"
ITEM_VARIANT_ABBREVIATION_FIELD = "item_abbreviation"
SPARE_PART_FIELD = "spare_part"
SPARE_PART_DOCTYPE = "Spare Part"
SPARE_PART_ABBREVIATION_FIELD = "abbreviation"
NUMBER_FIELD = "number"
SERVICE_FIELD = "services"
SERVICE_ABBREVIATION_FIELD = "abbreviation"
SERVICE_CODE_PREFIX = "SER"
PRODUCT_ITEM_GROUPS = {"Product", "Products"}
SERVICE_ITEM_GROUPS = {"Services"}
SUPPORTED_ITEM_GROUPS = PRODUCT_ITEM_GROUPS | SERVICE_ITEM_GROUPS
TEMPORARY_ITEM_CODE_PREFIX = "TEMP-ITEM-CODE-"


def generate_item_code(doc, method=None):
	if not should_generate_item_code(doc):
		return

	item_group = doc.get(ITEM_GROUP_FIELD)
	if item_group not in SUPPORTED_ITEM_GROUPS:
		return

	with filelock("dekure_custom_item_code_generation", timeout=30):
		if should_assign_temporary_variant_code(doc):
			item_code = make_temporary_item_code()
			doc.item_code = item_code
			doc.name = item_code
			doc.flags.dekure_custom_item_code_generated = True
			return

		prefix = build_product_prefix(doc) if item_group in PRODUCT_ITEM_GROUPS else build_service_prefix(doc)
		item_code = build_item_code_with_number(doc, prefix)

		if frappe.db.exists("Item", item_code):
			throw_duplicate_item_code(item_code)

		doc.item_code = item_code
		doc.name = item_code
		doc.flags.dekure_custom_item_code_generated = True


def rename_variant_item_code_if_ready(doc, method=None):
	if doc.flags.get("dekure_custom_renaming_item_code") or frappe.flags.get(
		"dekure_custom_renaming_item_code"
	):
		return

	if doc.is_new() or not doc.get(ITEM_VARIANT_FIELD):
		return

	if doc.get(ITEM_GROUP_FIELD) not in PRODUCT_ITEM_GROUPS:
		return

	if not cstr(doc.get(ITEM_NAME_ABBREVIATION_FIELD)).strip():
		return

	current_item_code = cstr(doc.get("item_code")).strip()
	if not current_item_code or not is_temporary_item_code(current_item_code):
		return

	with filelock("dekure_custom_item_code_generation", timeout=30):
		prefix = build_product_prefix(doc)
		generated_item_code = build_item_code_with_number(doc, prefix)

		if frappe.db.exists("Item", generated_item_code):
			throw_duplicate_item_code(generated_item_code)

		doc.flags.dekure_custom_renaming_item_code = True
		frappe.flags.dekure_custom_renaming_item_code = True
		try:
			frappe.rename_doc("Item", doc.name, generated_item_code, force=False, merge=False)
		finally:
			frappe.flags.dekure_custom_renaming_item_code = False


def should_generate_item_code(doc):
	if doc.flags.get("dekure_custom_item_code_generated"):
		return False

	if not doc.is_new():
		return False

	if doc.get(ITEM_GROUP_FIELD) not in SUPPORTED_ITEM_GROUPS:
		return False

	item_code = cstr(doc.get("item_code")).strip()
	return not item_code or is_temporary_item_code(item_code) or is_standard_variant_item_code(doc, item_code)


def is_temporary_item_code(item_code):
	temporary_prefixes = ("New Item", "STO-ITEM-", "TEMP", "TMP", TEMPORARY_ITEM_CODE_PREFIX)
	return item_code in temporary_prefixes or item_code.startswith(temporary_prefixes)


def is_standard_variant_item_code(doc, item_code):
	if not doc.get(ITEM_VARIANT_FIELD):
		return False

	return item_code == get_standard_variant_item_code(doc)


def get_standard_variant_item_code(doc):
	template_item_code = doc.get(ITEM_VARIANT_FIELD)
	template_item_name = frappe.db.get_value("Item", template_item_code, "item_name")
	if not template_item_name:
		return None

	variant = frappe._dict(
		{
			"item_code": None,
			"item_name": None,
			"attributes": [normalize_attribute(attribute) for attribute in doc.get("attributes") or []],
		}
	)
	make_variant_item_code(template_item_code, template_item_name, variant)
	return variant.item_code


def normalize_attribute(attribute):
	if hasattr(attribute, "as_dict"):
		return frappe._dict(attribute.as_dict())

	return frappe._dict(attribute)


def build_product_prefix(doc):
	brand = get_required_link_abbreviation(
		doc,
		BRAND_FIELD,
		BRAND_DOCTYPE,
		BRAND_ABBREVIATION_FIELD,
		_("Brand"),
	)
	item_name = get_product_item_abbreviation(doc)
	item_variant = get_product_variant_abbreviation(doc)

	parts = [brand, item_name]

	if item_variant:
		parts.append(item_variant)

	if doc.get(SPARE_PART_FIELD):
		parts.append(
			get_required_link_abbreviation(
				doc,
				SPARE_PART_FIELD,
				SPARE_PART_DOCTYPE,
				SPARE_PART_ABBREVIATION_FIELD,
				_("Spare Part"),
			)
		)

	return build_prefix(parts)


def get_product_item_abbreviation(doc):
	if doc.get(ITEM_VARIANT_FIELD):
		return get_link_abbreviation(
			ITEM_VARIANT_DOCTYPE,
			doc.get(ITEM_VARIANT_FIELD),
			ITEM_VARIANT_ABBREVIATION_FIELD,
			_("Item"),
		)

	return get_required_field_abbreviation(doc, ITEM_NAME_ABBREVIATION_FIELD, _("Item Abbreviation"))


def get_product_variant_abbreviation(doc):
	if doc.get(ITEM_VARIANT_FIELD):
		return get_required_field_abbreviation(doc, ITEM_NAME_ABBREVIATION_FIELD, _("Item Variant"))

	return None


def should_assign_temporary_variant_code(doc):
	return (
		doc.get(ITEM_GROUP_FIELD) in PRODUCT_ITEM_GROUPS
		and doc.get(ITEM_VARIANT_FIELD)
		and not cstr(doc.get(ITEM_NAME_ABBREVIATION_FIELD)).strip()
	)


def make_temporary_item_code():
	for _attempt in range(10):
		item_code = f"{TEMPORARY_ITEM_CODE_PREFIX}{frappe.generate_hash(length=10).upper()}"
		if not frappe.db.exists("Item", item_code):
			return item_code

	frappe.throw(_("Cannot generate a temporary Item Code. Please try again."))


def build_service_prefix(doc):
	item_name = get_required_field_abbreviation(doc, ITEM_NAME_ABBREVIATION_FIELD, _("Item Abbreviation"))
	service_type = get_service_type_abbreviation(doc)

	return build_prefix([SERVICE_CODE_PREFIX, item_name, service_type])


def get_service_type_abbreviation(doc):
	service_name = doc.get(SERVICE_FIELD)
	if not service_name:
		frappe.throw(_("Cannot generate Item Code because Service Type is not selected."))

	services_field = frappe.get_meta("Item").get_field(SERVICE_FIELD)
	if not services_field:
		frappe.throw(_("Cannot generate Item Code because the Services field is missing."))

	if services_field.fieldtype != "Link" or not services_field.options:
		frappe.throw(_("Cannot generate Item Code because the Services field is not configured as a Link field."))

	service_doctype = services_field.options
	service_meta = frappe.get_meta(service_doctype)
	if not service_meta.has_field(SERVICE_ABBREVIATION_FIELD):
		frappe.throw(
			_("Cannot generate Item Code because abbreviation field {0} does not exist in {1}.").format(
				SERVICE_ABBREVIATION_FIELD,
				service_doctype,
			)
		)

	abbreviation = frappe.db.get_value(service_doctype, service_name, SERVICE_ABBREVIATION_FIELD)
	segment = sanitize_code_segment(abbreviation)
	if not segment:
		frappe.throw(_("Cannot generate Item Code because Service Type abbreviation is missing."))

	return segment


def build_item_code_with_number(doc, prefix):
	number_value = get_required_number(doc)
	return f"{prefix}/{number_value}"


def get_required_number(doc):
	if not doc.meta.has_field(NUMBER_FIELD):
		frappe.throw(_("Cannot generate Item Code because Number field does not exist: {0}.").format(NUMBER_FIELD))

	number_value = sanitize_number(doc.get(NUMBER_FIELD))
	if not number_value:
		frappe.throw(_("Cannot generate Item Code because Number is missing."))

	return number_value


def sanitize_number(value):
	value = cstr(value).strip()
	if not value:
		return ""

	return value.replace("/", "-")


def throw_duplicate_item_code(item_code):
	frappe.throw(_("Item Code {0} already exists. Please enter a different Number.").format(item_code))


def get_required_field_abbreviation(doc, fieldname, label):
	if not doc.meta.has_field(fieldname):
		frappe.throw(_("Cannot generate Item Code because {0} field does not exist: {1}.").format(label, fieldname))

	value = sanitize_code_segment(doc.get(fieldname))
	if not value:
		frappe.throw(_("Cannot generate Item Code because {0} abbreviation is missing.").format(label))

	return value


def get_required_link_abbreviation(doc, fieldname, doctype, abbreviation_field, label):
	if not doc.meta.has_field(fieldname):
		frappe.throw(_("Cannot generate Item Code because {0} field does not exist: {1}.").format(label, fieldname))

	link_name = doc.get(fieldname)
	if not link_name:
		frappe.throw(_("Cannot generate Item Code because {0} is not selected.").format(label))

	return get_link_abbreviation(doctype, link_name, abbreviation_field, label)


def get_link_abbreviation(doctype, name, abbreviation_field, label=None):
	if not frappe.get_meta(doctype).has_field(abbreviation_field):
		frappe.throw(
			_("Cannot generate Item Code because abbreviation field {0} does not exist in {1}.").format(
				abbreviation_field,
				doctype,
			)
		)

	abbreviation = frappe.db.get_value(doctype, name, abbreviation_field)
	segment = sanitize_code_segment(abbreviation)

	if not segment:
		frappe.throw(
			_("Cannot generate Item Code because abbreviation is missing in {0}: {1}.").format(
				label or doctype,
				name,
			)
		)

	return segment


def sanitize_code_segment(value):
	value = cstr(value).strip().upper()
	value = re.sub(r"\s+", "-", value)
	value = value.replace("/", "")
	value = re.sub(r"[^A-Z0-9_-]", "", value)
	value = re.sub(r"-{2,}", "-", value).strip("-_")
	return value


def build_prefix(parts):
	parts = [part for part in parts if part]
	if not parts or any(not part for part in parts):
		frappe.throw(_("Cannot generate Item Code because one or more code segments are blank."))

	return "/".join(parts)
