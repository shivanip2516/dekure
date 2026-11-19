import csv
from io import BytesIO, StringIO
from numbers import Number
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from openpyxl import load_workbook
from openpyxl.formula import Tokenizer
from openpyxl.utils import get_column_letter

from dekure_custom.expense_claim_export import (
	SUMMARY_LABELS,
	_export_expense_claims,
	_has_detail_scope,
	_get_unique_claim_names,
	_get_excel_summary_records,
	_make_filter_aware_xlsx,
	_sanitize_csv_cell,
	_validate_export_permission,
	add_summary_xlsx_styles,
	build_expense_claim_export_data,
	export_query,
	get_expense_claim_summary,
	run_expense_claim_export_job,
)


def _claim(name, **values):
	claim = frappe._dict(
		name=name,
		company="Dekure",
		docstatus=1,
		approval_status="Approved",
		status="Unpaid",
		base_total_claimed_amount=0,
		base_total_sanctioned_amount=0,
		total_amount_reimbursed=0,
		exchange_rate=1,
	)
	claim.update(values)
	return claim


def _company(name="Dekure", currency="INR"):
	return frappe._dict(name=name, default_currency=currency)


class TestExpenseClaimExport(TestCase):
	def setUp(self):
		# Workbook generation reads these settings even for a text-only table.
		settings = patch.object(frappe, "get_system_settings", return_value=None)
		settings.start()
		self.addCleanup(settings.stop)

	def test_other_doctype_export_delegates_unchanged(self):
		with (
			patch(
				"dekure_custom.expense_claim_export.frappe.conf",
				frappe._dict(read_from_replica=False),
			),
			patch("dekure_custom.expense_claim_export.frappe.form_dict", {"doctype": "Lead"}),
			patch(
				"dekure_custom.expense_claim_export.reportview.export_query",
				return_value="standard-export",
			) as standard_export,
		):
			result = export_query()

		self.assertEqual(result, "standard-export")
		standard_export.assert_called_once_with()

	def test_expense_claim_background_export_uses_scoped_job(self):
		form_params = frappe._dict(doctype="Expense Claim", export_in_background=1)
		with (
			patch(
				"dekure_custom.expense_claim_export.frappe.conf",
				frappe._dict(read_from_replica=False),
			),
			patch(
				"dekure_custom.expense_claim_export.frappe.form_dict",
				{"doctype": "Expense Claim"},
			),
			patch(
				"dekure_custom.expense_claim_export.frappe.session",
				frappe._dict(user="approver@example.com"),
			),
			patch(
				"dekure_custom.expense_claim_export.frappe.flags",
				frappe._dict(in_test=False),
			),
			patch(
				"dekure_custom.expense_claim_export.reportview.get_form_params",
				return_value=form_params,
			),
			patch(
				"dekure_custom.expense_claim_export.frappe.get_cached_value",
				return_value="approver@example.com",
			),
			patch("dekure_custom.expense_claim_export.frappe.enqueue") as enqueue,
			patch("dekure_custom.expense_claim_export.frappe.msgprint"),
		):
			self.assertIsNone(export_query())

		enqueue.assert_called_once()
		self.assertEqual(
			enqueue.call_args.args[0],
			"dekure_custom.expense_claim_export.run_expense_claim_export_job",
		)

	def test_summary_uses_installed_status_and_amount_meanings(self):
		claims = [
			_claim(
				"APPROVED-PAID",
				status="Paid",
				base_total_claimed_amount=500,
				base_total_sanctioned_amount=400,
				total_amount_reimbursed=200,
				exchange_rate=2,
			),
			_claim(
				"PENDING",
				docstatus=0,
				approval_status="Draft",
				status="Draft",
				base_total_claimed_amount=300,
				base_total_sanctioned_amount=300,
			),
			_claim(
				"REJECTED",
				approval_status="Rejected",
				status="Rejected",
				base_total_claimed_amount=250,
			),
			_claim(
				"CANCELLED",
				docstatus=2,
				base_total_claimed_amount=999,
				base_total_sanctioned_amount=999,
			),
		]

		with patch(
			"dekure_custom.expense_claim_export.frappe.get_list",
			side_effect=[claims, [_company()]],
		):
			summary = get_expense_claim_summary([claim.name for claim in claims])

		self.assertEqual(
			summary,
			{
				"Approved": 400.0,
				"Pending": 300.0,
				"Rejected": 250.0,
				"Paid": 400.0,
				"Total Expense": 1050.0,
			},
		)

	def test_paid_is_also_approved_without_double_counting_total_expense(self):
		claim = _claim(
			"PAID",
			status="Paid",
			base_total_claimed_amount=100,
			base_total_sanctioned_amount=80,
			total_amount_reimbursed=80,
		)
		with patch(
			"dekure_custom.expense_claim_export.frappe.get_list",
			side_effect=[[claim], [_company()]],
		):
			summary = get_expense_claim_summary([claim.name])

		self.assertEqual(summary["Approved"], 80)
		self.assertEqual(summary["Paid"], 80)
		self.assertEqual(summary["Total Expense"], 100)

	def test_dynamic_summary_formulas_and_visibility_cover_every_detail(self):
		(_, _, content), _, _ = self._export_filtered_rows([500, 300, 153])
		book = load_workbook(BytesIO(content))
		sheet = book.active
		self.assertEqual(sheet.auto_filter.ref, "A9:Q12")
		self.assertEqual(sheet["B3"].value, "=SUMPRODUCT($H$10:$H$12,$I$10:$I$12)")
		self.assertEqual(sheet["B4"].value, "=SUMPRODUCT($H$10:$H$12,$J$10:$J$12)")
		self.assertEqual(sheet["B5"].value, "=SUMPRODUCT($H$10:$H$12,$K$10:$K$12)")
		self.assertEqual(sheet["B6"].value, '=IF(SUM($Q$10:$Q$12)>0,"",SUM($P$10:$P$12))')
		self.assertEqual(sheet["B7"].value, "=SUMPRODUCT($H$10:$H$12,$L$10:$L$12)")
		for row in range(10, 13):
			self.assertEqual(sheet[f"G{row}"].value, f"=SUBTOTAL(103,$E{row})")
			self.assertEqual(sheet[f"H{row}"].value, f"=$G{row}")
			self.assertTrue(sheet[f"E{row}"].value)  # mandatory hidden claim ID is the anchor
		for col in range(5, 18):
			self.assertTrue(any(d.hidden and d.min <= col <= d.max for d in sheet.column_dimensions.values()))
		for col in range(1, 5):
			self.assertFalse(sheet.column_dimensions[get_column_letter(col)].hidden)
		self.assertIn(book.calculation.calcMode, (None, "auto"))  # omitted means Excel's automatic default
		self.assertTrue(book.calculation.fullCalcOnLoad)
		for row in sheet:
			for cell in row:
				if cell.data_type == "f":
					self.assertTrue(Tokenizer(cell.value).items)
					for error in ("#REF!", "#VALUE!", "#NAME?"):
						self.assertNotIn(error, cell.value)

	def test_dynamic_formulas_exclude_existing_footer_and_have_no_fixed_row_limit(self):
		(_, _, content), _, _ = self._export_filtered_rows([500, 300], add_totals_row="1")
		sheet = load_workbook(BytesIO(content)).active
		self.assertEqual(sheet.auto_filter.ref, "A9:Q11")
		self.assertIn("$H$10:$H$11", sheet["B7"].value)
		self.assertEqual(sheet["B12"].value, "Total")
		self.assertIsNone(sheet["G12"].value)
		(_, _, content), _, _ = self._export_filtered_rows([1] * 5000)
		sheet = load_workbook(BytesIO(content)).active
		self.assertEqual(sheet.auto_filter.ref, "A9:Q5009")
		self.assertIn("$H$10:$H$5009", sheet["B7"].value)
		self.assertEqual(sheet["G5009"].value, "=SUBTOTAL(103,$E5009)")
		self.assertEqual(load_workbook(BytesIO(content), data_only=True).active["B7"].value, 5000)

	def test_empty_dynamic_workbook_has_zero_formulas_and_header_only_filter(self):
		(_, _, content), _, _ = self._export_filtered_rows([])
		sheet = load_workbook(BytesIO(content)).active
		self.assertEqual(sheet.auto_filter.ref, "A9:D9")
		for row in range(3, 8):
			self.assertEqual(sheet[f"B{row}"].value, "=0")

	def _paid_dynamic_workbook(self, include_second_detail=True, duplicate=False):
		claims = [_claim("PAID", status="Paid", total_amount_reimbursed=80,
			expense_detail_name=name) for name in ("D1", "D2")]
		rows = [("PAID", "D1", 50, 40)]
		if include_second_detail:
			rows.append(("PAID", "D2", 50, 40))
		if duplicate:
			rows.append(rows[0])
		# Here there are no selected visible fields before the internal fields.
		records = _get_excel_summary_records(rows, 0, True, claims)
		details = [frappe._dict(parent=row[0], name=row[1], base_amount=row[2],
			base_sanctioned_amount=row[3]) for row in rows]
		summary = get_expense_claim_summary(["PAID"], details, claim_rows=claims)
		table = [["Sr", "Amount"], *[[i + 1, row[2]] for i, row in enumerate(rows)]]
		data = build_expense_claim_export_data(table, summary, include_payment_note=False)
		content = _make_filter_aware_xlsx(data, add_summary_xlsx_styles({}, 8), records, summary, table)
		return load_workbook(BytesIO(content)), load_workbook(BytesIO(content), data_only=True)

	def test_paid_formulas_require_every_unique_detail_to_be_visible(self):
		book, cached = self._paid_dynamic_workbook()
		sheet = book.active
		self.assertEqual(cached.active["B6"].value, 80)
		self.assertEqual(cached.active["B3"].value, 80)  # Approved and Paid overlap
		self.assertEqual(cached.active["B7"].value, 100)
		self.assertTrue(sheet["M10"].value.startswith('=SUMIFS($F$10:$F$11,$C$10:$C$11,"="&SUBSTITUTE('))
		self.assertIn('$C10,"~","~~"', sheet["M10"].value)
		self.assertIn("ROUND($M10,0)=$L10", sheet["N10"].value)
		self.assertIn('COUNTIFS($C$10:$C$11,"="&SUBSTITUTE(', sheet["N10"].value)
		self.assertIn('$E$10:$E$11,1)', sheet["N10"].value)
		self.assertEqual(sheet["O10"].value, "=IF(AND($E10=1,NOT(ROUND($M10,0)=$L10)),1,0)")
		self.assertEqual(sheet["L10"].value, 2)
		self.assertEqual(sheet["L11"].value, 2)
		self.assertEqual(sheet["B6"].value, '=IF(SUM($O$10:$O$11)>0,"",SUM($N$10:$N$11))')
		self.assertEqual(sheet.auto_filter.ref, "A9:O11")

	def test_partial_paid_export_has_dynamic_blank_and_conditional_explanation(self):
		book, cached = self._paid_dynamic_workbook(include_second_detail=False)
		self.assertIsNone(cached.active["B6"].value)
		self.assertIn("cannot be attributed", cached.active["A8"].value)
		self.assertEqual(book.active["A9"].value, "Sr")  # no extra visible rows
		self.assertEqual(book.active["L10"].value, 2)
		self.assertEqual(cached.active["M10"].value, 1)
		self.assertTrue(book.active["A8"].value.startswith("=IF(SUM("))

	def test_duplicate_expense_helpers_count_only_visible_occurrences(self):
		book, cached = self._paid_dynamic_workbook(duplicate=True)
		sheet = book.active
		self.assertEqual(cached.active["B7"].value, 100)
		self.assertEqual(cached.active["B6"].value, 80)
		self.assertTrue(sheet["F10"].value.startswith('=IFERROR($E10/COUNTIFS($D$10:$D$12,"="&SUBSTITUTE('))
		self.assertIn('$D10,"~","~~"', sheet["F10"].value)
		self.assertTrue(sheet["F10"].value.endswith('$E$10:$E$12,1),0)'))
		self.assertEqual(cached.active["F10"].value, 0.5)
		self.assertEqual(cached.active["F12"].value, 0.5)
		self.assertEqual(sheet["F11"].value, "=$E11")

	def test_child_rows_do_not_duplicate_claims(self):
		fields = ["`tabExpense Claim`.`name`", "`tabExpense Claim Detail`.`expense_type`"]
		rows = [
			["EC-1", "Travel"],
			["EC-1", "Meals"],
			["EC-2", "Travel"],
		]

		self.assertEqual(_get_unique_claim_names(rows, fields), ["EC-1", "EC-2"])

	def test_empty_export_has_zero_summary_and_table_headers(self):
		summary = get_expense_claim_summary([])
		table = [["Sr", "ID", "Employee"]]

		data = build_expense_claim_export_data(table, summary)

		self.assertEqual(list(summary), list(SUMMARY_LABELS))
		self.assertTrue(all(value == 0 for value in summary.values()))
		self.assertEqual(data[-1], table[0])

	def test_csv_layout_preserves_table(self):
		from frappe.desk.utils import get_csv_bytes

		table = [["Sr", "ID", "Employee"], [1, "EC-1", "Naitri, Solanki"]]
		summary = {label: index for index, label in enumerate(SUMMARY_LABELS, start=1)}
		data = build_expense_claim_export_data(table, summary)

		content = get_csv_bytes(
			data,
			{"delimiter": ",", "quoting": csv.QUOTE_MINIMAL, "decimal_sep": "."},
		).decode("utf-8")
		rows = list(csv.reader(StringIO(content)))

		self.assertEqual(rows[0], ["Expense Claim Report"])
		self.assertEqual(rows[1], ["Summary", "Amount (Company Currency)"])
		self.assertEqual(rows[2:7], [[label, str(summary[label])] for label in SUMMARY_LABELS])
		self.assertEqual(rows[7], [])
		self.assertEqual(rows[8:], [[str(value) for value in row] for row in table])

	def test_csv_formula_like_text_is_neutralized(self):
		for value in ("=1+1", "+SUM(A1:A2)", "-1+2", "@cmd", "\t=1", "\r=1"):
			self.assertEqual(_sanitize_csv_cell(value), f"'{value}")

		self.assertEqual(_sanitize_csv_cell("Expense Claim"), "Expense Claim")
		self.assertEqual(_sanitize_csv_cell(""), "")

	def test_excel_layout_keeps_amounts_numeric_and_table_unchanged(self):
		from frappe.utils.xlsxutils import make_xlsx

		table = [["Sr", "ID", "Amount"], [1, "EC-1", 125.5]]
		summary = {label: float(index) for index, label in enumerate(SUMMARY_LABELS, start=1)}
		data = build_expense_claim_export_data(table, summary)
		styles = add_summary_xlsx_styles({}, table_row_offset=8)

		workbook = load_workbook(BytesIO(make_xlsx(data, "Expense Claim", styles=styles).getvalue()))
		values = list(workbook.active.values)

		self.assertEqual(values[0][0], "Expense Claim Report")
		self.assertEqual(values[1][:2], ("Summary", "Amount (Company Currency)"))
		self.assertIsInstance(values[2][1], Number)
		self.assertEqual(values[8][:3], tuple(table[0]))
		self.assertEqual(values[9][:3], tuple(table[1]))

	def test_excel_table_styles_are_shifted_below_summary(self):
		styles = {
			"styles": [{"bold": True}],
			"row_styles": {0: [0]},
			"cell_styles": {(1, 2): [0]},
			"column_styles": {2: [0]},
		}

		shifted = add_summary_xlsx_styles(styles, table_row_offset=8)

		self.assertEqual(shifted["row_styles"][0], [1])
		self.assertEqual(shifted["row_styles"][8], [0])
		self.assertEqual(shifted["cell_styles"][(9, 2)], [0])
		self.assertEqual(shifted["column_styles"], {2: [0]})

	@patch("dekure_custom.expense_claim_export.frappe.throw", side_effect=frappe.ValidationError)
	def test_different_company_currencies_are_not_combined(self, throw):
		claims = [
			_claim("INR", company="India"),
			_claim("USD", company="US"),
		]
		with patch(
			"dekure_custom.expense_claim_export.frappe.get_list",
			side_effect=[claims, [_company("India", "INR"), _company("US", "USD")]],
		):
			with self.assertRaises(frappe.ValidationError):
				get_expense_claim_summary(["INR", "USD"])

		self.assertIn("different base currencies", throw.call_args.args[0])

	def test_summary_query_uses_only_exported_claim_names_and_permissions(self):
		claim = _claim("EC-FILTERED")
		with patch(
			"dekure_custom.expense_claim_export.frappe.get_list",
			side_effect=[[claim], [_company()]],
		) as get_list:
			get_expense_claim_summary([claim.name])

		self.assertEqual(
			get_list.call_args_list[0].kwargs["filters"],
			{"name": ("in", ["EC-FILTERED"])},
		)
		self.assertNotIn("ignore_permissions", get_list.call_args_list[0].kwargs)

	def test_filtered_details_preserve_status_meanings_and_decimal_precision(self):
		claims, details = [], []
		for name, values, amounts in (
			("APPROVED", {}, [("10.10", "8.05"), ("0.20", "0.10")]),
			("DRAFT", {"docstatus": 0, "approval_status": "Draft"}, [("12.30", "0")]),
			("REJECTED", {"approval_status": "Rejected"}, [("20.40", "0")]),
			("CANCELLED", {"docstatus": 2}, [("999", "999")]),
		):
			for index, (amount, sanctioned) in enumerate(amounts):
				detail_name = f"{name}-{index}"
				claims.append(_claim(name, expense_detail_name=detail_name,
					base_total_claimed_amount=5000, base_total_sanctioned_amount=5000, **values))
				details.append(frappe._dict(name=detail_name, parent=name,
					base_amount=amount, base_sanctioned_amount=sanctioned))
		# A join to another child table must not count an expense twice.
		details.append(details[0])
		with patch("dekure_custom.expense_claim_export.frappe.get_list",
			side_effect=[claims, [_company()]]):
			summary = get_expense_claim_summary(list(dict.fromkeys(c.name for c in claims)), details)
		self.assertEqual(summary, {"Approved": 8.15, "Pending": 12.3,
			"Rejected": 20.4, "Paid": 0, "Total Expense": 43})

	def test_partial_paid_claim_is_not_arbitrarily_allocated(self):
		claims = [_claim("PAID", status="Paid", total_amount_reimbursed=80,
			expense_detail_name=name) for name in ("D1", "D2")]
		details = [frappe._dict(parent="PAID", name="D1", base_amount=30, base_sanctioned_amount=20)]
		with patch("dekure_custom.expense_claim_export.frappe.get_list",
			side_effect=[claims, [_company()]]):
			summary = get_expense_claim_summary(["PAID"], details)
		self.assertIsNone(summary["Paid"])
		self.assertEqual(summary["Total Expense"], 30)
		data = build_expense_claim_export_data([["ID"]], summary)
		self.assertIn("cannot be attributed", data[7][0])

	def test_complete_paid_claim_counts_reimbursement_once(self):
		claims = [_claim("PAID", status="Paid", total_amount_reimbursed=40,
			exchange_rate=2, expense_detail_name=name) for name in ("D1", "D2")]
		details = [frappe._dict(parent="PAID", name=name, base_amount=50,
			base_sanctioned_amount=40) for name in ("D1", "D2")]
		with patch("dekure_custom.expense_claim_export.frappe.get_list",
			side_effect=[claims, [_company()]]):
			summary = get_expense_claim_summary(["PAID"], details)
		self.assertEqual(summary["Paid"], 80)
		self.assertEqual(summary["Approved"], 80)
		self.assertEqual(summary["Total Expense"], 100)

	def test_detail_scope_in_columns_and_all_filter_forms(self):
		for params in (
			{"fields": ["`tabExpense Claim Detail`.`amount`"]},
			{"fields": ["expenses.amount"]},
			{"filters": [["Expense Claim Detail", "expense_date", "between", ["2026-10-16", "2026-10-31"]]]},
			{"filters": {"expenses.expense_type": "Travel"}},
			{"or_filters": [["Expense Claim Detail", "expense_type", "=", "Food"]]},
		):
			with self.subTest(params=params):
				self.assertTrue(_has_detail_scope(params))
		self.assertFalse(_has_detail_scope({"fields": ["name"], "filters": {"employee": "EMP-1"}}))

	def _export_filtered_rows(self, amounts, file_format="Excel", **options):
		"""Exercise the real export and workbook writer with permission-filtered query rows."""
		params = frappe._dict(doctype="Expense Claim", file_format_type=file_format,
			fields=["`tabExpense Claim`.`name`", "`tabExpense Claim Detail`.`amount`"],
			filters=[["Expense Claim Detail", "expense_date", "between", ["2026-10-16", "2026-10-31"]]],
			as_list=True, **options)
		query = MagicMock()
		def execute(**kwargs):
			query.fields = kwargs["fields"]
			return [("HR-EXP-2026-00110", amount, "HR-EXP-2026-00110", f"D{i}", amount,
				amount, "owner@example.com") for i, amount in enumerate(amounts)]
		query.execute.side_effect = execute
		claims = [_claim("HR-EXP-2026-00110", base_total_claimed_amount=1563,
			base_total_sanctioned_amount=1563, expense_detail_name=f"D{i}") for i in range(4)]
		def field_info(fields, doctype):
			return [{"label": str(field), "fieldname": str(field), "fieldtype": "Data", "translatable": False}
				for field in fields]
		with (
			patch("dekure_custom.expense_claim_export.DatabaseQuery", return_value=query),
			patch("dekure_custom.expense_claim_export.frappe.get_list", side_effect=[claims, [_company()]]),
			patch("dekure_custom.expense_claim_export.frappe.permissions.can_export", return_value=True),
			patch("dekure_custom.expense_claim_export.make_access_log"),
			patch("dekure_custom.expense_claim_export.reportview.get_field_info", side_effect=field_info),
			patch("dekure_custom.expense_claim_export.reportview.handle_duration_fieldtype_values", side_effect=lambda dt, data, fields: data),
			patch("frappe.utils.xlsxutils.get_default_xlsx_styles", return_value={}),
		):
			result = _export_expense_claims(params,
				{"delimiter": ",", "quoting": csv.QUOTE_MINIMAL, "decimal_sep": "."}, populate_response=False)
		return result, query.execute.call_args.kwargs, params

	def test_regression_153_detail_does_not_include_1563_parent_total(self):
		(_, extension, content), _, _ = self._export_filtered_rows([153])
		values = list(load_workbook(BytesIO(content), data_only=True).active.values)
		self.assertEqual(extension, "xlsx")
		self.assertEqual(values[6][:2], ("Total Expense", 153))
		self.assertEqual(values[2][:2], ("Approved", 153))
		self.assertEqual(values[9][2], 153)
		self.assertEqual(values[8][4], "_Claim ID")  # helpers follow every original column

	def test_export_summary_matches_detail_sum_after_filters_change_and_zero(self):
		for amounts in ([153], [500, 300], [0], [], [153, 10612]):
			with self.subTest(amounts=amounts):
				(_, _, content), _, _ = self._export_filtered_rows(amounts)
				values = list(load_workbook(BytesIO(content), data_only=True).active.values)
				self.assertEqual(values[6][1], sum(amounts))
				self.assertEqual(values[6][1], sum(row[2] for row in values[9:]))

	def test_csv_and_excel_use_the_same_filtered_summary(self):
		(_, _, csv_content), _, _ = self._export_filtered_rows([153], file_format="CSV")
		(_, _, excel_content), _, _ = self._export_filtered_rows([153])
		csv_rows = list(csv.reader(StringIO(csv_content.decode("utf-8"))))
		xlsx_rows = list(load_workbook(BytesIO(excel_content), data_only=True).active.values)
		self.assertEqual(float(csv_rows[6][1]), xlsx_rows[6][1])
		self.assertEqual(float(csv_rows[9][2]), xlsx_rows[9][2])

	def test_selection_and_visible_page_do_not_replace_active_filters(self):
		(_, _, content), query_params, original = self._export_filtered_rows([153],
			selected_items='["HR-EXP-2026-00110"]', visible_names='["HR-EXP-2026-00110"]',
			start=20, page_length=20, with_link_titles=True,
			or_filters=[["Expense Claim", "status", "=", "Unpaid"]])
		date_filter = next(f for f in query_params["filters"] if f.fieldname == "expense_date")
		self.assertEqual(tuple(date_filter.value), tuple(original.filters[0][3]))
		self.assertEqual(query_params["or_filters"], original.or_filters)
		self.assertTrue(any(f.fieldname == "name" for f in query_params["filters"]))
		for key in ("visible_names", "selected_items", "with_link_titles", "start", "page_length"):
			self.assertNotIn(key, query_params)
		self.assertEqual(original.page_length, 20)  # never mutate caller/background-job args
		self.assertEqual(list(load_workbook(BytesIO(content), data_only=True).active.values)[6][1], 153)

	def test_report_parent_filters_are_forwarded_without_reinterpretation(self):
		for key, value in (("company", "Dekure"), ("employee", "EMP-1"),
			("employee_name", "Employee One"), ("department", "Sales"),
			("posting_date", ["between", ["2026-10-01", "2026-10-31"]]),
			("approval_status", "Approved"), ("status", "Unpaid")):
			with self.subTest(field=key):
				_, query_params, _ = self._export_filtered_rows([153], or_filters={key: value})
				self.assertEqual(query_params["or_filters"], {key: value})

	def test_grouped_export_retains_native_aggregation(self):
		params = {"doctype": "Expense Claim", "fields": [{"SUM": "expenses.amount"}], "group_by": "employee"}
		with patch("dekure_custom.expense_claim_export.reportview._export_query", return_value="grouped") as native:
			self.assertEqual(_export_expense_claims(params, {}, populate_response=False), "grouped")
		native.assert_called_once_with(params, {}, populate_response=False)

	def test_export_permission_and_owner_checks_are_preserved(self):
		with (
			patch("dekure_custom.expense_claim_export.frappe.permissions.can_export", side_effect=[False, True]),
			patch("dekure_custom.expense_claim_export.frappe.session", frappe._dict(user="owner@example.com")),
		):
			with self.assertRaises(frappe.PermissionError):
				_validate_export_permission("Expense Claim", [("CLAIM", "other@example.com")])

	def test_background_job_uses_same_export_implementation_and_filter_snapshot(self):
		params = {"filters": [["Expense Claim Detail", "expense_type", "=", "Travel"]]}
		with (
			patch("dekure_custom.expense_claim_export._export_expense_claims",
				return_value=("Expense Claim", "xlsx", b"workbook")) as export,
			patch("frappe.desk.utils.send_report_email") as email,
		):
			run_expense_claim_export_job("user@example.com", params, {})
		export.assert_called_once_with(params, {}, populate_response=False)
		self.assertEqual(email.call_args.args[3], b"workbook")

	def test_filtered_amounts_use_stored_base_fields_without_converting_twice(self):
		claim = _claim("FOREIGN", exchange_rate=2, expense_detail_name="D1",
			base_total_claimed_amount=1000, base_total_sanctioned_amount=800)
		detail = frappe._dict(parent="FOREIGN", name="D1", amount=50, sanctioned_amount=40,
			base_amount=100, base_sanctioned_amount=80)
		with patch("dekure_custom.expense_claim_export.frappe.get_list",
			side_effect=[[claim], [_company()]]):
			summary = get_expense_claim_summary(["FOREIGN"], [detail])
		self.assertEqual(summary["Total Expense"], 100)
		self.assertEqual(summary["Approved"], 80)
