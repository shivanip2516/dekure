import copy
import json
from collections import Counter
from decimal import Decimal
from io import BytesIO

import frappe
import frappe.permissions
from frappe import _
from frappe.core.doctype.access_log.access_log import make_access_log
from frappe.desk import reportview
from frappe.model.qb_query import DatabaseQuery
from frappe.types.filter import Filters
from frappe.utils import cint


EXPENSE_CLAIM = "Expense Claim"
EXPENSE_DETAIL = "Expense Claim Detail"
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
	form_params.pop("with_link_titles", None)
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
	from frappe.utils.xlsxutils import get_default_xlsx_styles, handle_html

	form_params = frappe._dict(copy.deepcopy(form_params))
	csv_params = copy.deepcopy(csv_params)
	doctype = form_params.pop("doctype")
	if doctype != EXPENSE_CLAIM:
		frappe.throw(_("Expense Claim export received an invalid DocType."))

	form_params.pop("with_link_titles", None)
	visible_names = form_params.pop("visible_names", None)
	if isinstance(visible_names, str):
		visible_names = json.loads(visible_names)
	selection = form_params.pop("selected_items", None)
	if isinstance(selection, str):
		selection = json.loads(selection)
	for names in (selection, visible_names):
		if names is not None:
			filters = Filters(form_params.get("filters") or [], doctype=doctype)
			filters.append((doctype, "name", "in", names))
			form_params["filters"] = filters
	if visible_names is not None:
		# The client has already selected the displayed page. Apply its scope only
		# once, retaining child filters instead of expanding back to whole claims.
		for key in ("start", "offset", "limit_start", "page_length", "limit_page_length", "limit"):
			form_params.pop(key, None)

	# Grouped reports already export their native aggregate columns. Adding raw
	# detail identities would change their GROUP BY/DISTINCT result semantics.
	if form_params.get("group_by") or cint(form_params.get("distinct")) or any(
		isinstance(field, dict) or "(" in field for field in form_params.get("fields", [])
	):
		return reportview._export_query(
			dict(form_params, doctype=doctype), csv_params, populate_response=populate_response
		)

	detail_scope = _has_detail_scope(form_params)
	visible_fields = list(form_params["fields"])
	internal_fields = [f"`tab{doctype}`.`name`"]
	if detail_scope:
		internal_fields.extend(
			f"`tab{EXPENSE_DETAIL}`.`{field}`"
			for field in ("name", "base_amount", "base_sanctioned_amount")
		)
	owner_field = f"`tab{doctype}`.`owner`"
	form_params["fields"] = [*visible_fields, *internal_fields, owner_field]

	file_format_type = form_params.pop("file_format_type")
	title = form_params.pop("title", doctype)
	add_totals_row = 1 if form_params.pop("add_totals_row", None) == "1" else None
	translate_values = 1 if form_params.pop("translate_values", None) == "1" else None

	make_access_log(
		doctype=doctype,
		file_type=file_format_type,
		report_name=form_params.report_name,
		filters=form_params.filters,
	)

	db_query = DatabaseQuery(doctype)
	rows = db_query.execute(**form_params)
	_validate_export_permission(doctype, rows)
	if visible_names is not None:
		rows = reportview._reorder_by_visible_names(rows, db_query.fields, doctype, visible_names)

	claim_index = len(visible_fields)
	claim_names = list(dict.fromkeys(row[claim_index] for row in rows if row[claim_index]))
	details = None
	if detail_scope:
		details = [
			frappe._dict(
				parent=row[claim_index],
				name=row[claim_index + 1],
				base_amount=row[claim_index + 2],
				base_sanctioned_amount=row[claim_index + 3],
			)
			for row in rows
			if row[claim_index + 1]
		]
	claims = _get_summary_claims(claim_names, detail_scope)
	summary = get_expense_claim_summary(claim_names, details, claim_rows=claims)
	excel_records = _get_excel_summary_records(rows, claim_index, detail_scope, claims) if file_format_type == "Excel" else []
	# Internal fields are queried in the same row selection but never exported.
	rows = [tuple(row[: len(visible_fields)]) + (row[-1],) for row in rows]
	export_fields = [*visible_fields, owner_field]

	if add_totals_row:
		rows = reportview.append_totals_row(rows)

	fields_info = reportview.get_field_info(export_fields, doctype)
	if detail_scope:
		for field, info in zip(export_fields, fields_info, strict=True):
			field_doctype, fieldname = reportview.parse_field(field)
			if (field_doctype or doctype) == doctype and fieldname in {
				"grand_total",
				"total_claimed_amount",
				"total_sanctioned_amount",
				"base_grand_total",
				"base_total_claimed_amount",
				"base_total_sanctioned_amount",
				"total_amount_reimbursed",
			}:
				info["label"] += " " + _("(Full Claim)")
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

	table_data = reportview.handle_duration_fieldtype_values(doctype, table_data, export_fields)
	data = build_expense_claim_export_data(table_data, summary, include_payment_note=file_format_type != "Excel")

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
		content = _make_filter_aware_xlsx(data, styles, excel_records, summary, table_data)
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


