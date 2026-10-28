import csv
from io import BytesIO, StringIO
from numbers import Number
from unittest import TestCase
from unittest.mock import patch

import frappe
from openpyxl import load_workbook

from dekure_custom.expense_claim_export import (
	SUMMARY_LABELS,
	_get_unique_claim_names,
	_sanitize_csv_cell,
	add_summary_xlsx_styles,
	build_expense_claim_export_data,
	export_query,
	get_expense_claim_summary,
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
		self.assertEqual(rows[1], ["Summary", "Amount"])
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
		self.assertEqual(values[1][:2], ("Summary", "Amount"))
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
