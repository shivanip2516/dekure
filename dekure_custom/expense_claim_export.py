import copy
import json

import frappe
import frappe.permissions
from frappe import _
from frappe.core.doctype.access_log.access_log import make_access_log
from frappe.desk import reportview
from frappe.model.qb_query import DatabaseQuery
from frappe.utils import flt


EXPENSE_CLAIM = "Expense Claim"
SUMMARY_TITLE = "Expense Claim Report"
SUMMARY_LABELS = ("Approved", "Pending", "Rejected", "Paid", "Total Expense")


@frappe.whitelist()
@frappe.read_only()
def export_query():
	"""Extend only Expense Claim Report View exports with their amount summary."""
	if frappe.form_dict.get("doctype") != EXPENSE_CLAIM:
		return reportview.export_query()

	from frappe.desk.utils import pop_csv_params

	form_params = reportview.get_form_params()
	form_params["limit_page_length"] = None
	form_params["as_list"] = True
	csv_params = pop_csv_params(form_params)
	export_in_background = int(form_params.pop("export_in_background", 0))

	if export_in_background:
		user_email = frappe.get_cached_value("User", frappe.session.user, "email")
		frappe.enqueue(
			"dekure_custom.expense_claim_export.run_expense_claim_export_job",
			user_email=user_email,
			form_params=form_params,
			csv_params=csv_params,
			queue="long",
			now=frappe.flags.in_test,
		)
		frappe.msgprint(
			_(
				"Your report is being generated in the background. You will receive an email on {0} with a download link once it is ready."
			).format(user_email)
		)
		return

	return _export_expense_claims(form_params, csv_params)


def run_expense_claim_export_job(user_email, form_params, csv_params):
	from frappe.desk.utils import send_report_email

	report_name, file_extension, content = _export_expense_claims(
		form_params, csv_params, populate_response=False
	)
	send_report_email(
		user_email,
		report_name,
		file_extension,
		content,
		attached_to_name=report_name,
	)


def _export_expense_claims(form_params, csv_params, populate_response=True):
	"""Run Frappe's Report View export flow and prepend the scoped summary."""
	from frappe.desk.utils import get_csv_bytes, provide_binary_file
	from frappe.utils.xlsxutils import get_default_xlsx_styles, handle_html, make_xlsx

	form_params = frappe._dict(copy.deepcopy(form_params))
	csv_params = copy.deepcopy(csv_params)
	doctype = form_params.pop("doctype")
	if doctype != EXPENSE_CLAIM:
		frappe.throw(_("Expense Claim export received an invalid DocType."))

	owner_field = f"`tab{doctype}`.`owner`"
	if isinstance(form_params["fields"], list):
		form_params["fields"].append(owner_field)
	elif isinstance(form_params["fields"], tuple):
		form_params["fields"] = form_params["fields"] + (owner_field,)

	file_format_type = form_params.pop("file_format_type")
	title = form_params.pop("title", doctype)
	add_totals_row = 1 if form_params.pop("add_totals_row", None) == "1" else None
	translate_values = 1 if form_params.pop("translate_values", None) == "1" else None

	if selection := form_params.pop("selected_items", None):
		form_params["filters"] = {"name": ("in", json.loads(selection))}

	make_access_log(
		doctype=doctype,
		file_type=file_format_type,
		report_name=form_params.report_name,
		filters=form_params.filters,
	)

	db_query = DatabaseQuery(doctype)
	rows = db_query.execute(**form_params)
	_validate_export_permission(doctype, rows)

	claim_names = _get_unique_claim_names(rows, db_query.fields)
	summary = get_expense_claim_summary(claim_names)

	if add_totals_row:
		rows = reportview.append_totals_row(rows)

	fields_info = reportview.get_field_info(db_query.fields, doctype)
	labels = [info["label"] for info in fields_info]
	sr_label = _("Sr")
	table_data = [[sr_label, *labels]]

	if frappe.local.lang == "en" or not translate_values:
		table_data.extend([index + 1, *list(row)] for index, row in enumerate(rows))
	else:
		translatable_fields = [field["translatable"] for field in fields_info]
		for index, row in enumerate(rows):
			table_data.append(
				[index + 1]
				+ [_(value) if translatable_fields[i] else value for i, value in enumerate(row)]
			)

	table_data = reportview.handle_duration_fieldtype_values(doctype, table_data, db_query.fields)
	data = build_expense_claim_export_data(table_data, summary)

	if file_format_type == "CSV":
		file_extension = "csv"
		content = get_csv_bytes(
			[
				[
					_sanitize_csv_cell(handle_html(value)) if isinstance(value, str) else value
					for value in row
				]
				for row in data
			],
			csv_params,
		)
	elif file_format_type == "Excel":
		file_extension = "xlsx"
		styles = get_default_xlsx_styles(
			columns=[{"fieldname": "sr", "label": sr_label, "fieldtype": "Int"}, *fields_info],
			data=table_data[1:],
			has_total_row=bool(add_totals_row),
		)
		styles = add_summary_xlsx_styles(styles, table_row_offset=len(data) - len(table_data))
		content = make_xlsx(data, doctype, styles=styles).getvalue()
	else:
		frappe.throw(
			title=_("Unsupported file format: {0}").format(file_format_type),
			msg=_("Only CSV and Excel formats are supported for export"),
		)

	if not populate_response:
		return title, file_extension, content

	provide_binary_file(_(title), file_extension, content)