def _has_detail_scope(params):
	for field in params.get("fields", []):
		if reportview.parse_field(field)[0] == EXPENSE_DETAIL or field.startswith("expenses."):
			return True
	for key in ("filters", "or_filters"):
		for condition in Filters(params.get(key) or [], doctype=EXPENSE_CLAIM):
			if condition.doctype == EXPENSE_DETAIL or condition.fieldname.startswith("expenses."):
				return True
	return False


def _get_summary_claims(claim_names, detail_scope):
	if not claim_names:
		return []
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
		]
		+ ([f"`tab{EXPENSE_DETAIL}`.`name` as expense_detail_name"] if detail_scope else []),
		limit_page_length=0,
	)
	if {claim.name for claim in claims} != set(claim_names):
		frappe.throw(_("Some Expense Claims are no longer available for export."), frappe.PermissionError)

	_validate_single_company_currency(claims)
	return claims


def _row_summary_amounts(claim, claimed, sanctioned):
	"""The same status/amount definitions feed CSV totals and Excel helper values."""
	amounts = {label: Decimal(0) for label in SUMMARY_LABELS}
	if claim.docstatus == 2:
		return amounts
	if claim.docstatus == 1 and claim.approval_status == "Approved":
		amounts["Approved"] = sanctioned
	elif claim.docstatus == 0 and claim.approval_status == "Draft":
		amounts["Pending"] = claimed
	elif claim.docstatus == 1 and claim.approval_status == "Rejected":
		amounts["Rejected"] = claimed
	if claim.docstatus == 1 and claim.status == "Paid":
		amounts["Paid"] = Decimal(str(claim.total_amount_reimbursed or 0)) * Decimal(str(claim.exchange_rate or 1))
	amounts["Total Expense"] = claimed
	return amounts


def get_expense_claim_summary(claim_names, expense_details=None, *, claim_rows=None):
	"""Calculate totals for the exact permission-filtered claims included in export."""
	summary = {label: Decimal(0) for label in SUMMARY_LABELS}
	if not claim_names:
		return {label: 0.0 for label in SUMMARY_LABELS}
	claims = claim_rows if claim_rows is not None else _get_summary_claims(claim_names, expense_details is not None)
	included_details = {}
	all_details = {}
	for detail in expense_details or []:
		included_details.setdefault(detail.parent, {})[detail.name] = detail
	for claim in claims:
		all_details.setdefault(claim.name, set()).add(claim.get("expense_detail_name"))
	for claim in {claim.name: claim for claim in claims}.values():
		if claim.docstatus == 2:
			continue

		claimed = Decimal(str(claim.base_total_claimed_amount or 0))
		sanctioned = Decimal(str(claim.base_total_sanctioned_amount or 0))
		if expense_details is not None:
			details = included_details.get(claim.name, {})
			claimed = sum((Decimal(str(d.base_amount or 0)) for d in details.values()), Decimal(0))
			sanctioned = sum(
				(Decimal(str(d.base_sanctioned_amount or 0)) for d in details.values()), Decimal(0)
			)

		amounts = _row_summary_amounts(claim, claimed, sanctioned)
		for label in SUMMARY_LABELS:
			if label != "Paid":
				summary[label] += amounts[label]
		if amounts["Paid"]:
			if expense_details is not None and set(details) != all_details[claim.name]:
				# Payments refer to the claim, not an expense detail. Do not invent
				# an allocation (taxes and advances can also affect reimbursement).
				summary["Paid"] = None
			elif summary["Paid"] is not None:
				summary["Paid"] += amounts["Paid"]

	return {label: float(value) if value is not None else None for label, value in summary.items()}


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


