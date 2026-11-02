import frappe
from frappe.utils import add_days, cint, date_diff, flt, getdate


PAID_LEAVE_FIELD = "custom_paid_leave_days"
WEEKOFF_FIELD = "custom_weekoff_days"
HOLIDAY_FIELD = "custom_holiday_days"


def set_salary_slip_display_days(doc, method=None):
	"""Populate informational leave and holiday fields on draft Salary Slips."""
	if cint(doc.docstatus) != 0 or not (doc.employee and doc.start_date and doc.end_date):
		return

	set_paid_leave_days(doc)
	holiday_breakdown = get_salary_slip_holiday_breakdown(
		doc.employee,
		doc.start_date,
		doc.end_date,
	)
	setattr(doc, WEEKOFF_FIELD, holiday_breakdown.weekoff_days)
	setattr(doc, HOLIDAY_FIELD, holiday_breakdown.holiday_days)


def set_paid_leave_days(doc, method=None):
	"""Populate informational paid leave on draft Salary Slips only."""
	if cint(doc.docstatus) != 0 or not (doc.employee and doc.start_date and doc.end_date):
		return

	setattr(
		doc,
		PAID_LEAVE_FIELD,
		get_paid_leave_days(doc.employee, doc.start_date, doc.end_date),
	)


def get_paid_leave_days(employee, start_date, end_date):
	"""Count approved fully paid leave inside a payroll period.

	This is also safe for use by a print format as a read-only fallback for an
	already submitted Salary Slip that predates the stored custom field.
	"""
	if not employee or not start_date or not end_date:
		return 0.0

	start_date, end_date = getdate(start_date), getdate(end_date)
	if start_date > end_date:
		return 0.0

	paid_leave_types = frappe.get_all(
		"Leave Type",
		filters={"is_lwp": 0, "is_ppl": 0},
		pluck="name",
	)
	if not paid_leave_types:
		return 0.0

	applications = frappe.get_all(
		"Leave Application",
		filters={
			"employee": employee,
			"leave_type": ["in", paid_leave_types],
			"docstatus": 1,
			"status": "Approved",
			"from_date": ["<=", end_date],
			"to_date": [">=", start_date],
		},
		fields=["name", "leave_type", "from_date", "to_date", "half_day", "half_day_date"],
	)

	from hrms.hr.doctype.leave_application.leave_application import get_number_of_leave_days

	# HRMS prevents overlapping approved applications. The date map additionally
	# keeps duplicate query rows or legacy overlaps from counting a day twice.
	paid_days_by_date = {}
	seen_applications = set()
	for application in applications:
		if application.name in seen_applications:
			continue
		seen_applications.add(application.name)

		application_start = max(getdate(application.from_date), start_date)
		application_end = min(getdate(application.to_date), end_date)
		half_day_date = getdate(application.half_day_date) if application.half_day_date else None

		for offset in range(date_diff(application_end, application_start) + 1):
			leave_date = add_days(application_start, offset)
			is_half_day = cint(application.half_day) and (
				half_day_date == leave_date
				or (getdate(application.from_date) == getdate(application.to_date) == leave_date)
			)
			leave_days = get_number_of_leave_days(
				employee,
				application.leave_type,
				leave_date,
				leave_date,
				half_day=is_half_day,
				half_day_date=leave_date if is_half_day else None,
			)
			paid_days_by_date[leave_date] = max(flt(leave_days), paid_days_by_date.get(leave_date, 0.0))

	return flt(sum(paid_days_by_date.values()))


def get_salary_slip_holiday_breakdown(employee, start_date, end_date):
	"""Return non-overlapping weekly-off and holiday dates for a payroll period."""
	result = frappe._dict(
		holiday_list_ranges=[],
		weekoff_dates=[],
		holiday_dates=[],
		weekoff_days=0.0,
		holiday_days=0.0,
	)
	if not employee or not start_date or not end_date:
		return result

	start_date, end_date = getdate(start_date), getdate(end_date)
	if start_date > end_date:
		return result

	from hrms.utils.holiday_list import (
		fill_employee_holiday_list_date_gaps_with_company_holiday_list,
		get_assigned_holiday_lists_to_employee_and_company,
	)

	company = frappe.db.get_value("Employee", employee, "company")
	assigned_to = [employee]
	if company:
		assigned_to.append(company)

	assignment_map = get_assigned_holiday_lists_to_employee_and_company(
		assigned_to,
		start_date,
		end_date,
	)
	ranges = fill_employee_holiday_list_date_gaps_with_company_holiday_list(
		assignment_map.get(employee, []),
		assignment_map.get(company, []) if company else [],
		start_date,
		end_date,
	)
	if not ranges:
		return result

	holiday_lists = list({row["holiday_list"] for row in ranges})
	holiday_rows = frappe.get_all(
		"Holiday",
		filters={
			"parent": ["in", holiday_lists],
			"holiday_date": ["between", [start_date, end_date]],
		},
		fields=["parent", "holiday_date", "weekly_off"],
	)

	# A weekly-off marker wins if legacy or overlapping rows classify the same
	# date differently. This keeps each calendar date in exactly one count.
	weekly_off_by_date = {}
	for date_range in ranges:
		range_start = max(getdate(date_range["from_date"]), start_date)
		range_end = min(getdate(date_range["to_date"]), end_date)
		for holiday in holiday_rows:
			holiday_date = getdate(holiday.holiday_date)
			if (
				holiday.parent == date_range["holiday_list"]
				and range_start <= holiday_date <= range_end
			):
				weekly_off_by_date[holiday_date] = bool(
					weekly_off_by_date.get(holiday_date) or cint(holiday.weekly_off)
				)

	result.holiday_list_ranges = ranges
	result.weekoff_dates = sorted(date for date, is_weekoff in weekly_off_by_date.items() if is_weekoff)
	result.holiday_dates = sorted(date for date, is_weekoff in weekly_off_by_date.items() if not is_weekoff)
	result.weekoff_days = flt(len(result.weekoff_dates))
	result.holiday_days = flt(len(result.holiday_dates))
	return result
