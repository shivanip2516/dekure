from datetime import date
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from dekure_custom import salary_slip


def _application(name, leave_type, from_date, to_date, half_day=0, half_day_date=None):
	return frappe._dict(
		name=name,
		leave_type=leave_type,
		from_date=from_date,
		to_date=to_date,
		half_day=half_day,
		half_day_date=half_day_date,
	)


class TestSalarySlipPaidLeave(TestCase):
	def calculate(self, applications, day_values=None):
		day_values = day_values or {}
		with patch.object(
			frappe,
			"get_all",
			side_effect=[["Privilege Leave", "Sick Leave"], applications],
		) as get_all, patch(
			"hrms.hr.doctype.leave_application.leave_application.get_number_of_leave_days",
			side_effect=lambda employee, leave_type, from_date, to_date, **kwargs:
				day_values.get(from_date, 0.5 if kwargs.get("half_day") else 1.0),
		) as leave_days:
			result = salary_slip.get_paid_leave_days("EMP-1", date(2026, 10, 1), date(2026, 10, 31))
		return result, get_all, leave_days

	def test_full_and_half_day_paid_leave(self):
		applications = [
			_application("LA-1", "Privilege Leave", date(2026, 10, 10), date(2026, 10, 10)),
			_application(
				"LA-2", "Sick Leave", date(2026, 10, 24), date(2026, 10, 24),
				half_day=1, half_day_date=date(2026, 10, 24),
			),
		]

		result, get_all, leave_days = self.calculate(applications)

		self.assertEqual(result, 1.5)
		self.assertEqual(leave_days.call_count, 2)

	def test_multiple_half_days_total_one_day(self):
		applications = [
			_application("LA-1", "Privilege Leave", date(2026, 10, 10), date(2026, 10, 10), 1),
			_application("LA-2", "Privilege Leave", date(2026, 10, 20), date(2026, 10, 20), 1),
		]

		result, get_all, leave_days = self.calculate(applications)

		self.assertEqual(result, 1.0)

	def test_spanning_leave_is_clipped_to_salary_period(self):
		application = _application(
			"LA-1", "Privilege Leave", date(2026, 9, 29), date(2026, 10, 2),
		)

		result, get_all, leave_days = self.calculate([application])

		self.assertEqual(result, 2.0)
		self.assertEqual(
			[call.args[2] for call in leave_days.call_args_list],
			[date(2026, 10, 1), date(2026, 10, 2)],
		)

	def test_holidays_and_duplicate_rows_are_not_double_counted(self):
		application = _application(
			"LA-1", "Privilege Leave", date(2026, 10, 10), date(2026, 10, 11),
		)

		result, get_all, leave_days = self.calculate(
			[application, application],
			{date(2026, 10, 10): 1.0, date(2026, 10, 11): 0.0},
		)

		self.assertEqual(result, 1.0)
		self.assertEqual(leave_days.call_count, 2)

	def test_query_excludes_unpaid_partially_paid_cancelled_and_rejected_leave(self):
		result, get_all, leave_days = self.calculate([])

		self.assertEqual(result, 0.0)
		leave_type_call, application_call = get_all.call_args_list
		self.assertEqual(leave_type_call.kwargs["filters"], {"is_lwp": 0, "is_ppl": 0})
		self.assertEqual(application_call.kwargs["filters"]["docstatus"], 1)
		self.assertEqual(application_call.kwargs["filters"]["status"], "Approved")
		leave_days.assert_not_called()

	@patch("dekure_custom.salary_slip.get_paid_leave_days", return_value=0.5)
	def test_draft_hook_is_informational_only(self, calculate):
		doc = frappe._dict(
			docstatus=0,
			employee="EMP-1",
			start_date=date(2026, 10, 1),
			end_date=date(2026, 10, 31),
			payment_days=25,
			leave_without_pay=0,
			gross_pay=40000,
			net_pay=40000,
		)
		before = {field: doc[field] for field in (
			"payment_days", "leave_without_pay", "gross_pay", "net_pay",
		)}

		salary_slip.set_paid_leave_days(doc)

		self.assertEqual(doc.custom_paid_leave_days, 0.5)
		self.assertEqual(before, {field: doc[field] for field in before})

	@patch("dekure_custom.salary_slip.get_paid_leave_days")
	def test_submitted_salary_slip_is_not_modified(self, calculate):
		doc = frappe._dict(
			docstatus=1,
			employee="EMP-1",
			start_date=date(2026, 10, 1),
			end_date=date(2026, 10, 31),
			custom_paid_leave_days=None,
		)

		salary_slip.set_paid_leave_days(doc)

		self.assertIsNone(doc.custom_paid_leave_days)
		calculate.assert_not_called()