def build_expense_claim_export_data(table_data, summary, *, include_payment_note=True):
	notes = []
	if summary["Paid"] is None and include_payment_note:
		notes.append(
			[_(
				"Paid is unavailable: payments belong to whole claims and cannot be attributed to the included expense details."
			)]
		)
	return [
		[_(SUMMARY_TITLE)],
		[_("Summary"), _("Amount (Company Currency)")],
		*[[_(label), summary[label]] for label in SUMMARY_LABELS],
		*notes,
		[],
		*table_data,
	]


def _get_excel_summary_records(rows, claim_index, detail_scope, claims):
	"""Keep row identities and amounts aligned with the exported (possibly repeated) rows."""
	by_name = {claim.name: claim for claim in claims}
	all_details = {}
	for claim in claims:
		if claim.get("expense_detail_name"):
			all_details.setdefault(claim.name, set()).add(claim.expense_detail_name)
	records = []
	for row in rows:
		claim = by_name[row[claim_index]]
		if detail_scope:
			key = row[claim_index + 1]
			claimed, sanctioned = row[claim_index + 2 : claim_index + 4]
		else:
			key = claim.name
			claimed, sanctioned = claim.base_total_claimed_amount, claim.base_total_sanctioned_amount
		records.append(frappe._dict(
			claim=claim.name,
			key=key,
			amounts=_row_summary_amounts(claim, Decimal(str(claimed or 0)), Decimal(str(sanctioned or 0))),
			required_details=max(1, len(all_details.get(claim.name, ()))) if detail_scope else 1,
		))
	return records


