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
SERVICE_FIELD = "services"
SERVICE_ABBREVIATION_FIELD = "abbreviation"
SERVICE_CODE_PREFIX = "SER"
PRODUCT_ITEM_GROUPS = {"Product", "Products"}
SERVICE_ITEM_GROUPS = {"Services"}
SUPPORTED_ITEM_GROUPS = PRODUCT_ITEM_GROUPS | SERVICE_ITEM_GROUPS
TEMPORARY_ITEM_CODE_PREFIX = "TEMP-ITEM-CODE-"
VARIANT_ATTRIBUTES_FIELD = "attributes"
VARIANT_ATTRIBUTE_DOCTYPE = "Item Variant Attribute"
VARIANT_ATTRIBUTE_FIELD = "attribute"
VARIANT_ATTRIBUTE_VALUE_FIELD = "attribute_value"
ITEM_ATTRIBUTE_DOCTYPE = "Item Attribute"
ITEM_ATTRIBUTE_VALUES_FIELD = "item_attribute_values"
ITEM_ATTRIBUTE_VALUE_DOCTYPE = "Item Attribute Value"
ITEM_ATTRIBUTE_VALUE_FIELD = "attribute_value"
ITEM_ATTRIBUTE_VALUE_ABBREVIATION_FIELD = "abbr"
USE_FOR_ITEM_CODE_FIELD = "use_for_item_code"


def before_insert_item(doc, method=None):
	if doc.get(ITEM_VARIANT_FIELD):
		populate_variant_fields_from_item_attribute(doc)

	generate_item_code(doc, method=method)


def generate_item_code(doc, method=None):
	if not should_generate_item_code(doc):
		return

	item_group = doc.get(ITEM_GROUP_FIELD)
	if item_group not in SUPPORTED_ITEM_GROUPS:
		return

	with filelock("dekure_custom_item_code_generation", timeout=30):
		item_code = build_product_prefix(doc) if item_group in PRODUCT_ITEM_GROUPS else build_service_prefix(doc)

		if frappe.db.exists("Item", item_code):
			throw_duplicate_item_code(item_code)

		doc.item_code = item_code
		doc.name = item_code
		doc.flags.dekure_custom_item_code_generated = True


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


def throw_duplicate_item_code(item_code):
	frappe.throw(_("Item Code {0} already exists. Check the selected Item attributes and abbreviations.").format(item_code))


def populate_variant_fields_from_item_attribute(doc):
	if not doc.get(ITEM_VARIANT_FIELD):
		return

	selected = get_item_code_variant_attribute(doc)
	attribute_value = selected[VARIANT_ATTRIBUTE_VALUE_FIELD]
	attribute_abbreviation = get_attribute_value_abbreviation(
		attribute_name=selected[VARIANT_ATTRIBUTE_FIELD],
		attribute_value=attribute_value,
	)

	set_doc_value(doc, ITEM_NAME_ABBREVIATION_FIELD, sanitize_code_segment(attribute_abbreviation))


def get_item_code_variant_attribute(doc):
	validate_variant_attribute_metadata()
	configured_rows = []

	for row in doc.get(VARIANT_ATTRIBUTES_FIELD) or []:
		attribute_name = cstr(row.get(VARIANT_ATTRIBUTE_FIELD)).strip()
		attribute_value = cstr(row.get(VARIANT_ATTRIBUTE_VALUE_FIELD)).strip()

		if not attribute_name:
			continue

		if not attribute_value:
			frappe.throw(_("Cannot generate Variant Item Code because the selected Attribute Value is missing."))

		if frappe.db.get_value(ITEM_ATTRIBUTE_DOCTYPE, attribute_name, USE_FOR_ITEM_CODE_FIELD):
			configured_rows.append(
				{
					VARIANT_ATTRIBUTE_FIELD: attribute_name,
					VARIANT_ATTRIBUTE_VALUE_FIELD: attribute_value,
				}
			)

	if not configured_rows:
		frappe.throw(
			_(
				"Cannot generate Variant Item Code because no selected Item Attribute is marked as Use for Item Code."
			)
		)

	if len(configured_rows) > 1:
		frappe.throw(
			_(
				"Cannot generate Variant Item Code because multiple selected Item Attributes are marked as Use for Item Code."
			)
		)

	return configured_rows[0]