def _validate_export_permission(doctype, rows):
	if frappe.permissions.can_export(doctype):
		return
	if frappe.permissions.can_export(doctype, is_owner=True):
		for row in rows:
			if row[-1] != frappe.session.user:
				raise frappe.PermissionError(_("You are not allowed to export {} doctype").format(doctype))
		return
	raise frappe.PermissionError(_("You are not allowed to export {} doctype").format(doctype))


def _get_unique_claim_names(rows, fields):
	name_index = None
	for index, field in enumerate(fields):
		try:
			field_doctype, fieldname = reportview.parse_field(field)
		except ValueError:
			continue
		if fieldname == "name" and (field_doctype or EXPENSE_CLAIM) == EXPENSE_CLAIM:
			name_index = index
			break

	if name_index is None:
		frappe.throw(_("Expense Claim ID is required to calculate the export summary."))

	return list(dict.fromkeys(row[name_index] for row in rows if row[name_index]))


def get_expense_claim_summary(claim_names):
	"""Calculate totals for the exact permission-filtered claims included in export."""
	summary = {label: 0.0 for label in SUMMARY_LABELS}
	if not claim_names:
		return summary

	claims = frappe.get_list(
		EXPENSE_CLAIM,
		filters={"name": ("in", claim_names)},
		fields=[
			"name",
			"company",
			"docstatus",
			"approval_status",
			"status",
			"base_total_claimed_amount",
			"base_total_sanctioned_amount",
			"total_amount_reimbursed",
			"exchange_rate",
		],
		limit_page_length=0,
	)
	if len(claims) != len(set(claim_names)):
		frappe.throw(_("Some Expense Claims are no longer available for export."), frappe.PermissionError)

	_validate_single_company_currency(claims)
	for claim in claims:
		if claim.docstatus == 2:
			continue

		claimed = flt(claim.base_total_claimed_amount)
		sanctioned = flt(claim.base_total_sanctioned_amount)
		reimbursed = flt(claim.total_amount_reimbursed) * flt(claim.exchange_rate or 1)

		if claim.docstatus == 1 and claim.approval_status == "Approved":
			summary["Approved"] += sanctioned
		elif claim.docstatus == 0 and claim.approval_status == "Draft":
			summary["Pending"] += claimed
		elif claim.docstatus == 1 and claim.approval_status == "Rejected":
			summary["Rejected"] += claimed

		if claim.docstatus == 1 and claim.status == "Paid":
			summary["Paid"] += reimbursed

		summary["Total Expense"] += claimed

	return summary


def _validate_single_company_currency(claims):
	companies = sorted({claim.company for claim in claims if claim.company})
	if not companies:
		return

	company_rows = frappe.get_list(
		"Company",
		filters={"name": ("in", companies)},
		fields=["name", "default_currency"],
		limit_page_length=0,
	)
	currencies = {row.default_currency for row in company_rows if row.default_currency}
	if len(company_rows) != len(companies) or len(currencies) > 1:
		frappe.throw(
			_(
				"Expense Claim summary cannot combine companies with different base currencies. Filter the report to one company or one company currency before exporting."
			)
		)


def build_expense_claim_export_data(table_data, summary):
	return [
		[_(SUMMARY_TITLE)],
		[_("Summary"), _("Amount")],
		*[[_(label), summary[label]] for label in SUMMARY_LABELS],
		[],
		*table_data,
	]


def _sanitize_csv_cell(value):
	"""Keep spreadsheet applications from evaluating exported text as formulas."""
	if value and value[0] in ("=", "+", "-", "@", "\t", "\r"):
		return f"'{value}"
	return value


def add_summary_xlsx_styles(styles, table_row_offset):
	"""Shift standard table row styles and style the prepended summary rows."""
	styles = copy.deepcopy(styles or {})
	style_registry = styles.setdefault("styles", [])
	bold_style = len(style_registry)
	style_registry.append({"bold": True})
	amount_style = len(style_registry)
	style_registry.append({"num_format": "General", "align": "right"})

	styles["row_styles"] = {
		row + table_row_offset: style_ids
		for row, style_ids in (styles.get("row_styles") or {}).items()
	}
	styles["cell_styles"] = {
		(row + table_row_offset, column): style_ids
		for (row, column), style_ids in (styles.get("cell_styles") or {}).items()
	}
	styles["row_styles"][0] = [bold_style]
	styles["row_styles"][1] = [bold_style]
	for row in range(2, 2 + len(SUMMARY_LABELS)):
		styles["cell_styles"][(row, 0)] = [bold_style]
		styles["cell_styles"][(row, 1)] = [amount_style]
	return styles