class TestSalarySlipHolidayBreakdown(TestCase):
	def test_weekly_offs_and_holidays_are_separate_and_deduplicated(self):
		ranges = [
			{
				"holiday_list": "DI Holiday List 2026",
				"from_date": date(2026, 10, 1),
				"to_date": date(2026, 10, 31),
			}
		]
		holiday_rows = [
			frappe._dict(parent="DI Holiday List 2026", holiday_date=date(2026, 10, 2), weekly_off=0),
			frappe._dict(parent="DI Holiday List 2026", holiday_date=date(2026, 10, 4), weekly_off=1),
			frappe._dict(parent="DI Holiday List 2026", holiday_date=date(2026, 10, 4), weekly_off=0),
			frappe._dict(parent="DI Holiday List 2026", holiday_date=date(2026, 10, 11), weekly_off=1),
		]

		database = MagicMock()
		database.get_value.return_value = "Dekure Instruments"
		with patch.object(salary_slip.frappe, "db", database), patch(
			"hrms.utils.holiday_list.get_assigned_holiday_lists_to_employee_and_company",
			return_value={"DI-007": ranges},
		) as assignments, patch(
			"hrms.utils.holiday_list.fill_employee_holiday_list_date_gaps_with_company_holiday_list",
			return_value=ranges,
		), patch.object(frappe, "get_all", return_value=holiday_rows):
			result = salary_slip.get_salary_slip_holiday_breakdown(
				"DI-007", date(2026, 10, 1), date(2026, 10, 31)
			)

		self.assertEqual(result.weekoff_dates, [date(2026, 10, 4), date(2026, 10, 11)])
		self.assertEqual(result.holiday_dates, [date(2026, 10, 2)])
		self.assertEqual(result.weekoff_days, 2.0)
		self.assertEqual(result.holiday_days, 1.0)
		assignments.assert_called_once_with(
			["DI-007", "Dekure Instruments"],
			date(2026, 10, 1),
			date(2026, 10, 31),
		)

	def test_only_holidays_in_effective_list_ranges_are_counted(self):
		ranges = [
			{
				"holiday_list": "Old List",
				"from_date": date(2026, 10, 1),
				"to_date": date(2026, 10, 15),
			},
			{
				"holiday_list": "New List",
				"from_date": date(2026, 10, 16),
				"to_date": date(2026, 10, 31),
			},
		]
		holiday_rows = [
			frappe._dict(parent="Old List", holiday_date=date(2026, 10, 10), weekly_off=1),
			frappe._dict(parent="Old List", holiday_date=date(2026, 10, 20), weekly_off=1),
			frappe._dict(parent="New List", holiday_date=date(2026, 10, 20), weekly_off=0),
		]

		database = MagicMock()
		database.get_value.return_value = "Dekure Instruments"
		with patch.object(salary_slip.frappe, "db", database), patch(
			"hrms.utils.holiday_list.get_assigned_holiday_lists_to_employee_and_company",
			return_value={},
		), patch(
			"hrms.utils.holiday_list.fill_employee_holiday_list_date_gaps_with_company_holiday_list",
			return_value=ranges,
		), patch.object(frappe, "get_all", return_value=holiday_rows):
			result = salary_slip.get_salary_slip_holiday_breakdown(
				"DI-007", date(2026, 10, 1), date(2026, 10, 31)
			)

		self.assertEqual(result.weekoff_dates, [date(2026, 10, 10)])
		self.assertEqual(result.holiday_dates, [date(2026, 10, 20)])

	@patch("dekure_custom.salary_slip.get_salary_slip_holiday_breakdown")
	@patch("dekure_custom.salary_slip.set_paid_leave_days")
	def test_draft_display_hook_does_not_change_payroll_values(self, set_paid_leave, breakdown):
		breakdown.return_value = frappe._dict(weekoff_days=4.0, holiday_days=2.0)
		doc = frappe._dict(
			docstatus=0,
			employee="DI-007",
			start_date=date(2026, 10, 1),
			end_date=date(2026, 10, 31),
			total_working_days=25,
			payment_days=25,
			leave_without_pay=0,
			gross_pay=40000,
			net_pay=40000,
		)
		payroll_fields = (
			"total_working_days", "payment_days", "leave_without_pay", "gross_pay", "net_pay",
		)
		before = {field: doc[field] for field in payroll_fields}

		salary_slip.set_salary_slip_display_days(doc)

		self.assertEqual(doc.custom_weekoff_days, 4.0)
		self.assertEqual(doc.custom_holiday_days, 2.0)
		self.assertEqual(before, {field: doc[field] for field in payroll_fields})
		set_paid_leave.assert_called_once_with(doc)

	@patch("dekure_custom.salary_slip.get_salary_slip_holiday_breakdown")
	@patch("dekure_custom.salary_slip.set_paid_leave_days")
	def test_submitted_display_hook_does_nothing(self, set_paid_leave, breakdown):
		doc = frappe._dict(
			docstatus=1,
			employee="DI-007",
			start_date=date(2026, 10, 1),
			end_date=date(2026, 10, 31),
			custom_weekoff_days=None,
			custom_holiday_days=None,
		)

		salary_slip.set_salary_slip_display_days(doc)

		self.assertIsNone(doc.custom_weekoff_days)
		self.assertIsNone(doc.custom_holiday_days)
		set_paid_leave.assert_not_called()
		breakdown.assert_not_called()