def get_attribute_value_abbreviation(attribute_name, attribute_value):
	validate_item_attribute_value_metadata()
	attribute_doc = frappe.get_doc(ITEM_ATTRIBUTE_DOCTYPE, attribute_name)

	for row in attribute_doc.get(ITEM_ATTRIBUTE_VALUES_FIELD) or []:
		row_attribute_value = cstr(row.get(ITEM_ATTRIBUTE_VALUE_FIELD)).strip()
		if row_attribute_value == cstr(attribute_value).strip():
			abbreviation = cstr(row.get(ITEM_ATTRIBUTE_VALUE_ABBREVIATION_FIELD)).strip()
			if not abbreviation:
				frappe.throw(
					_("Abbreviation is missing for Attribute Value {0} in Item Attribute {1}.").format(
						attribute_value,
						attribute_name,
					)
				)

			return abbreviation

	frappe.throw(
		_("Attribute Value {0} was not found in Item Attribute {1}.").format(
			attribute_value,
			attribute_name,
		)
	)


def validate_variant_attribute_metadata():
	item_meta = frappe.get_meta("Item")
	attributes_field = item_meta.get_field(VARIANT_ATTRIBUTES_FIELD)
	if not attributes_field or attributes_field.fieldtype != "Table":
		frappe.throw(_("Cannot generate Variant Item Code because Item Variant Attributes table is not configured."))

	variant_attribute_doctype = attributes_field.options
	if variant_attribute_doctype != VARIANT_ATTRIBUTE_DOCTYPE:
		frappe.throw(
			_("Cannot generate Variant Item Code because Item attributes table points to {0}, expected {1}.").format(
				variant_attribute_doctype,
				VARIANT_ATTRIBUTE_DOCTYPE,
			)
		)

	variant_meta = frappe.get_meta(variant_attribute_doctype)
	for fieldname in (VARIANT_ATTRIBUTE_FIELD, VARIANT_ATTRIBUTE_VALUE_FIELD):
		if not variant_meta.has_field(fieldname):
			frappe.throw(
				_("Cannot generate Variant Item Code because field {0} is missing in {1}.").format(
					fieldname,
					variant_attribute_doctype,
				)
			)


def validate_item_attribute_value_metadata():
	attribute_meta = frappe.get_meta(ITEM_ATTRIBUTE_DOCTYPE)
	table_field = attribute_meta.get_field(ITEM_ATTRIBUTE_VALUES_FIELD)
	if not table_field or table_field.fieldtype != "Table" or table_field.options != ITEM_ATTRIBUTE_VALUE_DOCTYPE:
		frappe.throw(_("Cannot generate Variant Item Code because Item Attribute Values table is not configured."))

	if not attribute_meta.has_field(USE_FOR_ITEM_CODE_FIELD):
		frappe.throw(_("Cannot generate Variant Item Code because Use for Item Code field is missing in Item Attribute."))

	child_meta = frappe.get_meta(ITEM_ATTRIBUTE_VALUE_DOCTYPE)
	for fieldname in (ITEM_ATTRIBUTE_VALUE_FIELD, ITEM_ATTRIBUTE_VALUE_ABBREVIATION_FIELD):
		if not child_meta.has_field(fieldname):
			frappe.throw(
				_("Cannot generate Variant Item Code because field {0} is missing in {1}.").format(
					fieldname,
					ITEM_ATTRIBUTE_VALUE_DOCTYPE,
				)
			)


def set_doc_value(doc, fieldname, value):
	if hasattr(doc, "set"):
		doc.set(fieldname, value)
	else:
		doc[fieldname] = value


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