def _make_filter_aware_xlsx(data, styles, records, summary, table_data):
	"""Retain Frappe's formatting; write trusted formulas only in this workbook."""
	import xlsxwriter
	from xlsxwriter.utility import xl_col_to_name

	from frappe.utils.xlsxutils import make_xlsx

	output = BytesIO()
	# Normal memory mode permits replacing the summary after writing the table.
	# Frappe still writes user-supplied formula-like text as literal strings.
	with xlsxwriter.Workbook(output, {"strings_to_formulas": False}) as workbook:
		workbook.set_calc_mode("auto")
		make_xlsx(data, EXPENSE_CLAIM, wb=workbook, styles=styles)
		worksheet = workbook.worksheets()[0]
		header = len(data) - len(table_data)
		first, last = header + 2, header + 1 + len(records)  # Excel's one-based row numbers
		amount_format = workbook.add_format({"num_format": "General", "align": "right"})
		if not records:
			worksheet.autofilter(header, 0, header, len(table_data[0]) - 1)
			for index in range(len(SUMMARY_LABELS)):
				worksheet.write_formula(index + 2, 1, "=0", amount_format, 0)
		else:
			helper_start = len(table_data[0])
			helper_names = (
				"Claim ID", "Detail ID", "Visible", "Unique Visible", "Approved", "Pending",
				"Rejected", "Total Expense", "Reimbursed", "Required Details", "Visible Details",
				"Paid Contribution", "Incomplete Payment",
			)
			columns = {name: xl_col_to_name(helper_start + i) for i, name in enumerate(helper_names)}
			ranges = {name: f"${col}${first}:${col}${last}" for name, col in columns.items()}
			worksheet.write_row(header, helper_start, [f"_{name}" for name in helper_names])
			worksheet.set_column(helper_start, helper_start + len(helper_names) - 1, None, None, {"hidden": True})
			# Include helpers in sorting so identities and amounts move with the visible columns.
			worksheet.autofilter(header, 0, last - 1, helper_start + len(helper_names) - 1)
			key_counts = Counter(record.key for record in records if record.key)
			visible_details = {}
			visible_rows = Counter(record.claim for record in records)
			for record in records:
				if record.key:
					visible_details.setdefault(record.claim, set()).add(record.key)
			for row_number, record in enumerate(records, first):
				cells = {name: f"${col}{row_number}" for name, col in columns.items()}
				# COUNTIFS/SUMIFS interpret wildcard and comparison characters in
				# criteria. IDs are literal values, even when names contain these.
				criteria = {
					name: f'"="&SUBSTITUTE(SUBSTITUTE(SUBSTITUTE({cells[name]},"~","~~"),"*","~*"),"?","~?")'
					for name in ("Claim ID", "Detail ID")
				}
				cache_unique = 1 / key_counts[record.key] if record.key else 0
				cache_count = len(visible_details.get(record.claim, ()))
				paid = float(record.amounts["Paid"])
				incomplete = int(bool(paid) and cache_count != record.required_details)
				values = [record.claim, record.key or "", 1, cache_unique,
					*[float(record.amounts[label]) for label in ("Approved", "Pending", "Rejected", "Total Expense")],
					paid, record.required_details, cache_count,
					0 if incomplete else paid / visible_rows[record.claim], incomplete]
				# Explicit strings protect even hidden IDs from spreadsheet formula injection.
				for index, value in enumerate(values):
					if isinstance(value, str):
						worksheet.write_string(row_number - 1, helper_start + index, value)
					else:
						worksheet.write_number(row_number - 1, helper_start + index, value)
				formulas = {"Visible": f'=SUBTOTAL(103,{cells["Claim ID"]})'}
				if not record.key:
					formulas["Unique Visible"] = "=0"
				elif key_counts[record.key] == 1:
					formulas["Unique Visible"] = f'={cells["Visible"]}'
				else:
					formulas["Unique Visible"] = (
						f'=IFERROR({cells["Visible"]}/COUNTIFS({ranges["Detail ID"]},{criteria["Detail ID"]},'
						f'{ranges["Visible"]},1),0)'
					)
				if paid:
					formulas["Visible Details"] = (
						f'=SUMIFS({ranges["Unique Visible"]},{ranges["Claim ID"]},{criteria["Claim ID"]})'
					)
					complete = f'ROUND({cells["Visible Details"]},0)={cells["Required Details"]}'
					formulas["Paid Contribution"] = (
						f'=IF(AND({cells["Visible"]}=1,{complete}),{cells["Reimbursed"]}/'
						f'MAX(1,COUNTIFS({ranges["Claim ID"]},{criteria["Claim ID"]},{ranges["Visible"]},1)),0)'
					)
					formulas["Incomplete Payment"] = f'=IF(AND({cells["Visible"]}=1,NOT({complete})),1,0)'
				for name, formula in formulas.items():
					index = helper_names.index(name)
					worksheet.write_formula(row_number - 1, helper_start + index, formula, None, values[index])
			for index, label in enumerate(SUMMARY_LABELS):
				if label == "Paid":
					formula = f'=IF(SUM({ranges["Incomplete Payment"]})>0,"",SUM({ranges["Paid Contribution"]}))'
				else:
					formula = f'=SUMPRODUCT({ranges["Unique Visible"]},{ranges[label]})'
				cached = "" if summary[label] is None else summary[label]
				worksheet.write_formula(index + 2, 1, formula, amount_format, cached)
			# The existing separator row doubles as a conditional explanation.
			message = _("Paid is unavailable: payments belong to whole claims and cannot be attributed to the included expense details.")
			formula_message = message.replace('"', '""')
			worksheet.write_formula(header - 1, 0,
				f'=IF(SUM({ranges["Incomplete Payment"]})>0,"{formula_message}","")',
				None, message if summary["Paid"] is None else "")
	return output.getvalue()


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
